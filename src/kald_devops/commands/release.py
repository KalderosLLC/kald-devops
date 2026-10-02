"""Release pipeline commands (vsrm.dev.azure.com release definitions):
listing, deploying, provisioning environments, and creating releases from an
existing artifact."""
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from kald_devops.common import (
    http_get, http_post, http_patch, http_put, extract_error, resolve_pipeline_tokens,
    make_table, print_table, trunc, flex_width, _fmt_deploy_dt,
    organization, project, folder_path, env_sort_key, parse_pipelines_env,
    print_subcommand_usage, SEMVER_RE,
)

log = logging.getLogger(__name__)

# =============================================================================
# Release pipeline helpers
# =============================================================================

def fetch_release_definitions(headers):
    base_url = (
        f"https://vsrm.dev.azure.com/{organization}/{project}/"
        f"_apis/release/definitions"
    )
    params = {"api-version": "7.1", "$top": 50}
    all_defs = []
    page = 0
    while True:
        resp = http_get(base_url, headers=headers, params=params)
        if resp.status_code != 200:
            log.error("Failed to list release definitions: %s - %s", resp.status_code, extract_error(resp.text))
            sys.exit(1)
        page_defs = resp.json().get("value", [])
        all_defs.extend(page_defs)
        page += 1
        continuation = resp.headers.get("x-ms-continuationtoken")
        if not continuation:
            break
        params = {"api-version": "7.1", "$top": 50, "continuationToken": continuation}
    log.debug("Fetched %d release definition(s) across %d page(s)", len(all_defs), page)
    return [d for d in all_defs if d.get("path", "").lower() == folder_path.lower()]

def fetch_pipeline_detail(headers, definition_id):
    url = (
        f"https://vsrm.dev.azure.com/{organization}/{project}/"
        f"_apis/release/definitions/{definition_id}?api-version=7.1"
    )
    resp = http_get(url, headers=headers)
    if resp.status_code != 200:
        return None
    return resp.json()

def _repo_from_detail(headers, detail):
    if detail is None:
        return "-"
    artifacts = detail.get("artifacts", [])
    if not artifacts:
        return "-"
    def_ref = artifacts[0].get("definitionReference", {})
    # Some artifact types expose the repo directly on the definitionReference
    repo = def_ref.get("repository", {}).get("name")
    if repo:
        return repo
    # Fall back: resolve via the linked build definition
    build_def_id = def_ref.get("definition", {}).get("id")
    if not build_def_id:
        return "-"
    build_url = (
        f"https://dev.azure.com/{organization}/{project}/"
        f"_apis/build/definitions/{build_def_id}?api-version=7.1-preview.7"
    )
    build_resp = http_get(build_url, headers=headers)
    if build_resp.status_code != 200:
        return "-"
    return build_resp.json().get("repository", {}).get("name", "-")

def fetch_pipeline_repo(headers, definition_id):
    detail = fetch_pipeline_detail(headers, definition_id)
    return _repo_from_detail(headers, detail)

def fetch_pipeline_stages(headers, definition_id):
    detail = fetch_pipeline_detail(headers, definition_id)
    if detail is None:
        return []
    return [e["name"] for e in detail.get("environments", []) if e.get("name")]

def fetch_recent_build_refs(headers, release_def_id, n=8):
    detail = fetch_pipeline_detail(headers, release_def_id)
    if detail is None:
        return "-"
    artifacts = detail.get("artifacts", [])
    if not artifacts:
        return "-"
    build_def_id = artifacts[0].get("definitionReference", {}).get("definition", {}).get("id")
    if not build_def_id:
        return "-"
    builds_url = (
        f"https://dev.azure.com/{organization}/{project}/"
        f"_apis/build/builds?definitions={build_def_id}&$top=30&api-version=7.1-preview.7"
    )
    resp = http_get(builds_url, headers=headers)
    if resp.status_code != 200:
        return "-"
    seen = {}
    for b in resp.json().get("value", []):
        src = b.get("sourceBranch", "")
        for prefix in ("refs/tags/", "refs/heads/"):
            if src.startswith(prefix):
                src = src[len(prefix):]
                break
        if src and src not in seen:
            seen[src] = None
        if len(seen) == n:
            break
    return ", ".join(seen) if seen else "-"

def fetch_pipeline_repo_and_refs(headers, definition_id, n=8):
    detail = fetch_pipeline_detail(headers, definition_id)
    repo = _repo_from_detail(headers, detail)
    if detail is None:
        return repo, "-"
    artifacts = detail.get("artifacts", [])
    if not artifacts:
        return repo, "-"
    build_def_id = artifacts[0].get("definitionReference", {}).get("definition", {}).get("id")
    if not build_def_id:
        return repo, "-"
    builds_url = (
        f"https://dev.azure.com/{organization}/{project}/"
        f"_apis/build/builds?definitions={build_def_id}&$top=30&api-version=7.1-preview.7"
    )
    resp = http_get(builds_url, headers=headers)
    if resp.status_code != 200:
        return repo, "-"
    seen = {}
    for b in resp.json().get("value", []):
        src = b.get("sourceBranch", "")
        for prefix in ("refs/tags/", "refs/heads/"):
            if src.startswith(prefix):
                src = src[len(prefix):]
                break
        if src and src not in seen:
            seen[src] = None
        if len(seen) == n:
            break
    recent_refs = ", ".join(seen) if seen else "-"
    return repo, recent_refs

def fetch_pipeline_repo_and_stages(headers, definition_id):
    detail = fetch_pipeline_detail(headers, definition_id)
    repo = _repo_from_detail(headers, detail)
    stages = [e["name"] for e in detail.get("environments", []) if e.get("name")] if detail is not None else []
    return repo, stages

def get_current_version(headers, definition_id, stage_name):
    """Return (version_str, deployed_at_str) for the last succeeded deployment."""
    info = get_current_release_info(headers, definition_id, stage_name)
    return info["version"], info["deployed_at"]

def get_current_release_info(headers, definition_id, stage_name):
    """Return dict with version_str, deployed_at_str, release_id, release_name for last succeeded deployment."""
    # Resolve the definition-level environment ID for the target stage
    def_url = (
        f"https://vsrm.dev.azure.com/{organization}/{project}/"
        f"_apis/release/definitions/{definition_id}?api-version=7.1"
    )
    def_resp = http_get(def_url, headers=headers)
    if def_resp.status_code != 200:
        return {"version": "error", "deployed_at": "-", "release_id": None, "release_name": "-"}
    env_id = next(
        (e.get("id") for e in def_resp.json().get("environments", [])
         if e.get("name", "").lower() == stage_name.lower()),
        None,
    )
    if not env_id:
        return {"version": "-", "deployed_at": "-", "release_id": None, "release_name": "-"}

    # Query the most recent succeeded deployment for that stage
    deploy_url = (
        f"https://vsrm.dev.azure.com/{organization}/{project}/"
        f"_apis/release/deployments"
        f"?definitionId={definition_id}"
        f"&definitionEnvironmentId={env_id}"
        f"&deploymentStatus=succeeded"
        f"&$top=1"
        f"&api-version=7.1"
    )
    deploy_resp = http_get(deploy_url, headers=headers)
    if deploy_resp.status_code != 200:
        return {"version": "error", "deployed_at": "-", "release_id": None, "release_name": "-"}
    deployments = deploy_resp.json().get("value", [])
    if not deployments:
        return {"version": "-", "deployed_at": "-", "release_id": None, "release_name": "-"}

    deployment = deployments[0]
    deployed_at = _fmt_deploy_dt(deployment.get("completedOn"))
    release_stub = deployment.get("release", {})
    release_id = release_stub.get("id")
    release_name = release_stub.get("name", "-")
    if not release_id:
        return {"version": release_name, "deployed_at": deployed_at, "release_id": None, "release_name": release_name}

    # Fetch the release to get the artifact/build reference
    rel_url = (
        f"https://vsrm.dev.azure.com/{organization}/{project}/"
        f"_apis/release/releases/{release_id}?api-version=7.1"
    )
    rel_resp = http_get(rel_url, headers=headers)
    if rel_resp.status_code != 200:
        return {"version": release_name, "deployed_at": deployed_at, "release_id": release_id, "release_name": release_name}
    artifacts = rel_resp.json().get("artifacts", [])
    if not artifacts:
        return {"version": release_name, "deployed_at": deployed_at, "release_id": release_id, "release_name": release_name}

    build_id = artifacts[0].get("definitionReference", {}).get("version", {}).get("id")
    if not build_id:
        return {"version": release_name, "deployed_at": deployed_at, "release_id": release_id, "release_name": release_name}

    # Resolve the build's sourceBranch to a tag or branch name
    build_url = (
        f"https://dev.azure.com/{organization}/{project}/"
        f"_apis/build/builds/{build_id}?api-version=7.1"
    )
    build_resp = http_get(build_url, headers=headers)
    if build_resp.status_code != 200:
        return {"version": release_name, "deployed_at": deployed_at, "release_id": release_id, "release_name": release_name}
    src_branch = build_resp.json().get("sourceBranch", "")
    for prefix in ("refs/tags/", "refs/heads/"):
        if src_branch.startswith(prefix):
            version = src_branch[len(prefix):]
            return {"version": version, "deployed_at": deployed_at, "release_id": release_id, "release_name": release_name}
    version = src_branch or release_name
    return {"version": version, "deployed_at": deployed_at, "release_id": release_id, "release_name": release_name}

def cmd_list_pipelines(args, headers):
    all_defs = fetch_release_definitions(headers)
    if not all_defs:
        log.warning("No release definitions found under folder '%s'.", folder_path)
        sys.exit(0)

    tokens = parse_pipelines_env()
    if not tokens:
        print_subcommand_usage("list_pipelines")
        log.error("Missing required environment variable: PIPELINES")
        sys.exit(1)

    folder_defs, _ = resolve_pipeline_tokens(
        headers, tokens, all_defs, fetch_pipeline_repo_and_stages, "release pipeline", get_repo=lambda v: v[0]
    )
    if not folder_defs:
        log.error("No pipelines matched PIPELINES='%s'", os.getenv("PIPELINES"))
        sys.exit(1)

    folder_defs.sort(key=lambda d: d["name"].lower())

    raw_envs = os.getenv("ENVIRONMENTS")
    environments_filter = []
    if raw_envs:
        seen_env: set = set()
        for e in (e.strip() for e in raw_envs.split(",") if e.strip()):
            if e.lower() not in seen_env:
                seen_env.add(e.lower())
                environments_filter.append(e)

    # Phase 1: repos + per-pipeline stage lists
    with ThreadPoolExecutor() as executor:
        if environments_filter:
            repo_futures = [executor.submit(fetch_pipeline_repo, headers, d["id"]) for d in folder_defs]
            repos = [f.result() for f in repo_futures]
            pipeline_stages = [environments_filter for _ in folder_defs]
        else:
            rs_futures = [executor.submit(fetch_pipeline_repo_and_stages, headers, d["id"]) for d in folder_defs]
            rs_results = [f.result() for f in rs_futures]
            repos = [r[0] for r in rs_results]
            pipeline_stages = [r[1] for r in rs_results]

    # Phase 2: current version for every pipeline+environment pair
    with ThreadPoolExecutor() as executor:
        version_futures = {
            (d["id"], env): executor.submit(get_current_release_info, headers, d["id"], env)
            for d, stages in zip(folder_defs, pipeline_stages) for env in stages
        }
        versions = {k: f.result() for k, f in version_futures.items()}

    repo_col_w = max((len(r) for r in repos), default=len("repository"))

    log.info("Found %d Release definition(s) under folder '%s':", len(folder_defs), folder_path)

    rows = []
    for d, repo, stages in zip(folder_defs, repos, pipeline_stages):
        for env in stages:
            info = versions.get((d["id"], env), {"version": "-", "deployed_at": "-", "release_name": "-"})
            version = info.get("version", "-")
            deployed_at = info.get("deployed_at", "-")
            release_name = info.get("release_name", "-")
            rows.append((d["id"], env, d["name"], repo, version, deployed_at, release_name))
    rows.sort(key=lambda r: (r[2].lower(), env_sort_key(r[1])))
    table = make_table("pipeline_id", "repository", "pipeline_name", "environment", "current_version", "deployed_at", "release_name")
    table.align["repository"] = "l"
    table.align["pipeline_name"] = "l"
    table.align["environment"] = "l"
    table.align["current_version"] = "l"
    table.align["deployed_at"] = "l"
    table.align["release_name"] = "l"
    table.min_width["repository"] = repo_col_w
    table.max_width["repository"] = repo_col_w
    for pid, env, pname, repo, version, deployed_at, release_name in rows:
        table.add_row([pid, repo, pname, env, version, deployed_at, release_name])

    print_table(table, args)
    sys.exit(0)

def cmd_list_environments(args, headers):
    folder_defs = fetch_release_definitions(headers)
    if not folder_defs:
        log.warning("No release definitions found under folder '%s'.", folder_path)
        sys.exit(0)

    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(fetch_pipeline_stages, headers, d["id"]) for d in folder_defs]
        all_env_lists = [f.result() for f in futures]

    env_counts = {}
    for envs in all_env_lists:
        for e in envs:
            env_counts[e] = env_counts.get(e, 0) + 1

    sorted_envs = sorted(env_counts.items(), key=lambda x: x[1], reverse=True)
    log.info("Found %d unique environment(s) across %d pipeline(s)", len(sorted_envs), len(folder_defs))

    table = make_table("environment", "pipeline_count")
    table.align["environment"] = "l"
    table.align["pipeline_count"] = "l"
    for env, count in sorted_envs:
        table.add_row([env, count])
    print_table(table, args)
    sys.exit(0)

def cmd_list_recent_builds(args, headers):
    count = int(os.getenv("COUNT", "8"))
    folder_defs = fetch_release_definitions(headers)
    if not folder_defs:
        log.warning("No release definitions found under folder '%s'.", folder_path)
        sys.exit(0)

    folder_defs.sort(key=lambda d: d["name"].lower())
    log.info("Found %d Release definition(s) under folder '%s':", len(folder_defs), folder_path)

    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(fetch_pipeline_repo_and_refs, headers, d["id"], count) for d in folder_defs]
        results = [f.result() for f in futures]

    repos = [r[0] for r in results]
    recent_refs = [r[1] for r in results]

    name_col_w = max((len(d["name"]) for d in folder_defs), default=len("pipeline_name"))
    repo_col_w = max((len(r) for r in repos), default=len("repository"))
    table = make_table("pipeline_id", "pipeline_name", "repository", "recent_tags_and_branches")
    table.align["pipeline_name"] = "l"
    table.align["repository"] = "l"
    table.align["recent_tags_and_branches"] = "l"
    table.min_width["pipeline_name"] = name_col_w
    table.max_width["pipeline_name"] = name_col_w
    table.min_width["repository"] = repo_col_w
    table.max_width["repository"] = repo_col_w
    for d, repo, refs in zip(folder_defs, repos, recent_refs):
        table.add_row([d["id"], d["name"], repo, refs])

    print_table(table, args)
    sys.exit(0)

# =============================================================================
# Deploy
# =============================================================================

def _extract_deploy_failure_reason(env, fallback):
    """Walk a release environment's deploySteps for the first task- or job-level issue message.
    Not restricted to tasks/jobs literally marked "failed" -- rejections, gate failures, and
    other non-succeeded outcomes can still carry a populated issues list. Returns the first
    message found in step/phase/job/task order, not whichever happens to be encountered last."""
    for step in env.get("deploySteps", []):
        for phase in step.get("releaseDeployPhases", []):
            for job in phase.get("deploymentJobs", []):
                for task in job.get("tasks", []):
                    status = (task.get("status") or "").lower()
                    issues = task.get("issues") or []
                    if status not in ("succeeded", "success") and issues:
                        return issues[0].get("message", fallback)
                job_meta = job.get("job") or {}
                job_status = (job_meta.get("status") or "").lower()
                job_issues = job_meta.get("issues") or []
                if job_status not in ("succeeded", "success") and job_issues:
                    return job_issues[0].get("message", fallback)
    return fallback


def poll_ado_environment(headers, rid, env_id, poll_interval=10, timeout=1800):
    """Block until an Azure DevOps release environment reaches a terminal state.
    Returns (status_str, reason_str_or_None), where status_str is Azure's raw environment
    status value (e.g. "succeeded", "failed", "rejected", "canceled") -- not normalized,
    matching how poll_ado_build returns the raw build result."""
    url = (
        f"https://vsrm.dev.azure.com/{organization}/{project}/"
        f"_apis/release/releases/{rid}/environments/{env_id}"
    )
    release_url = (
        f"https://dev.azure.com/{organization}/{requests.utils.quote(project, safe='')}/"
        f"_releaseProgress?releaseId={rid}&_a=release-pipeline-progress"
    )
    # Statuses that mean the environment is still working -- anything else (succeeded, or any
    # other value Azure DevOps reports, known or not) is treated as terminal, so the "on
    # failure" reason-extraction below applies to every non-succeeded terminal status rather
    # than only a fixed enumeration of known failure statuses.
    in_progress = {"notstarted", "inprogress", "queued", "scheduled"}
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        resp = http_get(url, headers=headers, params={"api-version": "7.1"})
        if resp.status_code == 200:
            env = resp.json()
            status = env.get("status", "").lower()
            if status not in in_progress:
                success = status == "succeeded"
                reason = None
                if not success:
                    reason = env.get("statusComment") or status
                    if not reason or reason == status:
                        reason = _extract_deploy_failure_reason(env, status)
                return status, reason
            log.info("Still waiting (%s, %ds elapsed)... %s", status, int(time.time() - started), release_url)
        else:
            log.error("Polling release %s environment %s: HTTP %s - %s", rid, env_id, resp.status_code, extract_error(resp.text))
        time.sleep(poll_interval)
    return "timed_out", "polling timed out"


def _deploy_preflight(rd, source_ref, branch, environments_list, headers):
    """Read-only: resolve the release built from source_ref and determine which of the
    requested environments actually exist in it. Returns
    (definition_id, definition_name, error_msg_or_None, release_info_or_None).

    - If no release can be resolved for source_ref (i.e. TAG_OR_BRANCH doesn't exist for this
      pipeline, or the API call itself fails), that's an ERROR: error_msg is set and
      release_info is None. Any pre-flight error aborts the whole deploy_pipelines run before
      any deployment is triggered for any pipeline.
    - If the release is resolved but is missing one or more of the requested environments,
      that's only a WARNING (logged here, non-fatal).
    - release_info["valid_environments"] holds the subset of requested environments that exist
      in the release -- that's what gets triggered, regardless of whatever is already deployed
      there.

    Issues no mutating requests."""
    definition_id = rd["id"]
    definition_name = rd["name"]

    log.debug("Processing pipeline: '%s' (ID: %s)", definition_name, definition_id)

    rels_params = {
        "definitionId": definition_id,
        "sourceBranchFilter": source_ref,
        "$top": 1,
        "$expand": "artifacts",
        "api-version": "7.1",
    }
    log.debug("%s: querying releases with params: %s", definition_name, rels_params)
    rels_resp = http_get(
        f"https://vsrm.dev.azure.com/{organization}/{project}/_apis/release/releases",
        headers=headers,
        params=rels_params,
    )
    log.debug("%s: releases response HTTP %s", definition_name, rels_resp.status_code)
    if rels_resp.status_code != 200:
        msg = f"Failed to query releases: HTTP {rels_resp.status_code}"
        log.error("%s: %s", definition_name, msg)
        return definition_id, definition_name, msg, None

    releases = rels_resp.json().get("value", [])
    if not releases:
        log.debug("%s: empty releases response body: %s", definition_name, rels_resp.text[:500])
        msg = f"No release found built from '{source_ref}'"
        log.error("%s: %s", definition_name, msg)
        return definition_id, definition_name, msg, None

    found_release = releases[0]
    rid = found_release["id"]
    rname = found_release["name"]
    log.debug("%s: found release %s (ID: %s), createdOn: %s", definition_name, rname, rid, found_release.get("createdOn", "-"))

    full_resp = http_get(
        f"https://vsrm.dev.azure.com/{organization}/{project}/_apis/release/releases/{rid}",
        headers=headers,
        params={"api-version": "7.1", "$expand": "environments"},
    )
    if full_resp.status_code != 200:
        msg = f"Failed to fetch release {rid}: HTTP {full_resp.status_code}"
        log.error("%s: %s", definition_name, msg)
        return definition_id, definition_name, msg, None

    full_release = full_resp.json()
    release_url = full_release.get("_links", {}).get("web", {}).get("href") or (
        f"https://dev.azure.com/{organization}/{requests.utils.quote(project, safe='')}/"
        f"_releaseProgress?releaseId={rid}&_a=release-pipeline-progress"
    )
    release_env_map = {e.get("name", "").lower(): e["id"] for e in full_release.get("environments", [])}

    missing_environments = [e for e in environments_list if e.lower() not in release_env_map]
    for e in missing_environments:
        log.warning("%s: environment/stage '%s' not present in this release, skipping", definition_name, e)

    valid_environments = [e for e in environments_list if e.lower() in release_env_map]

    release_info = {
        "rid": rid,
        "rname": rname,
        "release_url": release_url,
        "release_env_map": release_env_map,
        "valid_environments": valid_environments,
    }
    return definition_id, definition_name, None, release_info


def _deploy_trigger(definition_id, definition_name, release_info, branch, headers):
    """Mutating: PATCH each environment already confirmed to exist for this release
    (release_info["valid_environments"], computed during pre-flight) to 'inProgress'. Only
    called once every pipeline has already passed _deploy_preflight, so no deploy request
    goes out for any pipeline if any other pipeline failed its pre-flight check."""
    rid = release_info["rid"]
    rname = release_info["rname"]
    release_url = release_info["release_url"]
    release_env_map = release_info["release_env_map"]

    triggered = []
    errors = []

    for environment in release_info["valid_environments"]:
        env_id = release_env_map[environment.lower()]

        log.debug("%s: triggering environment '%s' (env ID: %s)", definition_name, environment, env_id)
        deploy_resp = http_patch(
            f"https://vsrm.dev.azure.com/{organization}/{project}/_apis/release/releases/{rid}/environments/{env_id}",
            headers=headers,
            params={"api-version": "7.1"},
            json={"status": "inProgress"},
        )
        if deploy_resp.status_code in (200, 202):
            log.info("%s / %s: queued successfully", definition_name, environment)
            triggered.append((definition_id, definition_name, branch, rname, environment, release_url, rid, env_id))
        else:
            msg = f"Failed to queue deployment: HTTP {deploy_resp.status_code} - {extract_error(deploy_resp.text)}"
            log.error("%s / %s: %s", definition_name, environment, msg)
            errors.append((definition_id, definition_name, msg))

    return triggered, errors


def cmd_deploy(args, headers):
    branch_or_tag = os.getenv("BRANCH_OR_TAG")
    raw_envs = os.getenv("ENVIRONMENTS")

    missing = [name for name, val in [("BRANCH_OR_TAG", branch_or_tag), ("ENVIRONMENTS", raw_envs)] if not val]
    if missing:
        print_subcommand_usage("deploy_pipelines")
        for name in missing:
            log.error("Missing required environment variable: %s", name)
        sys.exit(1)

    if SEMVER_RE.match(branch_or_tag):
        source_ref = f"refs/tags/{branch_or_tag}"
        log.debug("BRANCH_OR_TAG='%s' matches semver - treating as a tag", branch_or_tag)
    else:
        source_ref = f"refs/heads/{branch_or_tag}"
        log.debug("BRANCH_OR_TAG='%s' does not match semver - treating as a branch", branch_or_tag)

    # Parse and deduplicate requested environments (preserve order, case-insensitive dedup)
    seen_env = set()
    environments_list = []
    for e in (e.strip() for e in raw_envs.split(",") if e.strip()):
        if e.lower() not in seen_env:
            seen_env.add(e.lower())
            environments_list.append(e)

    all_defs = fetch_release_definitions(headers)

    if not all_defs:
        log.warning("No release definitions found under folder '%s'. Exiting.", folder_path)
        sys.exit(0)

    tokens = parse_pipelines_env()
    if not tokens:
        print_subcommand_usage("deploy_pipelines")
        log.error("Missing required environment variable: PIPELINES")
        sys.exit(1)

    # repo_stage_map: {definition_id: (repo, stages)} for every definition in all_defs, populated
    # only when resolve_pipeline_tokens needed to resolve non-numeric (repo-name) tokens; reused
    # below to avoid re-fetching stages for pipelines already resolved this way.
    folder_defs, repo_stage_map = resolve_pipeline_tokens(
        headers, tokens, all_defs, fetch_pipeline_repo_and_stages, "release pipeline", get_repo=lambda v: v[0]
    )
    if not folder_defs:
        log.error("No pipelines matched PIPELINES='%s'", os.getenv("PIPELINES"))
        sys.exit(1)

    # Validate requested environments against what's actually defined across all selected pipelines
    log.debug("Validating environment name(s) across %d pipeline(s)", len(folder_defs))
    pipeline_envs = {}
    missing_stage_defs = []
    for d in folder_defs:
        did = d["id"]
        if did in repo_stage_map:
            _, stages = repo_stage_map[did]
            pipeline_envs[did] = {e.lower(): e for e in stages}
        else:
            missing_stage_defs.append(d)
    if missing_stage_defs:
        with ThreadPoolExecutor() as executor:
            env_futures = {d["id"]: executor.submit(fetch_pipeline_stages, headers, d["id"]) for d in missing_stage_defs}
            for did, f in env_futures.items():
                pipeline_envs[did] = {e.lower(): e for e in f.result()}

    all_valid = {e for env_map in pipeline_envs.values() for e in env_map}
    invalid = [e for e in environments_list if e.lower() not in all_valid]
    if invalid:
        log.error("Invalid environment(s): %s. Valid environments across selected pipelines: %s",
                  invalid, sorted(all_valid))
        sys.exit(1)

    log.debug("Deploying %d pipeline(s) from '%s' (%s) to: %s", len(folder_defs), branch_or_tag, source_ref, environments_list)

    # Phase 1 - pre-flight only: resolve the release for every pipeline and check which of the
    # requested environments/stages actually exist in it. Purely read-only (no PATCH calls yet),
    # so nothing is triggered until every pipeline in the batch has been verified. A pipeline
    # missing an environment/stage only logs a warning and is skipped for that environment (see
    # _deploy_preflight); a pipeline with no release for TAG_OR_BRANCH is an error and aborts the
    # whole run before any pipeline is triggered. Every environment that does exist in the
    # release is (re)deployed regardless of what's already recorded as deployed there.
    with ThreadPoolExecutor() as executor:
        preflight_futures = [
            executor.submit(_deploy_preflight, rd, source_ref, branch_or_tag, environments_list, headers)
            for rd in folder_defs
        ]
        preflight_results = [f.result() for f in preflight_futures]

    preflight_errors = [(did, dname, msg) for did, dname, msg, _ in preflight_results if msg is not None]

    if preflight_errors:
        log.error("Aborting: %d pipeline(s) failed pre-flight checks - no deployments were triggered for any pipeline.",
                   len(preflight_errors))
        sys.exit(1)

    # Phase 2 - every pipeline passed pre-flight, so it's now safe to actually trigger deployments.
    triggered = []
    errors = []

    with ThreadPoolExecutor() as executor:
        trigger_futures = [
            executor.submit(_deploy_trigger, did, dname, release_info, branch_or_tag, headers)
            for did, dname, _msg, release_info in preflight_results
        ]
        for future in trigger_futures:
            t, e = future.result()
            triggered.extend(t)
            errors.extend(e)

    if errors:
        log.warning("%d deployment(s) failed to queue after pre-flight passed - see errors above.", len(errors))

    if not triggered:
        log.warning("No deployments were queued.")
        if errors:
            sys.exit(1)
        return

    for pid, pname, _bot, rname, environment, url, rid, env_id in triggered:
        log.info("%-20s %s", trunc(f"{environment}/{pname}", 20), url)

    log.info("Waiting for %d deployment(s) to complete...", len(triggered))

    def _poll_deploy(row):
        pid, pname, bot, rname, environment, url, rid, env_id = row
        status, reason = poll_ado_environment(headers, rid, env_id)
        return pid, pname, bot, rname, environment, url, status, reason or "-"

    with ThreadPoolExecutor() as executor:
        poll_results = list(executor.map(_poll_deploy, triggered))

    # Look up what's actually deployed to each environment right now (the last succeeded
    # deployment) rather than showing what we merely attempted -- a rejected/failed deployment
    # leaves the environment running whatever was already there, and the table should reflect
    # that reality rather than the branch/tag we asked for.
    def _lookup_active_version(row):
        pid, pname, _bot, rname, environment, url, status, reason = row
        active_version, _deployed_at = get_current_version(headers, pid, environment)
        return pid, pname, active_version, rname, environment, url, status, reason

    with ThreadPoolExecutor() as executor:
        poll_results = list(executor.map(_lookup_active_version, poll_results))

    poll_results.sort(key=lambda r: (r[4].lower(), r[1].lower()))
    id_w = 12
    env_w  = max((len(r[4]) for r in poll_results), default=len("environment"))
    bot_w  = max((len(r[2]) for r in poll_results), default=len("branch_or_tag"))
    rel_w  = max((len(r[3]) for r in poll_results), default=len("release"))
    name_w = flex_width(6, id_w, env_w, bot_w, rel_w, len("status"), len("reason"))
    table = make_table("pipeline_id", "environment", "pipeline_name", "branch_or_tag", "release", "status", "reason")
    table.align["environment"]   = "l"
    table.align["pipeline_name"] = "l"
    table.align["branch_or_tag"] = "l"
    table.align["release"]       = "l"
    table.align["status"]        = "l"
    table.align["reason"]        = "l"
    any_unsuccessful = bool(errors)
    for pid, pname, active_version, rname, environment, url, status, reason in poll_results:
        if status == "succeeded":
            log.info("%s / %s: %s", pname, environment, status)
        else:
            any_unsuccessful = True
            log.error("%s / %s: %s - %s", pname, environment, status, reason)
        table.add_row([pid, environment, trunc(pname, name_w), active_version, rname, status, reason])
    print_table(table, args)

    if any_unsuccessful:
        sys.exit(1)

# =============================================================================
# Add environment to pipelines
# =============================================================================

def cmd_add_environment_to_pipelines(args, headers):
    """Add a new environment to multiple release pipelines."""
    pipelines_str = os.getenv("PIPELINES", "")
    env_name = os.getenv("ENVIRONMENT", "")

    if not env_name:
        print_subcommand_usage("add_environment_to_pipelines")
        log.error("Missing required environment variable: ENVIRONMENT (name of the new environment/stage to add)")
        sys.exit(1)

    if not pipelines_str:
        print_subcommand_usage("add_environment_to_pipelines")
        log.error("Missing required environment variable: PIPELINES (comma-separated pipeline names)")
        sys.exit(1)

    pipelines = [p.strip() for p in pipelines_str.split(",")]
    log.info("Adding environment '%s' to %d pipelines", env_name, len(pipelines))

    success = 0
    failures = 0

    for pipeline_name in pipelines:
        try:
            # Find pipeline definition
            pipelines_data = fetch_release_definitions(headers)
            pipeline_def = next((p for p in pipelines_data if p.get("name") == pipeline_name), None)

            if not pipeline_def:
                log.error("Pipeline not found: %s", pipeline_name)
                failures += 1
                continue

            definition_id = pipeline_def["id"]
            log.info("Processing pipeline: %s (id=%d)", pipeline_name, definition_id)

            # Fetch full definition
            definition = fetch_pipeline_detail(headers, definition_id)
            if not definition:
                log.error("Failed to fetch definition for %s", pipeline_name)
                failures += 1
                continue

            # Check if environment already exists
            existing_envs = definition.get("environments", [])
            if any(e.get("name", "").lower() == env_name.lower() for e in existing_envs):
                log.info("Environment '%s' already exists in %s, skipping", env_name, pipeline_name)
                success += 1
                continue

            # Clone an existing environment (prefer Preview, fall back to first)
            template_env = next((e for e in existing_envs if e.get("name", "").lower() == "preview"), None)
            if not template_env and existing_envs:
                template_env = existing_envs[0]

            if not template_env:
                log.error("No existing environment to clone for %s", pipeline_name)
                failures += 1
                continue

            # Clone and modify the environment
            new_env = {k: v for k, v in template_env.items() if k not in ["id", "rank", "currentRelease"]}
            new_env["id"] = None  # Let Azure assign ID
            new_env["name"] = env_name
            new_env["rank"] = max([e.get("rank", 0) for e in existing_envs] or [0]) + 1

            # Null out nested IDs that will be auto-assigned
            if "deployStep" in new_env:
                new_env["deployStep"]["id"] = None
            if "preDeployApprovals" in new_env and "approvals" in new_env["preDeployApprovals"]:
                for approval in new_env["preDeployApprovals"]["approvals"]:
                    approval["id"] = None
            if "postDeployApprovals" in new_env and "approvals" in new_env["postDeployApprovals"]:
                for approval in new_env["postDeployApprovals"]["approvals"]:
                    approval["id"] = None

            # Append to environments
            definition["environments"].append(new_env)

            # PUT definition back (Azure DevOps release definitions require PUT, not PATCH)
            encoded_project = requests.utils.quote(project)
            url = f"https://vsrm.dev.azure.com/{organization}/{encoded_project}/_apis/release/definitions/{definition_id}?api-version=7.1"
            log.debug("PUT URL: %s", url)
            log.debug("Request body size: %d bytes", len(str(definition)))
            resp = http_put(url, headers=headers, json=definition)

            if resp.status_code != 200:
                log.error("HTTP %d: %s", resp.status_code, resp.text[:500])
                failures += 1
                continue

            log.info("Added environment '%s' to %s (rank %d)", env_name, pipeline_name, new_env["rank"])
            success += 1

        except Exception as e:
            log.error("Error processing %s: %s", pipeline_name, str(e))
            failures += 1

    print()
    print(f"{success} successful, {failures} failed")

    if failures > 0:
        sys.exit(1)

# =============================================================================
# Create releases from artifact
# =============================================================================

def _fetch_releases_for_definition(headers, encoded_project, definition_id):
    """Read-only: list every release for definition_id, following continuation tokens.
    Returns (releases, None) on success or (None, failing_response) on the first error."""
    url = f"https://vsrm.dev.azure.com/{organization}/{encoded_project}/_apis/release/releases"
    params = {"definitionId": definition_id, "$top": 50, "api-version": "7.1"}
    releases = []
    while True:
        resp = http_get(url, headers=headers, params=params)
        if resp.status_code != 200:
            return None, resp
        releases.extend(resp.json().get("value", []))
        continuation = resp.headers.get("x-ms-continuationtoken")
        if not continuation:
            return releases, None
        params = {"definitionId": definition_id, "$top": 50, "api-version": "7.1", "continuationToken": continuation}

def _find_release_artifact(headers, encoded_project, releases, source_release=None):
    """Read-only: find an artifact instance ID to reuse for a new release.

    If source_release is given, only releases whose name matches it (case-insensitively)
    are considered; otherwise every release is a candidate, in API order. Returns
    (artifact_id, matched_release_name) for the first candidate that actually has an
    artifact, or (None, None) if none do."""
    if source_release:
        candidates = [r for r in releases if r.get("name", "").lower() == source_release.lower()]
    else:
        candidates = releases

    for rel_summary in candidates:
        rel_id = rel_summary["id"]
        rel_name = rel_summary.get("name")
        url_full = f"https://vsrm.dev.azure.com/{organization}/{encoded_project}/_apis/release/releases/{rel_id}?api-version=7.1"
        resp_full = http_get(url_full, headers=headers)
        if resp_full.status_code != 200:
            continue
        for artifact in resp_full.json().get("artifacts", []):
            artifact_id = artifact.get("definitionReference", {}).get("version", {}).get("id")
            if artifact_id:
                log.debug("Found artifact ID %s in release %s", artifact_id, rel_name)
                return artifact_id, rel_name
    return None, None

def _create_release_preflight(headers, encoded_project, pipeline_name, pipeline_def, source_release):
    """Read-only: resolve everything needed to create a release for one pipeline -- the
    Build artifact alias from its definition, and an existing artifact instance to reuse
    (from source_release if given, otherwise the first release that has one). Returns
    (pipeline_name, definition_id, artifact_alias, artifact_id, source_release_name,
    error_msg_or_None). Issues no mutating requests."""
    definition_id = pipeline_def["id"]
    log.debug("Pre-flight: pipeline '%s' (id=%s)", pipeline_name, definition_id)

    def_url = f"https://vsrm.dev.azure.com/{organization}/{encoded_project}/_apis/release/definitions/{definition_id}?api-version=7.1"
    def_resp = http_get(def_url, headers=headers)
    if def_resp.status_code != 200:
        msg = f"Failed to fetch definition: HTTP {def_resp.status_code} - {extract_error(def_resp.text)}"
        return pipeline_name, definition_id, None, None, None, msg

    definition = def_resp.json()
    artifact_alias = next((a.get("alias") for a in definition.get("artifacts", []) if a.get("type") == "Build"), None)
    if not artifact_alias:
        return pipeline_name, definition_id, None, None, None, "No Build artifact found in pipeline definition"

    releases, err_resp = _fetch_releases_for_definition(headers, encoded_project, definition_id)
    if releases is None:
        msg = f"Failed to fetch releases: HTTP {err_resp.status_code} - {extract_error(err_resp.text)}"
        return pipeline_name, definition_id, artifact_alias, None, None, msg

    artifact_id, source_release_name = _find_release_artifact(headers, encoded_project, releases, source_release)
    if not artifact_id:
        if source_release:
            msg = f"Release '{source_release}' not found or has no artifacts"
        else:
            msg = "No releases found with a usable artifact"
        return pipeline_name, definition_id, artifact_alias, None, None, msg

    return pipeline_name, definition_id, artifact_alias, artifact_id, source_release_name, None

def cmd_create_releases_from_artifact(args, headers):
    """Create new releases for the given pipelines, reusing an existing build artifact
    (SOURCE_RELEASE if given, otherwise the first release found with one). Does not modify
    the pipeline's release definition itself."""
    pipelines_str = os.getenv("PIPELINES", "")
    source_release = os.getenv("SOURCE_RELEASE", "")

    if not pipelines_str:
        print_subcommand_usage("create_releases_from_artifact")
        log.error("Missing required environment variable: PIPELINES")
        sys.exit(1)

    pipelines = [p.strip() for p in pipelines_str.split(",")]
    if source_release:
        log.info("Creating new releases from source release '%s' across %d pipelines", source_release, len(pipelines))
    else:
        log.info("Creating new releases from first available artifact across %d pipelines", len(pipelines))

    encoded_project = requests.utils.quote(project)
    all_defs = fetch_release_definitions(headers)

    # Phase 1 -- pre-flight only: resolve the pipeline definition, artifact alias and artifact
    # to reuse for every requested pipeline. Purely read-only (no POST calls yet), so nothing is
    # created until every pipeline in the batch has been verified -- if any pipeline can't be
    # resolved, the whole run aborts before a release is created for any pipeline.
    preflight_results = []
    preflight_errors = []
    for pipeline_name in pipelines:
        pipeline_def = next((p for p in all_defs if p.get("name", "").lower() == pipeline_name.lower()), None)
        if not pipeline_def:
            preflight_errors.append((pipeline_name, "Pipeline not found"))
            continue
        result = _create_release_preflight(headers, encoded_project, pipeline_name, pipeline_def, source_release)
        preflight_results.append(result)
        if result[-1] is not None:
            preflight_errors.append((pipeline_name, result[-1]))

    for pipeline_name, msg in preflight_errors:
        log.error("%s: %s", pipeline_name, msg)

    if preflight_errors:
        log.error("Aborting: %d of %d pipeline(s) failed pre-flight checks - no releases were created for any pipeline.",
                   len(preflight_errors), len(pipelines))
        sys.exit(1)

    # Phase 2 -- every pipeline passed pre-flight, so it's now safe to actually create releases.
    success = 0
    failures = 0

    for pipeline_name, definition_id, artifact_alias, artifact_id, source_release_name, _msg in preflight_results:
        try:
            release_body = {
                "definitionId": definition_id,
                "description": f"Created from artifact of release '{source_release_name}' via create_releases_from_artifact",
                "artifacts": [{"alias": artifact_alias, "instanceReference": {"id": artifact_id}}],
                "isDraft": False,
                "reason": "manualUsingArtifacts"
            }

            url_create = f"https://vsrm.dev.azure.com/{organization}/{encoded_project}/_apis/release/releases?api-version=7.1"
            resp_create = http_post(url_create, headers=headers, json=release_body)

            if resp_create.status_code not in (200, 201):
                log.error("Failed to create release for %s: HTTP %d: %s", pipeline_name, resp_create.status_code, resp_create.text[:300])
                failures += 1
                continue

            new_release = resp_create.json()
            log.info("Created release %s (id=%d) for %s from artifact of release '%s'",
                      new_release.get("name"), new_release.get("id"), pipeline_name, source_release_name)
            success += 1

        except Exception as e:
            log.error("Error processing %s: %s", pipeline_name, str(e))
            failures += 1

    print()
    print(f"{success} successful, {failures} failed")

    if failures > 0:
        sys.exit(1)
