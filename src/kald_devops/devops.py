#!/usr/bin/env python3

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging
import os
import re
import shutil
import sys
import threading
import time
import requests
import base64
from prettytable import PrettyTable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stderr)
log = logging.getLogger(__name__)

def trunc(text, width):
    text = str(text) if text else ""
    return text if len(text) <= width else text[:width - 3] + "..."

def _term_width():
    return shutil.get_terminal_size((220, 50)).columns

def flex_width(num_cols, *fixed_widths):
    overhead = 3 * num_cols + 1
    return max(_term_width() - overhead - sum(fixed_widths), 10)

def make_table(*fields):
    t = PrettyTable(list(fields))
    t.max_table_width = _term_width()
    return t

def print_table(table, args):
    if getattr(args, "csv", False):
        print(table.get_csv_string())
    elif getattr(args, "json", False):
        print(table.get_json_string())
    else:
        print(table)

RETRYABLE_STATUS_CODES = {400, 429, 502, 503}
MAX_RETRY_SECONDS = 300  # give up retrying after 5 minutes and hand back the last (failing) response

def _with_retry(fn):
    def wrapper(*args, **kwargs):
        delay = 3
        attempt = 0
        started = time.time()
        while True:
            try:
                resp = fn(*args, **kwargs)
            except requests.exceptions.RequestException as exc:
                # Network-level failure (connection refused, DNS failure, timeout, etc.) --
                # there's no response/status_code to inspect here, so retry on a fixed schedule
                # the same way as a retryable HTTP status, and only give up (re-raising) once
                # MAX_RETRY_SECONDS has elapsed.
                attempt += 1
                elapsed = time.time() - started
                if elapsed >= MAX_RETRY_SECONDS:
                    log.error("Giving up after %d retries (%.0fs) on a network error calling %s: %s",
                              attempt, elapsed, args[0] if args else "", exc)
                    raise
                log.warning("Stuck retrying: network error calling %s (attempt %d, %.0fs elapsed): %s -- retrying in %ds...",
                            args[0] if args else "", attempt, elapsed, exc, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if resp.status_code not in RETRYABLE_STATUS_CODES:
                return resp
            attempt += 1
            elapsed = time.time() - started
            if elapsed >= MAX_RETRY_SECONDS:
                log.warning("Giving up after %d retries (%.0fs) on HTTP %d from %s -- returning the failed response instead of retrying further",
                            attempt, elapsed, resp.status_code, args[0] if args else "")
                return resp
            # Honor Retry-After on 429s (seconds, or an HTTP-date) when present; otherwise
            # fall back to the same exponential backoff used for the other retryable codes.
            wait = delay
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(int(retry_after), 1)
                    except ValueError:
                        pass
            log.warning("Stuck retrying: HTTP %d from %s (attempt %d, %.0fs elapsed), retrying in %ds...",
                        resp.status_code, args[0] if args else "", attempt, elapsed, wait)
            time.sleep(wait)
            delay = min(delay * 2, 60)
    return wrapper

http_get   = _with_retry(requests.get)
http_post  = _with_retry(requests.post)
http_patch = _with_retry(requests.patch)

# === CONFIGURATION ===
organization = "kalderos"
project = "Drug Discount Management"
folder_path = "\\phoenix"
# Release pipelines excluded from automatic triggering -- must be run manually
TOP_RELEASES = 30

# =============================================================================
# Shared utilities
# =============================================================================

def extract_error(text):
    match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return "HTTP error (see response for details)"

def make_headers(pat):
    auth_token = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {auth_token}",
        "Content-Type": "application/json"
    }

def parse_pipelines_env():
    raw = os.getenv("PIPELINES")
    if not raw:
        return None
    return [t.strip() for t in raw.split(",") if t.strip()]

def make_gh_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def make_slack_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

# =============================================================================
# Build pipeline helpers  (dev.azure.com build definitions)
# =============================================================================

def fetch_build_pipelines(headers):
    url = (
        f"https://dev.azure.com/{organization}/{project}/"
        f"_apis/build/definitions?api-version=7.1-preview.7"
    )
    response = http_get(url, headers=headers)
    if response.status_code != 200:
        log.error("Failed to fetch build pipelines: %s - %s", response.status_code, extract_error(response.text))
        sys.exit(1)
    return response.json().get("value", [])

def fetch_build_def_repo(headers, definition_id):
    url = (
        f"https://dev.azure.com/{organization}/{project}/"
        f"_apis/build/definitions/{definition_id}?api-version=7.1-preview.7"
    )
    resp = http_get(url, headers=headers)
    return resp.json().get("repository", {}).get("name", "-") if resp.status_code == 200 else "-"

def _build_preflight(definition, source_branch, headers, skip_existing_check=False):
    """Read-only: check whether a build artifact already exists for source_branch on this
    pipeline. Returns (definition_id, definition_name, error_msg_or_None). Issues no mutating
    requests. Pass skip_existing_check=True to bypass the existing build check (FORCE_REBUILD)."""
    definition_id = definition["id"]
    definition_name = definition["name"]

    if skip_existing_check:
        log.debug("%s: skipping existing build check (FORCE_REBUILD)", definition_name)
        return definition_id, definition_name, None

    url = (
        f"https://dev.azure.com/{organization}/{project}/"
        f"_apis/build/builds?api-version=7.1-preview.7"
    )
    params = {
        "definitions": definition_id,
        "branchName": source_branch,
        "$top": 1,
    }
    resp = http_get(url, headers=headers, params=params)
    if resp.status_code != 200:
        msg = f"Failed to check existing builds: HTTP {resp.status_code} - {extract_error(resp.text)}"
        log.error("%s: %s", definition_name, msg)
        return definition_id, definition_name, msg

    existing_builds = resp.json().get("value", [])
    if existing_builds:
        existing = existing_builds[0]
        build_id = existing.get("id")
        build_number = existing.get("buildNumber", str(build_id))
        msg = f"A build artifact already exists for '{source_branch}' (build {build_number}, ID {build_id}) — refusing to rebuild"
        log.error("%s: %s", definition_name, msg)
        return definition_id, definition_name, msg

    return definition_id, definition_name, None

def cmd_build(args, headers):
    branch_or_tag = os.getenv("BRANCH_OR_TAG", "main")
    force_rebuild = str(os.getenv("FORCE_REBUILD", "")).strip().lower() in ("1", "true", "yes")

    if SEMVER_RE.match(branch_or_tag):
        source_branch = f"refs/tags/{branch_or_tag}"
        log.debug("BRANCH_OR_TAG='%s' matches semver — treating as a tag", branch_or_tag)
    else:
        source_branch = f"refs/heads/{branch_or_tag}"
        log.debug("BRANCH_OR_TAG='%s' does not match semver — treating as a branch", branch_or_tag)
    log.debug("Building from: %s", source_branch)

    if force_rebuild:
        log.warning("FORCE_REBUILD is set -- bypassing the 'existing build' pre-flight check. "
                    "Every pipeline will be triggered regardless of whether a build already exists.")

    all_defs = fetch_build_pipelines(headers)

    tokens = parse_pipelines_env()
    if not tokens:
        print_subcommand_usage("build_pipelines")
        log.error("Missing required environment variable: PIPELINES")
        sys.exit(1)

    id_tokens = [t for t in tokens if t.isdigit()]
    repo_tokens = [t for t in tokens if not t.isdigit()]
    raw_defs = []

    for t in id_tokens:
        matches = [d for d in all_defs if str(d["id"]) == t]
        if matches:
            raw_defs.extend(matches)
        else:
            log.warning("No build pipeline found with ID %s", t)

    if repo_tokens:
        log.debug("Resolving build pipeline(s) by repository: %s", repo_tokens)
        with ThreadPoolExecutor() as executor:
            repo_futures = {d["id"]: executor.submit(fetch_build_def_repo, headers, d["id"]) for d in all_defs}
            repo_map = {did: f.result() for did, f in repo_futures.items()}
        for t in repo_tokens:
            matches = [d for d in all_defs if repo_map.get(d["id"], "").lower() == t.lower()]
            if matches:
                raw_defs.extend(matches)
            else:
                log.warning("No build pipeline found for repository '%s'", t)

    if not raw_defs:
        log.error("No build pipelines matched PIPELINES='%s'", os.getenv("PIPELINES"))
        sys.exit(1)

    seen_ids: set = set()
    folder_defs = [d for d in raw_defs if d["id"] not in seen_ids and not seen_ids.add(d["id"])]

    log.debug("Found %d pipeline(s) to build", len(folder_defs))

    # Pre-flight: verify none of the selected pipelines already have a build artifact for
    # source_branch. Purely read-only (no trigger requests yet), so nothing is built until every
    # pipeline in the batch has been verified — if any pipeline already has an artifact for this
    # tag/branch, the whole run aborts before triggering a build for any pipeline. Pass
    # skip_existing_check=True (FORCE_REBUILD) to bypass this check.
    with ThreadPoolExecutor() as executor:
        preflight_futures = [
            executor.submit(_build_preflight, definition, source_branch, headers, force_rebuild)
            for definition in folder_defs
        ]
        preflight_results = [f.result() for f in preflight_futures]

    preflight_errors = [(did, dname, msg) for did, dname, msg in preflight_results if msg is not None]

    if preflight_errors:
        log.error("Aborting: %d pipeline(s) already have a build artifact for '%s' — no builds were triggered for any pipeline.",
                   len(preflight_errors), source_branch)
        sys.exit(1)

    trigger_url = (
        f"https://dev.azure.com/{organization}/{project}/"
        f"_apis/build/builds?api-version=7.1-preview.7"
    )

    def _trigger_build(definition):
        pipeline_id = definition["id"]
        pipeline_name = definition["name"]
        payload = {
            "definition": {"id": pipeline_id},
            "sourceBranch": source_branch
        }
        response = http_post(trigger_url, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            build_id = data.get("id")
            build_url = (
                f"https://dev.azure.com/{organization}/"
                f"{requests.utils.quote(project, safe='')}/_build/results?buildId={build_id}"
            )
            log.debug("Triggered build %s for pipeline '%s' (ID: %s) from %s", build_id, pipeline_name, pipeline_id, source_branch)
            return (pipeline_id, pipeline_name, branch_or_tag, build_id, build_url)
        else:
            log.error("Failed to trigger pipeline '%s': %s - %s", pipeline_name, response.status_code, extract_error(response.text))
            return None

    log.info("Triggering %d build(s)...", len(folder_defs))

    triggered = []
    failure_count = 0

    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(_trigger_build, definition) for definition in folder_defs]
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                failure_count += 1
                continue
            pid, pname, bot, build_id, build_url = result
            log.info("%-20s %s", trunc(pname, 20), build_url)
            triggered.append(result)

    if failure_count:
        log.warning("%d build trigger(s) failed", failure_count)

    if not triggered:
        log.warning("No builds were triggered.")
        if failure_count:
            sys.exit(1)
        return

    log.info("Waiting for %d build(s) to complete...", len(triggered))

    def _poll_build(row):
        pid, pname, bot, build_id, build_url = row
        status, reason, duration = poll_ado_build(headers, build_id)
        return pid, pname, bot, build_url, duration, status, reason or "-"

    with ThreadPoolExecutor() as executor:
        poll_results = list(executor.map(_poll_build, triggered))

    poll_results.sort(key=lambda r: r[1].lower())
    id_w = 12
    bot_w = max((len(r[2]) for r in poll_results), default=len("branch_or_tag"))
    dur_w = max((len(r[4]) for r in poll_results), default=len("duration"))
    name_w = flex_width(5, id_w, bot_w, dur_w, len("status"), len("reason"))
    table = make_table("pipeline_id", "pipeline_name", "branch_or_tag", "duration", "status", "reason")
    table.align["pipeline_name"] = "l"
    table.align["branch_or_tag"] = "l"
    table.align["duration"]      = "l"
    table.align["status"]        = "l"
    table.align["reason"]        = "l"
    any_unsuccessful = failure_count > 0
    for pid, pname, bot, build_url, duration, status, reason in poll_results:
        if status == "succeeded":
            log.info("%s: %s", pname, status)
        else:
            any_unsuccessful = True
            log.error("%s: %s — %s", pname, status, reason)
        table.add_row([pid, trunc(pname, name_w), bot, duration, status, reason])
    print_table(table, args)

    if any_unsuccessful:
        sys.exit(1)

def cmd_list_build_pipelines(args, headers):
    all_defs = fetch_build_pipelines(headers)
    if not all_defs:
        log.warning("No build pipelines found.")
        sys.exit(0)

    raw_repos = os.getenv("REPOSITORIES")
    if not raw_repos:
        print_subcommand_usage("list_build_pipelines")
        log.error("Missing required environment variable: REPOSITORIES")
        sys.exit(1)

    tokens = [r.strip() for r in raw_repos.split(",") if r.strip()]

    id_tokens = [t for t in tokens if t.isdigit()]
    repo_tokens = [t for t in tokens if not t.isdigit()]
    raw_defs = []

    for t in id_tokens:
        matches = [d for d in all_defs if str(d["id"]) == t]
        if matches:
            raw_defs.extend(matches)
        else:
            log.warning("No build pipeline found with ID %s", t)

    if repo_tokens:
        log.debug("Resolving build pipeline(s) by repository: %s", repo_tokens)
        with ThreadPoolExecutor() as executor:
            repo_futures = {d["id"]: executor.submit(fetch_build_def_repo, headers, d["id"]) for d in all_defs}
            repo_map = {did: f.result() for did, f in repo_futures.items()}
        for t in repo_tokens:
            matches = [d for d in all_defs if repo_map.get(d["id"], "").lower() == t.lower()]
            if matches:
                raw_defs.extend(matches)
            else:
                log.warning("No build pipeline found for repository '%s'", t)

    if not raw_defs:
        log.error("No build pipelines matched REPOSITORIES='%s'", os.getenv("REPOSITORIES"))
        sys.exit(1)

    seen_ids = set()
    pipelines = [d for d in raw_defs if d["id"] not in seen_ids and not seen_ids.add(d["id"])]
    pipelines.sort(key=lambda d: d["name"].lower())

    # Get repository for each pipeline
    with ThreadPoolExecutor() as executor:
        repo_futures = {d["id"]: executor.submit(fetch_build_def_repo, headers, d["id"]) for d in pipelines}
        repos = {did: f.result() for did, f in repo_futures.items()}

    log.info("Found %d build pipeline(s) matching REPOSITORIES='%s':", len(pipelines), os.getenv("REPOSITORIES"))

    name_w = max((len(d["name"]) for d in pipelines), default=len("pipeline_name"))
    repo_w = max((len(repos.get(d["id"], "-")) for d in pipelines), default=len("repository"))
    table = make_table("pipeline_id", "pipeline_name", "repository")
    table.align["pipeline_name"] = "l"
    table.align["repository"] = "l"
    table.min_width["pipeline_name"] = name_w
    table.max_width["pipeline_name"] = name_w
    table.min_width["repository"] = repo_w
    table.max_width["repository"] = repo_w

    for d in pipelines:
        repo = repos.get(d["id"], "-")
        table.add_row([d["id"], d["name"], repo])

    print_table(table, args)
    sys.exit(0)

# =============================================================================
# Release pipeline helpers  (vsrm.dev.azure.com release definitions)
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

def resolve_release_definitions(headers, tokens):
    results = []
    for token in tokens:
        if token.isdigit():
            url = (
                f"https://vsrm.dev.azure.com/{organization}/{project}/"
                f"_apis/release/definitions/{token}?api-version=7.1"
            )
            resp = http_get(url, headers=headers)
            if resp.status_code == 404:
                log.warning("No release pipeline found with ID %s", token)
            elif resp.status_code != 200:
                log.error("Failed to fetch release pipeline %s: %s - %s", token, resp.status_code, extract_error(resp.text))
            else:
                d = resp.json()
                if d.get("path", "").lower() != folder_path.lower():
                    log.warning("Release pipeline ID %s is not in folder '%s'", token, folder_path)
                else:
                    results.append(d)
        else:
            url = (
                f"https://vsrm.dev.azure.com/{organization}/{project}/"
                f"_apis/release/definitions?searchText={requests.utils.quote(token)}&api-version=7.1"
            )
            resp = http_get(url, headers=headers)
            if resp.status_code != 200:
                log.error("Failed to search release pipeline '%s': %s - %s", token, resp.status_code, extract_error(resp.text))
                continue
            matches = [
                d for d in resp.json().get("value", [])
                if d.get("name", "").lower() == token.lower()
                and d.get("path", "").lower() == folder_path.lower()

            ]
            if matches:
                results.extend(matches)
            else:
                log.warning("No release pipeline found with name '%s'", token)
    return results

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

def _fmt_deploy_dt(raw):
    """Format an Azure DevOps ISO-8601 timestamp to 'YYYY-MM-DD HH:MM UTC'."""
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return raw

def get_current_version(headers, definition_id, stage_name):
    """Return (version_str, deployed_at_str) for the last succeeded deployment."""
    # Resolve the definition-level environment ID for the target stage
    def_url = (
        f"https://vsrm.dev.azure.com/{organization}/{project}/"
        f"_apis/release/definitions/{definition_id}?api-version=7.1"
    )
    def_resp = http_get(def_url, headers=headers)
    if def_resp.status_code != 200:
        return "error", "-"
    env_id = next(
        (e.get("id") for e in def_resp.json().get("environments", [])
         if e.get("name", "").lower() == stage_name.lower()),
        None,
    )
    if not env_id:
        return "-", "-"

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
        return "error", "-"
    deployments = deploy_resp.json().get("value", [])
    if not deployments:
        return "-", "-"

    deployment = deployments[0]
    deployed_at = _fmt_deploy_dt(deployment.get("completedOn"))
    release_stub = deployment.get("release", {})
    release_id = release_stub.get("id")
    release_name = release_stub.get("name", "-")
    if not release_id:
        return release_name, deployed_at

    # Fetch the release to get the artifact/build reference
    rel_url = (
        f"https://vsrm.dev.azure.com/{organization}/{project}/"
        f"_apis/release/releases/{release_id}?api-version=7.1"
    )
    rel_resp = http_get(rel_url, headers=headers)
    if rel_resp.status_code != 200:
        return release_name, deployed_at
    artifacts = rel_resp.json().get("artifacts", [])
    if not artifacts:
        return release_name, deployed_at

    build_id = artifacts[0].get("definitionReference", {}).get("version", {}).get("id")
    if not build_id:
        return release_name, deployed_at

    # Resolve the build's sourceBranch to a tag or branch name
    build_url = (
        f"https://dev.azure.com/{organization}/{project}/"
        f"_apis/build/builds/{build_id}?api-version=7.1"
    )
    build_resp = http_get(build_url, headers=headers)
    if build_resp.status_code != 200:
        return release_name, deployed_at
    src_branch = build_resp.json().get("sourceBranch", "")
    for prefix in ("refs/tags/", "refs/heads/"):
        if src_branch.startswith(prefix):
            return src_branch[len(prefix):], deployed_at
    return src_branch or release_name, deployed_at

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

    id_tokens = [t for t in tokens if t.isdigit()]
    repo_tokens = [t for t in tokens if not t.isdigit()]
    raw_defs = []

    for t in id_tokens:
        matches = [d for d in all_defs if str(d["id"]) == t]
        if matches:
            raw_defs.extend(matches)
        else:
            log.warning("No release pipeline found with ID %s", t)

    if repo_tokens:
        log.debug("Resolving pipeline(s) by repository: %s", repo_tokens)
        with ThreadPoolExecutor() as executor:
            detail_futures = {d["id"]: executor.submit(fetch_pipeline_repo_and_stages, headers, d["id"]) for d in all_defs}
            repo_map = {did: f.result()[0] for did, f in detail_futures.items()}
        for t in repo_tokens:
            matches = [d for d in all_defs if repo_map.get(d["id"], "").lower() == t.lower()]
            if matches:
                raw_defs.extend(matches)
            else:
                log.warning("No release pipeline found for repository '%s'", t)

    if not raw_defs:
        log.error("No pipelines matched PIPELINES='%s'", os.getenv("PIPELINES"))
        sys.exit(1)

    seen_ids: set = set()
    folder_defs = [d for d in raw_defs if d["id"] not in seen_ids and not seen_ids.add(d["id"])]

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
            (d["id"], env): executor.submit(get_current_version, headers, d["id"], env)
            for d, stages in zip(folder_defs, pipeline_stages) for env in stages
        }
        versions = {k: f.result() for k, f in version_futures.items()}

    repo_col_w = max((len(r) for r in repos), default=len("repository"))

    log.info("Found %d Release definition(s) under folder '%s':", len(folder_defs), folder_path)

    rows = []
    for d, repo, stages in zip(folder_defs, repos, pipeline_stages):
        for env in stages:
            version, deployed_at = versions.get((d["id"], env), ("-", "-"))
            rows.append((d["id"], env, d["name"], repo, version, deployed_at))
    rows.sort(key=lambda r: (r[2].lower(), env_sort_key(r[1])))
    table = make_table("pipeline_id", "repository", "pipeline_name", "environment", "current_version", "deployed_at")
    table.align["repository"] = "l"
    table.align["pipeline_name"] = "l"
    table.align["environment"] = "l"
    table.align["current_version"] = "l"
    table.align["deployed_at"] = "l"
    table.min_width["repository"] = repo_col_w
    table.max_width["repository"] = repo_col_w
    for pid, env, pname, repo, version, deployed_at in rows:
        table.add_row([pid, repo, pname, env, version, deployed_at])

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

def _deploy_preflight(rd, source_ref, branch, environments_list, headers, skip_already_deployed_check=False):
    """Read-only: resolve the release built from source_ref and determine which of the
    requested environments actually need a deployment triggered. Returns
    (definition_id, definition_name, error_msg_or_None, release_info_or_None).

    - If no release can be resolved for source_ref (i.e. TAG_OR_BRANCH doesn't exist for this
      pipeline, or the API call itself fails), that's an ERROR: error_msg is set and
      release_info is None. Any pre-flight error aborts the whole deploy_pipelines run before
      any deployment is triggered for any pipeline.
    - If the release is resolved but is missing one or more of the requested environments,
      that's only a WARNING (logged here, non-fatal).
    - If a requested environment already has this exact release successfully deployed to it,
      BRANCH_OR_TAG is already in the desired state there — that's also only a WARNING, and no
      redeploy is queued for that environment. This check compares against the newest EXISTING
      release object for source_ref, which can be stale if no fresh build/release has been
      created since source_ref last moved -- pass skip_already_deployed_check=True (wired to
      FORCE_REDEPLOY) to bypass it and always deploy to every environment that exists in the
      found release, regardless of its recorded status.
    - release_info["valid_environments"] holds the subset of requested environments that both
      exist in the release and (unless bypassed) are not already deployed — that's what gets
      triggered.

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
    release_env_status = {e.get("name", "").lower(): e.get("status", "") for e in full_release.get("environments", [])}

    missing_environments = [e for e in environments_list if e.lower() not in release_env_map]
    for e in missing_environments:
        log.warning("%s: environment/stage '%s' not present in this release, skipping", definition_name, e)

    existing_environments = [e for e in environments_list if e.lower() in release_env_map]

    if skip_already_deployed_check:
        already_deployed = []
    else:
        # BRANCH_OR_TAG represents the desired state: if this exact release has already succeeded
        # in an environment, that environment is already where it needs to be -- don't redeploy it.
        already_deployed = [e for e in existing_environments if release_env_status.get(e.lower(), "").lower() == "succeeded"]
    for e in already_deployed:
        log.warning("%s / %s: '%s' is already deployed to this environment, skipping", definition_name, e, branch)

    valid_environments = [e for e in existing_environments if e not in already_deployed]

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
    force_redeploy = str(os.getenv("FORCE_REDEPLOY", "")).strip().lower() in ("1", "true", "yes")

    missing = [name for name, val in [("BRANCH_OR_TAG", branch_or_tag), ("ENVIRONMENTS", raw_envs)] if not val]
    if missing:
        print_subcommand_usage("deploy_pipelines")
        for name in missing:
            log.error("Missing required environment variable: %s", name)
        sys.exit(1)

    if force_redeploy:
        log.warning("FORCE_REDEPLOY is set -- bypassing the 'already deployed' pre-flight check. "
                    "Every pipeline/environment where the release exists will be (re)triggered "
                    "regardless of its currently recorded status.")

    if SEMVER_RE.match(branch_or_tag):
        source_ref = f"refs/tags/{branch_or_tag}"
        log.debug("BRANCH_OR_TAG='%s' matches semver — treating as a tag", branch_or_tag)
    else:
        source_ref = f"refs/heads/{branch_or_tag}"
        log.debug("BRANCH_OR_TAG='%s' does not match semver — treating as a branch", branch_or_tag)

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

    repo_stage_map = {}  # {definition_id: (repo, stages)} populated when repo_tokens are resolved
    id_tokens = [t for t in tokens if t.isdigit()]
    repo_tokens = [t for t in tokens if not t.isdigit()]
    raw_defs = []

    for t in id_tokens:
        matches = [d for d in all_defs if str(d["id"]) == t]
        if matches:
            raw_defs.extend(matches)
        else:
            log.warning("No release pipeline found with ID %s", t)

    if repo_tokens:
        log.debug("Resolving pipeline(s) by repository: %s", repo_tokens)
        with ThreadPoolExecutor() as executor:
            detail_futures = {d["id"]: executor.submit(fetch_pipeline_repo_and_stages, headers, d["id"]) for d in all_defs}
            repo_stage_map = {did: f.result() for did, f in detail_futures.items()}
        repos = {did: rs[0] for did, rs in repo_stage_map.items()}
        for t in repo_tokens:
            matches = [d for d in all_defs if repos.get(d["id"], "").lower() == t.lower()]
            if matches:
                raw_defs.extend(matches)
            else:
                log.warning("No release pipeline found for repository '%s'", t)

    if not raw_defs:
        log.error("No pipelines matched PIPELINES='%s'", os.getenv("PIPELINES"))
        sys.exit(1)

    # Deduplicate pipelines by ID
    seen_ids = set()
    folder_defs = [d for d in raw_defs if d["id"] not in seen_ids and not seen_ids.add(d["id"])]

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

    # Phase 1 — pre-flight only: resolve the release for every pipeline and check which of the
    # requested environments/stages actually need a deployment. Purely read-only (no PATCH calls
    # yet), so nothing is triggered until every pipeline in the batch has been verified. A
    # pipeline missing an environment/stage, or one where BRANCH_OR_TAG is already deployed to an
    # environment, only logs a warning and is skipped for that environment (see
    # _deploy_preflight); a pipeline with no release for TAG_OR_BRANCH is an error and aborts the
    # whole run before any pipeline is triggered.
    with ThreadPoolExecutor() as executor:
        preflight_futures = [
            executor.submit(_deploy_preflight, rd, source_ref, branch_or_tag, environments_list, headers, force_redeploy)
            for rd in folder_defs
        ]
        preflight_results = [f.result() for f in preflight_futures]

    preflight_errors = [(did, dname, msg) for did, dname, msg, _ in preflight_results if msg is not None]

    if preflight_errors:
        log.error("Aborting: %d pipeline(s) failed pre-flight checks — no deployments were triggered for any pipeline.",
                   len(preflight_errors))
        sys.exit(1)

    # Phase 2 — every pipeline passed pre-flight, so it's now safe to actually trigger deployments.
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
        log.warning("%d deployment(s) failed to queue after pre-flight passed — see errors above.", len(errors))

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
            log.error("%s / %s: %s — %s", pname, environment, status, reason)
        table.add_row([pid, environment, trunc(pname, name_w), active_version, rname, status, reason])
    print_table(table, args)

    if any_unsuccessful:
        sys.exit(1)

# =============================================================================
# GitHub projects
# =============================================================================

GITHUB_ORG = "KalderosLLC"

def cmd_list_repositories(args):
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        print_subcommand_usage("list_repositories")
        log.error("Missing required environment variable: GITHUB_TOKEN")
        sys.exit(1)

    gh_headers = make_gh_headers(github_token)

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    repos = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/orgs/{GITHUB_ORG}/repos"
            f"?per_page=100&page={page}&type=all&sort=pushed&direction=desc"
        )
        resp = http_get(url, headers=gh_headers)
        if resp.status_code != 200:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            message = body.get("message", resp.text)
            log.error("Failed to fetch repositories: %s - %s", resp.status_code, message)
            if resp.status_code == 403 and "SAML" in message:
                log.error(
                    "The token has not been authorized for the '%s' organization. "
                    "Go to https://github.com/settings/tokens, find your token, "
                    "click 'Configure SSO', and authorize it for '%s'.",
                    GITHUB_ORG, GITHUB_ORG
                )
            sys.exit(1)
        page_repos = resp.json()
        if not page_repos:
            break
        for r in page_repos:
            pushed_at = r.get("pushed_at")
            if not pushed_at:
                continue
            if datetime.fromisoformat(pushed_at.replace("Z", "+00:00")) < cutoff:
                page_repos = []  # signal outer loop to stop
                break
            repos.append(r)
        if not page_repos:
            break
        page += 1

    repos.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
    log.info("Found %d repositories under %s active in the past 30 days", len(repos), GITHUB_ORG)

    repo_names = [f"{GITHUB_ORG}/{r['name']}" for r in repos]
    repo_w = max((len(n) for n in repo_names), default=len("repository"))
    vis_w = 12
    date_w = 10
    desc_w = 48
    table = make_table("last_modified", "repository", "visibility", "description")
    table.align["last_modified"] = "l"
    table.align["repository"] = "l"
    table.align["visibility"] = "l"
    table.align["description"] = "l"
    for r, repo_name in zip(repos, repo_names):
        pushed_at = r.get("pushed_at", "")
        last_modified = pushed_at[:10] if pushed_at else "-"
        table.add_row([
            last_modified,
            repo_name,
            trunc(r.get("visibility", "unknown"), vis_w),
            trunc(r.get("description") or "", desc_w),
        ])
    print_table(table, args)
    sys.exit(0)

# =============================================================================
# Tag
# =============================================================================

def _gh_resolve_branch(gh_headers, repo, branch):
    url = f"https://api.github.com/repos/{repo}/git/ref/heads/{branch}"
    resp = http_get(url, headers=gh_headers)
    if resp.status_code != 200:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        log.error("Failed to resolve branch '%s' in %s: %s - %s", branch, repo, resp.status_code, body.get("message", resp.text))
        return None
    return resp.json()["object"]["sha"]


def _gh_resolve_tag(gh_headers, repo, tag):
    """Resolve an existing tag to its commit SHA, dereferencing an annotated tag object
    if needed. Returns None (silently -- absence just means "not a tag") if it doesn't exist."""
    resp = http_get(f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}", headers=gh_headers)
    if resp.status_code != 200:
        return None
    obj = resp.json()["object"]
    if obj.get("type") != "tag":
        return obj["sha"]
    # Annotated tag -- the ref's "object" is the tag object itself, not the commit.
    deref = http_get(f"https://api.github.com/repos/{repo}/git/tags/{obj['sha']}", headers=gh_headers)
    if deref.status_code != 200:
        log.error("Failed to dereference annotated tag '%s' in %s: %s", tag, repo, deref.json().get("message"))
        return None
    return deref.json()["object"]["sha"]


def _gh_resolve_tag_or_branch(gh_headers, repo, name):
    """Resolve `name` in repo as a tag first, then as a branch. Returns (sha, kind)
    where kind is "tag" or "branch", or (None, None) if neither resolves (in which case
    the branch-lookup failure has already been logged by _gh_resolve_branch)."""
    tag_sha = _gh_resolve_tag(gh_headers, repo, name)
    if tag_sha:
        return tag_sha, "tag"
    branch_sha = _gh_resolve_branch(gh_headers, repo, name)
    if branch_sha:
        return branch_sha, "branch"
    return None, None


def _gh_create_tag(gh_headers, repo, tag, sha):
    """Create a tag ref; if it already exists, verify it points to the same commit and
    reuse it. Returns SHA or None on failure -- including when a pre-existing tag of the
    same name points somewhere else, which is refused rather than silently reused."""
    url = f"https://api.github.com/repos/{repo}/git/refs"
    resp = http_post(url, headers=gh_headers, json={"ref": f"refs/tags/{tag}", "sha": sha})
    if resp.status_code in (200, 201):
        return resp.json()["object"]["sha"]
    if resp.status_code == 422:
        # Tag already exists -- fetch its SHA via the same safe single-ref lookup used
        # elsewhere (the plural git/refs/:ref endpoint can return a partial-match array
        # instead of one ref, which git/ref/:ref never does).
        resp2 = http_get(f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}", headers=gh_headers)
        if resp2.status_code == 200:
            existing_sha = resp2.json()["object"]["sha"]
            if existing_sha == sha:
                log.info("Tag '%s' already exists in %s at %s — reusing", tag, repo, existing_sha)
                return existing_sha
            log.error(
                "Tag '%s' already exists in %s at %s, but this run resolved a different "
                "commit (%s) — refusing to silently reuse a mismatched tag.",
                tag, repo, existing_sha, sha,
            )
            return None
        log.error("Tag '%s' already exists in %s but could not fetch it: %s", tag, repo, resp2.json().get("message"))
        return None
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    log.error("Failed to create tag '%s' in %s: %s - %s", tag, repo, resp.status_code, body.get("message", resp.text))
    return None


def _gh_update_submodule(gh_headers, repo, branch, submodule_path, submodule_sha, tag):
    """Pin submodule_path in repo/branch to submodule_sha, fast-forward branch to the
    new commit, and return the new commit SHA."""
    head_sha = _gh_resolve_branch(gh_headers, repo, branch)
    if not head_sha:
        return None

    # Get the root tree SHA of the current HEAD commit
    resp = http_get(f"https://api.github.com/repos/{repo}/git/commits/{head_sha}", headers=gh_headers)
    if resp.status_code != 200:
        log.error("Failed to fetch commit %s in %s: %s", head_sha[:7], repo, resp.json().get("message"))
        return None
    root_tree_sha = resp.json()["tree"]["sha"]

    # Create a new tree from the existing root, overriding only the submodule entry.
    # GitHub resolves all intermediate subtrees automatically when base_tree is provided.
    resp = http_post(
        f"https://api.github.com/repos/{repo}/git/trees",
        headers=gh_headers,
        json={
            "base_tree": root_tree_sha,
            "tree": [{"path": submodule_path, "mode": "160000", "type": "commit", "sha": submodule_sha}],
        },
    )
    if resp.status_code not in (200, 201):
        log.error("Failed to create tree in %s: %s", repo, resp.json().get("message"))
        return None
    new_tree_sha = resp.json()["sha"]

    # Create a commit on the new tree
    resp = http_post(
        f"https://api.github.com/repos/{repo}/git/commits",
        headers=gh_headers,
        json={
            "message": f"chore: update phoenix-data-gateway submodule to {tag}",
            "tree": new_tree_sha,
            "parents": [head_sha],
        },
    )
    if resp.status_code not in (200, 201):
        log.error("Failed to create commit in %s: %s", repo, resp.json().get("message"))
        return None
    new_commit_sha = resp.json()["sha"]

    # Fast-forward the branch to the new commit so it actually reflects the submodule
    # bump, instead of leaving that commit reachable only through the tag we're about
    # to create on it. force=False so this only succeeds as a fast-forward -- if the
    # branch moved concurrently since head_sha was resolved, this fails loudly rather
    # than silently overwriting or orphaning someone else's commit.
    resp = http_patch(
        f"https://api.github.com/repos/{repo}/git/refs/heads/{branch}",
        headers=gh_headers,
        json={"sha": new_commit_sha, "force": False},
    )
    if resp.status_code not in (200, 201):
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        log.error(
            "Failed to fast-forward %s '%s' to %s: %s - %s",
            repo, branch, new_commit_sha[:7], resp.status_code, body.get("message", resp.text),
        )
        return None

    return new_commit_sha


def cmd_tag(args):
    name = os.getenv("TAG")
    branch = os.getenv("BRANCH", "main")
    pdg_tag_or_branch = os.getenv("PDG_TAG_OR_BRANCH", branch)
    github_token = os.getenv("GITHUB_TOKEN")
    raw_repos = os.getenv("REPOSITORIES")

    missing = [n for n, v in [("TAG", name), ("REPOSITORIES", raw_repos), ("GITHUB_TOKEN", github_token)] if v is None]
    if missing:
        print_subcommand_usage("tag_repository")
        for n in missing:
            log.error("Missing required environment variable: %s", n)
        sys.exit(1)

    repositories = [r.strip() for r in raw_repos.split(",") if r.strip()]
    if not repositories:
        log.error("REPOSITORIES is empty.")
        sys.exit(1)

    gh_headers = make_gh_headers(github_token)

    PHOENIX     = "KalderosLLC/phoenix"
    PHOENIX_PDG = "KalderosLLC/phoenix-data-gateway"

    tag_phoenix = PHOENIX in repositories

    # If phoenix is being tagged, phoenix-data-gateway is handled implicitly —
    # remove it from the list so it is not tagged a second time independently.
    remaining = [r for r in repositories if r != PHOENIX and not (r == PHOENIX_PDG and tag_phoenix)]

    if tag_phoenix:
        # 1. Tag phoenix-data-gateway -- PDG_TAG_OR_BRANCH (or BRANCH) is checked as an
        #    existing tag first, then as a branch, so an already-released PDG version can
        #    be pinned directly without needing a same-named branch to exist in PDG.
        pdg_branch_sha, resolved_kind = _gh_resolve_tag_or_branch(gh_headers, PHOENIX_PDG, pdg_tag_or_branch)
        if not pdg_branch_sha:
            sys.exit(1)
        log.info("Resolved %s '%s' (%s) -> %s", PHOENIX_PDG, pdg_tag_or_branch, resolved_kind, pdg_branch_sha)
        pdg_tag_sha = _gh_create_tag(gh_headers, PHOENIX_PDG, name, pdg_branch_sha)
        if not pdg_tag_sha:
            sys.exit(1)
        log.info("Tag '%s' on %s at %s", name, PHOENIX_PDG, pdg_tag_sha)

        # 2. Update submodule pointer in phoenix to the tagged PDG SHA, and fast-forward
        #    branch to that commit so it actually reflects the bump going forward.
        new_sha = _gh_update_submodule(
            gh_headers, PHOENIX, branch,
            "kalderos-edi-functions/phoenix-data-gateway",
            pdg_tag_sha, name,
        )
        if not new_sha:
            sys.exit(1)
        log.info(
            "Submodule pointer in %s updated to %s; branch '%s' fast-forwarded to %s",
            PHOENIX, pdg_tag_sha[:7], branch, new_sha[:7],
        )

        # 3. Tag phoenix at the new submodule-update commit
        if not _gh_create_tag(gh_headers, PHOENIX, name, new_sha):
            sys.exit(1)
        log.info("Tag '%s' created on %s/%s at %s", name, PHOENIX, branch, new_sha)

    for repository in remaining:
        sha = _gh_resolve_branch(gh_headers, repository, branch)
        if not sha:
            sys.exit(1)
        log.info("Resolved %s '%s' -> %s", repository, branch, sha)
        if not _gh_create_tag(gh_headers, repository, name, sha):
            sys.exit(1)
        log.info("Tag '%s' created on %s/%s at %s", name, repository, branch, sha)

# =============================================================================
# Git tickets
# =============================================================================

JIRA_PATTERN = re.compile(r'\b((?:CES|T340B)-\d+)\b')
SEMVER_RE = re.compile(r'^(v?)(\d+)\.(\d+)\.(\d+)(?:[.\-].+)?$')

ENV_PRECEDENCE = ["dev", "automated", "perftest", "load", "mt", "demo", "qa", "test", "stage", "preview", "prod"]

def env_sort_key(env_name):
    lower = env_name.lower()
    try:
        return ENV_PRECEDENCE.index(lower)
    except ValueError:
        return len(ENV_PRECEDENCE)

def parse_semver(tag):
    m = SEMVER_RE.match(tag)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))

def resolve_from_tag(gh_headers, repository, prefix, major, minor):
    target_minor = minor - 1
    tag_prefix = f"{prefix}{major}.{target_minor}."
    log.debug("resolve_from_tag: repository=%s prefix=%r major=%s minor=%s → searching for tags starting with %r",
              repository, prefix, major, minor, tag_prefix)
    matching = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/tags?per_page=100&page={page}"
        log.debug("GET %s", url)
        resp = http_get(url, headers=gh_headers)
        log.debug("Response: HTTP %s", resp.status_code)
        if resp.status_code != 200:
            is_json = resp.headers.get("content-type", "").startswith("application/json")
            body = resp.json() if is_json else {}
            log.debug("Response headers: %s", dict(resp.headers))
            log.debug("Response body: %s", resp.text[:500])
            return None, f"Failed to fetch tags: {resp.status_code} - {body.get('message', resp.text)}"
        tags = resp.json()
        log.debug("Page %d: received %d tag(s)", page, len(tags))
        if not tags:
            break
        for t in tags:
            name = t.get("name", "")
            if name.startswith(tag_prefix):
                patch_part = name[len(tag_prefix):]
                if patch_part.isdigit():
                    log.debug("Matched tag: %s", name)
                    matching.append((int(patch_part), name))
        page += 1
        if len(tags) < 100:
            break
    if not matching:
        return None, f"No tags found matching {prefix}{major}.{target_minor}.x in {repository}"
    matching.sort(reverse=True)
    log.debug("Best match: %s", matching[0][1])
    return matching[0][1], None

def fetch_compare_commits(gh_headers, repository, from_tag, tag):
    """Return (commits, error_message) for the full commit range from_tag...tag.
    GitHub's compare API caps the returned "commits" array at 250 entries even when
    total_commits is larger, so when truncated, fall back to paginating the commits
    list endpoint (starting at tag, walking back to the merge base) to recover the
    rest of the range."""
    url = f"https://api.github.com/repos/{repository}/compare/{from_tag}...{tag}"
    resp = http_get(url, headers=gh_headers)
    if resp.status_code != 200:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        return None, f"{resp.status_code} - {body.get('message', resp.text)}"
    data = resp.json()
    commits = data.get("commits", [])
    total_commits = data.get("total_commits", len(commits))
    if total_commits <= len(commits):
        return commits, None

    log.info(
        "Compare API truncated results (%d of %d commits) -- paginating the commits list to recover the rest",
        len(commits), total_commits,
    )
    merge_base_sha = data.get("merge_base_commit", {}).get("sha")
    commits = []
    page = 1
    while True:
        list_url = f"https://api.github.com/repos/{repository}/commits?sha={tag}&per_page=100&page={page}"
        resp = http_get(list_url, headers=gh_headers)
        if resp.status_code != 200:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            return None, f"{resp.status_code} - {body.get('message', resp.text)}"
        page_commits = resp.json()
        if not page_commits:
            break
        reached_base = False
        for commit in page_commits:
            if commit.get("sha") == merge_base_sha:
                reached_base = True
                break
            commits.append(commit)
        if reached_base or len(page_commits) < 100:
            break
        page += 1
    return commits, None

def cmd_git_tickets(args):
    repository = os.getenv("REPOSITORY")
    tag = os.getenv("TAG")
    from_tag = os.getenv("FROM_TAG")
    github_token = os.getenv("GITHUB_TOKEN")

    missing = [n for n, v in [("REPOSITORY", repository), ("TAG", tag), ("GITHUB_TOKEN", github_token)] if v is None]
    if missing:
        print_subcommand_usage("git_tickets")
        for n in missing:
            log.error("Missing required environment variable: %s", n)
        sys.exit(1)

    if "/" not in repository or repository.startswith("/") or repository.endswith("/"):
        log.error("REPOSITORY '%s' must be in the format organization/repo_name (e.g. KalderosLLC/phoenix)", repository)
        sys.exit(1)

    parsed_tag = parse_semver(tag)
    if not parsed_tag:
        log.error("TAG '%s' does not conform to semantic versioning (e.g. v1.21.0)", tag)
        sys.exit(1)
    prefix, major, minor, patch = parsed_tag
    log.debug("Parsed TAG '%s' → prefix=%r major=%s minor=%s patch=%s", tag, prefix, major, minor, patch)

    gh_headers = make_gh_headers(github_token)
    log.debug("GITHUB_TOKEN present: %s", bool(github_token))
    log.debug("Authorization header: %s", gh_headers.get("Authorization", "(none)")[:20] + "...")

    if from_tag:
        if not parse_semver(from_tag):
            log.error("FROM_TAG '%s' does not conform to semantic versioning (e.g. v1.20.0)", from_tag)
            sys.exit(1)
    else:
        if minor == 0:
            log.error(
                "Cannot auto-determine FROM_TAG: TAG '%s' has minor version 0. Set FROM_TAG explicitly.", tag
            )
            sys.exit(1)
        from_tag, err = resolve_from_tag(gh_headers, repository, prefix, major, minor)
        if err:
            log.error("Could not resolve FROM_TAG automatically: %s", err)
            sys.exit(1)
        log.info("FROM_TAG not set -- resolved to highest patch of previous minor: %s", from_tag)

    log.info("Range: %s..%s in %s", from_tag, tag, repository)

    commits, err = fetch_compare_commits(gh_headers, repository, from_tag, tag)
    if err:
        log.error("Failed to compare %s...%s: %s", from_tag, tag, err)
        sys.exit(1)

    ticket_authors = {}
    for commit in commits:
        if len(commit.get("parents", [])) < 2:
            continue
        message = commit.get("commit", {}).get("message", "")
        author = (
            commit.get("commit", {}).get("author", {}).get("name")
            or commit.get("author", {}).get("login", "-")
        )
        jira_ids = JIRA_PATTERN.findall(message)
        for jira_id in jira_ids:
            if jira_id not in ticket_authors:
                ticket_authors[jira_id] = []
            if author not in ticket_authors[jira_id]:
                ticket_authors[jira_id].append(author)

    if not ticket_authors:
        log.info("No merge commits found between %s and %s.", from_tag, tag)
        sys.exit(0)

    jira_email = os.getenv("JIRA_EMAIL")
    jira_token = os.getenv("JIRA_TOKEN")
    jira_base  = "https://kalderos.atlassian.net"

    def fetch_jira_summary(jira_id):
        if not jira_email or not jira_token:
            return "-"
        resp = requests.get(
            f"{jira_base}/rest/api/3/issue/{jira_id}",
            auth=(jira_email, jira_token),
            params={"fields": "summary"},
        )
        if resp.status_code == 200:
            return resp.json().get("fields", {}).get("summary", "-")
        log.debug("Could not fetch summary for %s: HTTP %s", jira_id, resp.status_code)
        return "-"

    rows = sorted(ticket_authors.items(), key=lambda r: r[0])
    with ThreadPoolExecutor() as executor:
        summaries = dict(zip(
            [jira_id for jira_id, _ in rows],
            executor.map(lambda r: fetch_jira_summary(r[0]), rows),
        ))

    jira_w    = max((len(r[0]) for r in rows), default=len("jira_id"))
    summary_w = 60
    authors_w = flex_width(3, jira_w, summary_w)
    table = make_table("jira_id", "summary", "authors")
    table.align["jira_id"]  = "l"
    table.align["summary"]  = "l"
    table.align["authors"]  = "l"
    for jira_id, authors in rows:
        table.add_row([jira_id, trunc(summaries[jira_id], summary_w), trunc(", ".join(authors), authors_w)])
    print_table(table, args)
    sys.exit(0)

# =============================================================================
# Release notes
# =============================================================================

def cmd_create_release_notes(args):
    repository = os.getenv("REPOSITORY")
    tag = os.getenv("TAG")
    github_token = os.getenv("GITHUB_TOKEN")

    missing = [n for n, v in [("REPOSITORY", repository), ("TAG", tag), ("GITHUB_TOKEN", github_token)] if v is None]
    if missing:
        print_subcommand_usage("create_release_notes")
        for n in missing:
            log.error("Missing required environment variable: %s", n)
        sys.exit(1)

    gh_headers = make_gh_headers(github_token)

    url = f"https://api.github.com/repos/{repository}/releases"
    payload = {
        "tag_name": tag,
        "name": tag,
        "generate_release_notes": True,
    }
    resp = http_post(url, headers=gh_headers, json=payload)
    if resp.status_code not in (200, 201):
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        log.error("Failed to create release: %s - %s", resp.status_code, body.get("message", resp.text))
        sys.exit(1)

    print(resp.json().get("html_url", "-"))
    sys.exit(0)

# =============================================================================
# GitHub Actions helpers
# =============================================================================

def fetch_triggered_run_url(gh_headers, workflow_file, triggered_at, retries=5, delay=2,
                            seen_urls=None, seen_lock=None, repo="KalderosLLC/phoenix"):
    """Return (html_url, run_id) for the newly-dispatched workflow run, or (None, None)."""
    runs_url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/"
        f"{workflow_file}/runs?per_page=20"
    )
    for _ in range(retries):
        time.sleep(delay)
        resp = http_get(runs_url, headers=gh_headers)
        if resp.status_code != 200:
            break
        for run in resp.json().get("workflow_runs", []):
            created_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
            if created_at < triggered_at:
                continue
            url = run["html_url"]
            run_id = run["id"]
            if seen_urls is None:
                return url, run_id
            with seen_lock:
                if url not in seen_urls:
                    seen_urls.add(url)
                    return url, run_id
    return None, None


def poll_gh_run(gh_headers, repo, run_id, poll_interval=10, timeout=1800):
    """Block until a GitHub Actions run completes. Returns (status_str, reason_str_or_None)."""
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        resp = http_get(url, headers=gh_headers)
        if resp.status_code == 200:
            run = resp.json()
            if run.get("status") == "completed":
                conclusion = run.get("conclusion") or "unknown"
                return conclusion, (None if conclusion == "success" else conclusion)
            log.info("Still waiting on %s run %s (%s, %ds elapsed)...", repo, run_id, run.get("status", "unknown"), int(time.time() - started))
        else:
            log.error("Polling %s/actions/runs/%s: HTTP %s - %s", repo, run_id, resp.status_code, extract_error(resp.text))
        time.sleep(poll_interval)
    return "timed_out", "polling timed out"


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
            log.info("Still waiting on release %s environment %s (%s, %ds elapsed)...", rid, env_id, status, int(time.time() - started))
        else:
            log.error("Polling release %s environment %s: HTTP %s - %s", rid, env_id, resp.status_code, extract_error(resp.text))
        time.sleep(poll_interval)
    return "timed_out", "polling timed out"


def _build_duration_str(build):
    """Format the elapsed time between an Azure DevOps build's startTime and finishTime."""
    start = build.get("startTime")
    finish = build.get("finishTime")
    if not start or not finish:
        return "-"
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        finish_dt = datetime.fromisoformat(finish.replace("Z", "+00:00"))
        return str(finish_dt - start_dt)
    except ValueError:
        return "-"


def poll_ado_build(headers, build_id, poll_interval=10, timeout=1800):
    """Block until an Azure DevOps build completes. Returns
    (status_str, reason_str_or_None, duration_str)."""
    url = f"https://dev.azure.com/{organization}/{project}/_apis/build/builds/{build_id}?api-version=7.1-preview.7"
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        resp = http_get(url, headers=headers)
        if resp.status_code == 200:
            build = resp.json()
            if build.get("status") == "completed":
                result = (build.get("result") or "unknown").lower()
                success = result == "succeeded"
                duration = _build_duration_str(build)
                reason = None
                if not success:
                    reason = result
                    # Try to find a more specific failure message from the build's timeline.
                    timeline_url = (
                        f"https://dev.azure.com/{organization}/{project}/"
                        f"_apis/build/builds/{build_id}/timeline?api-version=7.1-preview.7"
                    )
                    tl_resp = http_get(timeline_url, headers=headers)
                    if tl_resp.status_code == 200:
                        for record in tl_resp.json().get("records", []):
                            if (record.get("result") or "").lower() == "failed":
                                issues = record.get("issues", [])
                                if issues:
                                    reason = issues[0].get("message", result)
                                    break
                return result, reason, duration
            log.info("Still waiting on build %s (%s, %ds elapsed)...", build_id, build.get("status", "unknown"), int(time.time() - started))
        else:
            log.error("Polling build %s: HTTP %s - %s", build_id, resp.status_code, extract_error(resp.text))
        time.sleep(poll_interval)
    return "timed_out", "polling timed out", None

# =============================================================================
# Terraform
# =============================================================================

def _trigger_terraform_env(environment, pr, gh_headers, seen_urls, seen_lock):
    url = "https://api.github.com/repos/KalderosLLC/phoenix/actions/workflows/terraform-apply-eastus.yml/dispatches"
    triggered_at = datetime.now(timezone.utc)
    resp = http_post(url, headers=gh_headers, json={
        "ref": "main",
        "inputs": {"pr-number": pr, "environment": environment.lower()},
    })
    if resp.status_code not in (200, 204):
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        msg = f"HTTP {resp.status_code} - {body.get('message', resp.text)}"
        log.error("Failed to trigger terraform-apply-eastus for '%s': %s", environment, msg)
        return environment, None, None
    log.debug("Triggered terraform-apply-eastus for PR #%s in environment '%s'", pr, environment)
    run_url, run_id = fetch_triggered_run_url(gh_headers, "terraform-apply-eastus.yml", triggered_at,
                                              seen_urls=seen_urls, seen_lock=seen_lock)
    if not run_url:
        log.warning("Could not determine run URL for environment '%s'", environment)
    return environment, run_url, run_id


def fetch_workflow_runs(gh_headers, workflow_file, repo="KalderosLLC/phoenix", count=12):
    """Return the most recent `count` runs of a GitHub Actions workflow (newest first, GitHub's
    default order), or None if the fetch failed (error already logged)."""
    runs_url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs?per_page={count}"
    resp = http_get(runs_url, headers=gh_headers)
    if resp.status_code != 200:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        log.error("Failed to fetch %s runs: %s - %s", workflow_file, resp.status_code, body.get("message", resp.text))
        return None
    return resp.json().get("workflow_runs", [])


PR_MERGE_REF_RE = re.compile(r"refs/pull/(\d+)/merge")

def _pr_number_from_run(run):
    """Extract the PR number associated with a workflow run.

    GitHub is supposed to auto-attach a `pull_requests` array to pull_request-triggered runs,
    but in practice it comes back empty for runs that call a reusable workflow (as
    terraform-plan-eastus.yml does) -- observed directly against a real run payload. In that
    case, fall back to `referenced_workflows[].ref`, which encodes the PR merge ref
    (e.g. "refs/pull/5510/merge") the reusable workflow was invoked with.
    """
    prs = run.get("pull_requests") or []
    if prs and prs[0].get("number"):
        return str(prs[0]["number"])

    for ref_wf in run.get("referenced_workflows") or []:
        match = PR_MERGE_REF_RE.search(ref_wf.get("ref", ""))
        if match:
            return match.group(1)

    return None


def find_pr_with_successful_plan(runs):
    """Scan terraform-plan-eastus.yml runs (newest first) for the first one that completed
    successfully and has an attached PR -- that's the plan apply_terraform would actually be
    applying. Returns the PR number as a string, or None if no run qualifies."""
    for run in runs:
        if run.get("status") == "completed" and run.get("conclusion") == "success":
            pr = _pr_number_from_run(run)
            if pr:
                return pr
    return None


def resolve_pr_from_terraform_plan(gh_headers, repo="KalderosLLC/phoenix", workflow_file="terraform-plan-eastus.yml"):
    """Return (pr_number_or_None, runs_list). pr_number is the PR attached to the most recent
    successfully-completed run of terraform-plan-eastus.yml -- i.e. the PR whose plan is
    actually ready to be applied via apply_terraform. runs_list is the raw run data that was
    scanned (empty if the fetch itself failed). If pr_number is None, an error has already been
    logged."""
    runs = fetch_workflow_runs(gh_headers, workflow_file, repo)
    if runs is None:
        return None, []
    pr = find_pr_with_successful_plan(runs)
    if not pr:
        log.error("Could not find a successfully completed %s run with an attached PR", workflow_file)
    return pr, runs


def cmd_calc_pr(args):
    github_token = os.getenv("GITHUB_TOKEN")

    missing = [n for n, v in [("GITHUB_TOKEN", github_token)] if not v]
    if missing:
        print_subcommand_usage("calc_pr")
        for n in missing:
            log.error("Missing required environment variable: %s", n)
        sys.exit(1)

    gh_headers = make_gh_headers(github_token)

    pr, runs = resolve_pr_from_terraform_plan(gh_headers)

    # Show the terraform-plan-eastus.yml runs that were scanned (newest first) before announcing
    # the decision, so the user can visually confirm which run's PR was picked -- this is the
    # plan that apply_terraform would actually be applying, not just whatever last merged into
    # main (which may not have touched Terraform at all).
    if runs:
        picked_run_id = None
        for run in runs:
            if run.get("status") == "completed" and run.get("conclusion") == "success" and _pr_number_from_run(run):
                picked_run_id = run.get("id")
                break

        table = make_table("run", "pr", "status", "created_at", "branch", "picked")
        table.align["run"]        = "l"
        table.align["status"]     = "l"
        table.align["created_at"] = "l"
        table.align["branch"]     = "l"
        table.align["picked"]     = "l"
        for run in runs:
            run_url = run.get("html_url", "-")
            status_disp = run.get("conclusion") or run.get("status", "-")
            created_at = _fmt_deploy_dt(run.get("created_at"))
            branch = run.get("head_branch", "-")
            pr_number = _pr_number_from_run(run)
            pr_disp = f"#{pr_number}" if pr_number else "-"
            picked = "yes" if run.get("id") == picked_run_id else "-"
            table.add_row([run_url, pr_disp, status_disp, created_at, branch, picked])
        print_table(table, args)

    if not pr:
        sys.exit(1)

    log.info("Resolved PR #%s from the latest successful terraform-plan-eastus.yml run", pr)
    print(pr)
    sys.exit(0)


def cmd_apply_terraform(args):
    pr = os.getenv("PR")
    raw_envs = os.getenv("ENVIRONMENTS")
    github_token = os.getenv("GITHUB_TOKEN")

    missing = [n for n, v in [("PR", pr), ("ENVIRONMENTS", raw_envs), ("GITHUB_TOKEN", github_token)] if not v]
    if missing:
        print_subcommand_usage("apply_terraform")
        for n in missing:
            log.error("Missing required environment variable: %s", n)
        sys.exit(1)

    # Parse and deduplicate environments (preserve order, case-insensitive dedup)
    seen_env = set()
    environments_list = []
    for e in (e.strip() for e in raw_envs.split(",") if e.strip()):
        if e.lower() not in seen_env:
            seen_env.add(e.lower())
            environments_list.append(e)

    gh_headers = make_gh_headers(github_token)

    seen_urls: set = set()
    seen_lock = threading.Lock()
    with ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(_trigger_terraform_env, env, pr, gh_headers, seen_urls, seen_lock)
            for env in environments_list
        ]
        results = [f.result() for f in futures]

    triggered = [(env, url, run_id) for env, url, run_id in results if url]
    errors    = [env for env, url, run_id in results if url is None]

    for env, url, _ in triggered:
        log.info("%-20s %s", trunc(f"{env}/PR#{pr}", 20), url)
    if errors:
        log.warning("%d environment(s) failed to trigger: %s", len(errors), errors)
    if not triggered:
        sys.exit(1 if errors else 0)

    log.info("Waiting for %d run(s) to complete...", len(triggered))

    def _poll_terraform(item):
        env, url, run_id = item
        if run_id:
            status, reason = poll_gh_run(gh_headers, "KalderosLLC/phoenix", run_id)
        else:
            status, reason = "unknown", "run ID not captured"
        return env, url, status, reason or "-"

    with ThreadPoolExecutor() as executor:
        poll_results = list(executor.map(_poll_terraform, triggered))

    poll_results.sort(key=lambda r: r[0].lower())
    table = make_table("environment", "pr", "link", "status", "reason")
    table.align["environment"] = "l"
    table.align["link"]        = "l"
    table.align["status"]      = "l"
    table.align["reason"]      = "l"
    any_unsuccessful = bool(errors)
    for env, url, status, reason in poll_results:
        if status == "success":
            log.info("terraform / %s: %s", env, status)
        else:
            any_unsuccessful = True
            log.error("terraform / %s: %s — %s", env, status, reason)
        table.add_row([env, pr, url, status, reason])
    print_table(table, args)
    sys.exit(1 if any_unsuccessful else 0)

# =============================================================================
# Flyway
# =============================================================================

def _trigger_flyway_env(environment, branch_or_tag, gh_headers, seen_urls, seen_lock):
    repo = "KalderosLLC/phoenix-data-gateway"
    url = f"https://api.github.com/repos/{repo}/actions/workflows/flywayMigration.yml/dispatches"
    triggered_at = datetime.now(timezone.utc)
    resp = http_post(url, headers=gh_headers, json={"ref": branch_or_tag, "inputs": {"environment": environment.lower()}})
    if resp.status_code not in (200, 204):
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        msg = f"HTTP {resp.status_code} - {body.get('message', resp.text)}"
        log.error("Failed to trigger flywayMigration for '%s': %s", environment, msg)
        return environment, None, None
    log.debug("Triggered flywayMigration for environment '%s' from '%s'", environment, branch_or_tag)
    run_url, run_id = fetch_triggered_run_url(gh_headers, "flywayMigration.yml", triggered_at,
                                              seen_urls=seen_urls, seen_lock=seen_lock, repo=repo)
    if not run_url:
        log.warning("Could not determine run URL for environment '%s'", environment)
    return environment, run_url, run_id


def cmd_apply_flyway(args):
    raw_envs = os.getenv("ENVIRONMENTS")
    github_token = os.getenv("GITHUB_TOKEN")
    branch_or_tag = os.getenv("BRANCH_OR_TAG", "main")

    missing = [n for n, v in [("ENVIRONMENTS", raw_envs), ("GITHUB_TOKEN", github_token)] if not v]
    if missing:
        print_subcommand_usage("apply_flyway")
        for n in missing:
            log.error("Missing required environment variable: %s", n)
        sys.exit(1)

    # Parse and deduplicate environments (preserve order, case-insensitive dedup)
    seen_env = set()
    environments_list = []
    for e in (e.strip() for e in raw_envs.split(",") if e.strip()):
        if e.lower() not in seen_env:
            seen_env.add(e.lower())
            environments_list.append(e)

    gh_headers = make_gh_headers(github_token)

    # Verify the tag or branch exists in KalderosLLC/phoenix
    is_tag = bool(SEMVER_RE.match(branch_or_tag))
    if is_tag:
        ref_url = f"https://api.github.com/repos/KalderosLLC/phoenix/git/ref/tags/{requests.utils.quote(branch_or_tag, safe='')}"
    else:
        ref_url = f"https://api.github.com/repos/KalderosLLC/phoenix/branches/{requests.utils.quote(branch_or_tag, safe='')}"
    ref_resp = http_get(ref_url, headers=gh_headers)
    if ref_resp.status_code != 200:
        ref_type = "tag" if is_tag else "branch"
        log.error("Could not find %s '%s' in KalderosLLC/phoenix: HTTP %s", ref_type, branch_or_tag, ref_resp.status_code)
        sys.exit(1)
    log.debug("Verified %s '%s' exists in KalderosLLC/phoenix", "tag" if is_tag else "branch", branch_or_tag)

    seen_urls: set = set()
    seen_lock = threading.Lock()
    with ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(_trigger_flyway_env, env, branch_or_tag, gh_headers, seen_urls, seen_lock)
            for env in environments_list
        ]
        results = [f.result() for f in futures]

    triggered = [(env, url, run_id) for env, url, run_id in results if url]
    errors    = [env for env, url, run_id in results if url is None]

    for env, url, _ in triggered:
        log.info("%-20s %s", trunc(f"{env}/{branch_or_tag}", 20), url)
    if errors:
        log.warning("%d environment(s) failed to trigger: %s", len(errors), errors)
    if not triggered:
        sys.exit(1 if errors else 0)

    log.info("Waiting for %d run(s) to complete...", len(triggered))

    def _poll_flyway(item):
        env, url, run_id = item
        if run_id:
            status, reason = poll_gh_run(gh_headers, "KalderosLLC/phoenix-data-gateway", run_id)
        else:
            status, reason = "unknown", "run ID not captured"
        return env, url, status, reason or "-"

    with ThreadPoolExecutor() as executor:
        poll_results = list(executor.map(_poll_flyway, triggered))

    poll_results.sort(key=lambda r: r[0].lower())
    table = make_table("environment", "branch_or_tag", "link", "status", "reason")
    table.align["environment"]   = "l"
    table.align["branch_or_tag"] = "l"
    table.align["link"]          = "l"
    table.align["status"]        = "l"
    table.align["reason"]        = "l"
    any_unsuccessful = bool(errors)
    for env, url, status, reason in poll_results:
        if status == "success":
            log.info("flyway / %s: %s", env, status)
        else:
            any_unsuccessful = True
            log.error("flyway / %s: %s — %s", env, status, reason)
        table.add_row([env, branch_or_tag, url, status, reason])
    print_table(table, args)
    sys.exit(1 if any_unsuccessful else 0)

# =============================================================================
# Help
# =============================================================================

SUBCOMMAND_ENV_VARS = [
    ("list_repositories",     "GITHUB_TOKEN",          "required", "GitHub personal access token with read:org and repo scopes"),
    ("tag_repository",        "GITHUB_TOKEN",          "required", "GitHub personal access token with repo scope"),
    ("tag_repository",        "TAG",                   "required", "Git tag name to create (e.g. v1.21.0-rc)"),
    ("tag_repository",        "REPOSITORIES",          "required", "Comma-separated list of full GitHub repository paths to tag (e.g. KalderosLLC/phoenix,KalderosLLC/phoenix-snowflake-gateway); when KalderosLLC/phoenix is included, phoenix-data-gateway is tagged implicitly and the submodule pointer is updated before tagging phoenix — do not also list phoenix-data-gateway separately"),
    ("tag_repository",        "BRANCH",                "optional", "Source branch to tag (default: main, e.g. hotfix/v1.19.0); applies to all repositories"),
    ("tag_repository",        "PDG_TAG_OR_BRANCH",     "optional", "Tag or branch to pin phoenix-data-gateway to when KalderosLLC/phoenix is tagged implicitly (default: BRANCH); checked as an existing tag first, then as a branch, and errors out if neither exists in phoenix-data-gateway"),
    ("create_release_notes",  "GITHUB_TOKEN",          "required", "GitHub personal access token with repo scope"),
    ("create_release_notes",  "REPOSITORY",            "required", "Full GitHub repository path (e.g. KalderosLLC/phoenix)"),
    ("create_release_notes",  "TAG",                   "required", "Tag name to create the release for"),
    ("git_tickets",           "GITHUB_TOKEN",          "required", "GitHub personal access token with repo scope"),
    ("git_tickets",           "REPOSITORY",            "required", "Full GitHub repository path (e.g. KalderosLLC/phoenix)"),
    ("git_tickets",           "TAG",                   "required", "Head tag or branch to inspect"),
    ("git_tickets",           "FROM_TAG",              "optional", "Base tag or branch (defaults to last 100 commits of TAG)"),
    ("git_tickets",           "JIRA_EMAIL",            "optional", "Atlassian account email for fetching ticket summaries"),
    ("git_tickets",           "JIRA_TOKEN",            "optional", "Atlassian API token for fetching ticket summaries (omit to skip summary lookup)"),
    ("list_environments",     "AZURE_DEVOPS_EXT_PAT",  "required", "Azure DevOps personal access token"),
    ("list_recent_builds",   "AZURE_DEVOPS_EXT_PAT",  "required", "Azure DevOps personal access token"),
    ("list_recent_builds",   "COUNT",                  "optional", "Number of recent builds to show per pipeline (default: 8)"),
    ("list_build_pipelines",  "AZURE_DEVOPS_EXT_PAT",  "required", "Azure DevOps personal access token"),
    ("list_build_pipelines",  "REPOSITORIES",          "required", "Comma-delimited pipeline IDs or repository paths (e.g. KalderosLLC/phoenix); filters to pipelines with a matching repository path"),
    ("list_pipelines",        "AZURE_DEVOPS_EXT_PAT",  "required", "Azure DevOps personal access token"),
    ("list_pipelines",        "PIPELINES",             "required", "Comma-delimited pipeline IDs or repository paths (e.g. KalderosLLC/phoenix); filters to pipelines with a matching repository path"),
    ("list_pipelines",        "ENVIRONMENTS",          "optional", "Comma-delimited environments; adds environment and current_version columns, sorted by pipeline then environment"),
    ("build_pipelines",       "AZURE_DEVOPS_EXT_PAT",  "required", "Azure DevOps personal access token"),
    ("build_pipelines",       "BRANCH_OR_TAG",         "optional", "Semver tag (e.g. v1.21.0) or branch name (e.g. main) to build from (default: main)"),
    ("build_pipelines",       "PIPELINES",             "required", "Comma-delimited pipeline IDs or repository paths (e.g. KalderosLLC/phoenix); filters to pipelines with a matching repository path"),
    ("build_pipelines",       "FORCE_REBUILD",         "optional", "Set to 1/true to bypass the 'existing build' pre-flight check and force a rebuild even if one already exists"),
    ("calc_pr",                "GITHUB_TOKEN",          "required", "GitHub personal access token with repo scope"),
    ("apply_terraform",       "GITHUB_TOKEN",          "required", "GitHub personal access token with workflow scope"),
    ("apply_terraform",       "ENVIRONMENTS",          "required", "Comma-delimited list of target environments (e.g. Stage,Prod)"),
    ("apply_terraform",       "PR",                    "required", "Pull request number (use calc_pr to find the PR with the latest successful terraform-plan-eastus.yml run)"),
    ("apply_flyway",          "GITHUB_TOKEN",          "required", "GitHub personal access token with workflow scope"),
    ("apply_flyway",          "ENVIRONMENTS",          "required", "Comma-delimited list of target environments (e.g. Stage,Prod)"),
    ("apply_flyway",          "BRANCH_OR_TAG",         "optional", "Semver tag (e.g. v1.21.0) or branch name (e.g. main); verified before dispatch (default: main)"),
    ("deploy_pipelines",      "AZURE_DEVOPS_EXT_PAT",  "required", "Azure DevOps personal access token"),
    ("deploy_pipelines",      "BRANCH_OR_TAG",         "required", "Semver tag (e.g. v1.21.0) or branch name (e.g. main) to deploy from"),
    ("deploy_pipelines",      "FORCE_REDEPLOY",        "optional", "Set to 1/true to bypass the 'already deployed' pre-flight check (TEMPORARY -- see known issue re: stale release detection for branches)"),
    ("deploy_pipelines",      "ENVIRONMENTS",          "required", "Comma-delimited list of target environments (e.g. Stage,Prod)"),
    ("deploy_pipelines",      "PIPELINES",             "required", "Comma-delimited pipeline IDs or repository paths (e.g. KalderosLLC/phoenix); filters to pipelines with a matching repository path"),
    ("slack_find_channel",    "SLACK_BOT_TOKEN",        "required", "Slack bot OAuth token (xoxb-...) with channels:read scope (add groups:read for private channels)"),
    ("slack_find_channel",    "NAME",                  "required", "Channel name to resolve to an ID (e.g. releases or #releases)"),
    ("slack_release",         "ENVIRONMENTS",          "required", "Comma-delimited list of target environments (e.g. Stage,Prod)"),
    ("slack_release",         "BRANCH_OR_TAG",         "required", "Semver tag (e.g. v1.21.0) or branch name (e.g. main)"),
    ("slack_release",         "SLACK_BOT_TOKEN",        "optional", "Slack bot OAuth token (xoxb-...) with chat:write scope; must be set together with SLACK_CHANNEL to post"),
    ("slack_release",         "SLACK_CHANNEL",         "optional", "Slack channel ID or name (e.g. C1234567890 or #releases); must be set together with SLACK_BOT_TOKEN to post"),
]

_SUBCOMMAND_DESCS = [
    ("usage",                "Show this usage information"),
    ("list_repositories",    "List all repositories in the KalderosLLC GitHub org"),
    ("tag_repository",       "Create and push a git tag to one or more repositories; when KalderosLLC/phoenix is listed, also tags phoenix-data-gateway and updates the submodule pointer"),
    ("create_release_notes", "Create a GitHub release with auto-generated notes"),
    ("git_tickets",          "List Jira tickets from merge commits between two refs"),
    ("list_build_pipelines", "List Azure DevOps build pipelines with repository"),
    ("list_environments",    "List all unique environments across release pipelines"),
    ("list_recent_builds",  "List all release pipelines with recent tags and branches"),
    ("list_pipelines",       "List release definitions with repository and recent refs"),
    ("build_pipelines",      "Trigger Azure DevOps build pipelines"),
    ("calc_pr",               "Print the PR with the latest successful terraform-plan-eastus.yml run"),
    ("apply_terraform",      "Trigger the terraform-apply-eastus workflow"),
    ("apply_flyway",         "Trigger the flywayMigration workflow"),
    ("deploy_pipelines",     "Trigger Azure DevOps release pipeline deployments"),
    ("slack_find_channel",   "Resolve a Slack channel name to its ID (SLACK_BOT_TOKEN, NAME)"),
    ("slack_release",        "Prepare a release notification message, and post it to Slack if SLACK_BOT_TOKEN/SLACK_CHANNEL are set"),
]

def fetch_slack_channels(slack_bot_token, types="public_channel,private_channel"):
    """Return all Slack channels visible to the bot (paginated via cursor), or None if the
    fetch failed (error already logged)."""
    headers = make_slack_headers(slack_bot_token)
    channels = []
    cursor = None
    while True:
        params = {"types": types, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = http_get("https://slack.com/api/conversations.list", headers=headers, params=params)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or not body.get("ok"):
            log.error("Failed to list Slack channels: %s - %s", resp.status_code, body.get("error", resp.text))
            return None
        channels.extend(body.get("channels", []))
        cursor = body.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels


def cmd_slack_find_channel(args):
    slack_bot_token = os.getenv("SLACK_BOT_TOKEN")
    name = os.getenv("NAME")

    missing = [n for n, v in [("SLACK_BOT_TOKEN", slack_bot_token), ("NAME", name)] if not v]
    if missing:
        print_subcommand_usage("slack_find_channel")
        for n in missing:
            log.error("Missing required environment variable: %s", n)
        sys.exit(1)

    target = name.lstrip("#").lower()

    channels = fetch_slack_channels(slack_bot_token)
    if channels is None:
        sys.exit(1)

    matches = [c for c in channels if c.get("name", "").lower() == target]
    if not matches:
        # Suggest close matches (substring either direction) to help catch typos, since a bot
        # can only see channels it's a member of (or all public ones, depending on scope).
        suggestions = [c["name"] for c in channels if target in c.get("name", "").lower() or c.get("name", "").lower() in target]
        log.error("No Slack channel named '%s' found (searched %d channel(s) visible to the bot).", name, len(channels))
        if suggestions:
            log.error("Did you mean: %s", ", ".join(sorted(suggestions)[:10]))
        sys.exit(1)

    channel = matches[0]
    log.info("Resolved #%s -> %s", channel.get("name"), channel["id"])
    print(channel["id"])
    sys.exit(0)


def cmd_slack_release(args):
    environments = os.getenv("ENVIRONMENTS")
    branch_or_tag = os.getenv("BRANCH_OR_TAG")
    slack_bot_token = os.getenv("SLACK_BOT_TOKEN")
    slack_channel = os.getenv("SLACK_CHANNEL")

    missing = [n for n, v in [("ENVIRONMENTS", environments), ("BRANCH_OR_TAG", branch_or_tag)] if not v]
    if missing:
        print_subcommand_usage("slack_release")
        for n in missing:
            log.error("Missing required environment variable: %s", n)
        sys.exit(1)

    if bool(slack_bot_token) != bool(slack_channel):
        print_subcommand_usage("slack_release")
        log.error("SLACK_BOT_TOKEN and SLACK_CHANNEL must both be set to post to Slack")
        sys.exit(1)

    log.debug("ENVIRONMENTS:  %s", environments)
    log.debug("BRANCH_OR_TAG: %s", branch_or_tag)
    log.debug("SLACK_CHANNEL: %s", slack_channel)

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%A, %B %-d, %Y - %H:%M %Z")
    is_tag = bool(SEMVER_RE.match(branch_or_tag))
    release_notes_url = (
        f"https://github.com/KalderosLLC/phoenix/releases/tag/{branch_or_tag}"
        if is_tag else
        f"https://github.com/KalderosLLC/phoenix/tree/{branch_or_tag}"
    )

    message = "\n".join([
        "Truzo Release",
        f"• Date: {today}",
        f"• Branch or tag: {branch_or_tag}",
        f"• Release notes: {release_notes_url}",
        f"• Environments: {environments}",
    ])
    print(message)

    if slack_bot_token and slack_channel:
        resp = http_post(
            "https://slack.com/api/chat.postMessage",
            headers=make_slack_headers(slack_bot_token),
            json={"channel": slack_channel, "text": message},
        )
        # Slack's Web API returns HTTP 200 even on failure -- the real success/failure signal is
        # the "ok" field in the JSON body, not the status code.
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or not body.get("ok"):
            log.error("Failed to post to Slack channel '%s': %s - %s", slack_channel, resp.status_code, body.get("error", resp.text))
            sys.exit(1)
        log.info("Posted release notification to Slack channel '%s'", slack_channel)

    sys.exit(0)

def print_subcommand_usage(subcommand):
    prog = os.path.basename(sys.argv[0])
    sub_desc = next((d for s, d in _SUBCOMMAND_DESCS if s == subcommand), "")
    var_w = max((len(row[1]) for row in SUBCOMMAND_ENV_VARS if row[0] == subcommand), default=8)

    grouped = {}
    for sub, var, req, desc_text in SUBCOMMAND_ENV_VARS:
        if sub == subcommand:
            grouped.setdefault(sub, []).append((var, req, desc_text))

    def _var_order(v):
        var, req, _ = v
        return (0 if req == "required" else 1, 1 if var == "BRANCH_OR_TAG" else 0)

    lines = [f"usage: {prog} [-v] {subcommand}", "", f"  {sub_desc}"]
    if subcommand in grouped:
        lines.append("")
        lines.append("  environment variables:")
        for var, req, desc_text in sorted(grouped[subcommand], key=_var_order):
            lines.append(f"    {var:<{var_w}}  ({req})  {desc_text}")
    lines.append("")
    print("\n".join(lines), file=sys.stderr)


def cmd_usage(args):
    prog = os.path.basename(sys.argv[0])
    sub_w = max(len(s) for s, _ in _SUBCOMMAND_DESCS)
    var_w = max(len(row[1]) for row in SUBCOMMAND_ENV_VARS)

    lines = [
        "  _  __     _     _                    ",
        " | |/ /__ _| | __| | ___ _ __ ___  ___ ",
        " | ' // _` | |/ _` |/ _ \\ '__/ _ \\/ __|",
        " | . \\ (_| | | (_| |  __/ | | (_) \\__ \\",
        " |_|\\_\\__,_|_|\\__,_|\\___|_|  \\___/|___/",
        "",
        f"usage: {prog} [-v] <subcommand>",
        "",
        "Manage Kalderos Phoenix pipelines and GitHub workflows.",
        "All configuration is supplied via environment variables.",
        "",
        "options:",
        "  -v, --verbose    Enable verbose (DEBUG) logging",
        "  -s, --csv        Output tables as CSV",
        "  -j, --json       Output tables as JSON",
        "",
        "subcommands:",
    ]
    for sub, desc in _SUBCOMMAND_DESCS:
        lines.append(f"  {sub:<{sub_w}}  {desc}")

    grouped = {}
    for sub, var, req, desc in SUBCOMMAND_ENV_VARS:
        grouped.setdefault(sub, []).append((var, req, desc))

    lines.append("")
    lines.append("environment variables:")
    for sub, _ in _SUBCOMMAND_DESCS:
        if sub not in grouped:
            continue
        lines.append("")
        lines.append(f"  {sub}")
        def _var_order(v):
            var, req, _ = v
            return (0 if req == "required" else 1, 1 if var == "BRANCH_OR_TAG" else 0)
        for var, req, desc in sorted(grouped[sub], key=_var_order):
            lines.append(f"    {var:<{var_w}}  ({req})  {desc}")

    print("\n".join(lines))
    sys.exit(0)

# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Kalderos DevOps -- manage Phoenix pipelines in Azure DevOps")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging")
    parser.add_argument("-s", "--csv", action="store_true", help="Output tables as CSV")
    parser.add_argument("-j", "--json", action="store_true", help="Output tables as JSON")
    subparsers = parser.add_subparsers(dest="command", required=False)

    subparsers.add_parser("usage",  help="Show usage information and environment variables")
    subparsers.add_parser("help",   help="Alias for usage")
    subparsers.add_parser("list_environments", help="List all unique environments across all release pipelines")
    subparsers.add_parser("list_recent_builds", help="List all release pipelines with recent tags and branches (COUNT, default 8)")
    subparsers.add_parser("list_build_pipelines", help="List all build pipelines (filter via REPOSITORIES)")
    subparsers.add_parser("list_pipelines", help="List all release definitions with their git repository (set STAGE for current_version)")
    subparsers.add_parser("build_pipelines",  help="Trigger build pipelines (uses BRANCH_OR_TAG if set, otherwise main; filter via PIPELINES)")
    subparsers.add_parser("calc_pr", help="Print the PR whose terraform-plan-eastus.yml run last succeeded (GITHUB_TOKEN)")
    subparsers.add_parser("deploy_pipelines", help="Trigger deployments for release pipelines matching BRANCH_OR_TAG to ENVIRONMENTS (filter via PIPELINES)")
    subparsers.add_parser("tag_repository", help="Create and push a git tag from a source branch (BRANCH -> NAME)")
    subparsers.add_parser("list_repositories", help="List all repositories under the KalderosLLC GitHub org")
    subparsers.add_parser("create_release_notes", help="Create a GitHub release with auto-generated release notes (REPOSITORY, TAG)")
    subparsers.add_parser("git_tickets", help="List Jira tickets (CES-*, T340B-*) from merge commits between FROM_TAG and TAG")
    subparsers.add_parser("apply_terraform", help="Trigger the terraform-apply-eastus workflow for a PR and one or more environments (PR, ENVIRONMENTS)")
    subparsers.add_parser("apply_flyway", help="Trigger the flywayMigration workflow for one or more environments (ENVIRONMENTS, optional BRANCH_OR_TAG)")
    subparsers.add_parser("slack_find_channel", help="Resolve a Slack channel name to its ID (SLACK_BOT_TOKEN, NAME)")
    subparsers.add_parser("slack_release", help="Prepare a release notification message, and post it to Slack if SLACK_BOT_TOKEN/SLACK_CHANNEL are set (ENVIRONMENTS, BRANCH_OR_TAG)")

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)
        logging.getLogger().handlers[0].setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s [%(funcName)s:%(lineno)d] %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")
        )

    if not args.command or args.command in ("usage", "help"):
        cmd_usage(args)

    # Subcommands that don't need an Azure DevOps PAT / headers.
    NO_AUTH_COMMANDS = {
        "tag_repository":       cmd_tag,
        "list_repositories":    cmd_list_repositories,
        "create_release_notes": cmd_create_release_notes,
        "git_tickets":          cmd_git_tickets,
        "calc_pr":              cmd_calc_pr,
        "apply_terraform":      cmd_apply_terraform,
        "apply_flyway":         cmd_apply_flyway,
        "slack_find_channel":  cmd_slack_find_channel,
        "slack_release":        cmd_slack_release,
    }

    if args.command in NO_AUTH_COMMANDS:
        NO_AUTH_COMMANDS[args.command](args)

    pat = os.getenv("AZURE_DEVOPS_EXT_PAT")
    missing = [name for name, val in [("AZURE_DEVOPS_EXT_PAT", pat)] if val is None]
    if missing:
        print_subcommand_usage(args.command)
        for name in missing:
            log.error("Missing required environment variable: %s", name)
        sys.exit(1)

    headers = make_headers(pat)

    # Subcommands that need an Azure DevOps PAT / headers.
    AZURE_COMMANDS = {
        "list_build_pipelines": cmd_list_build_pipelines,
        "list_pipelines":     cmd_list_pipelines,
        "list_recent_builds": cmd_list_recent_builds,
        "list_environments":  cmd_list_environments,
        "build_pipelines":    cmd_build,
        "deploy_pipelines":   cmd_deploy,
    }

    if args.command in AZURE_COMMANDS:
        AZURE_COMMANDS[args.command](args, headers)

if __name__ == "__main__":
    main()
