"""Build pipeline commands (dev.azure.com build definitions)."""
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

from kald_devops.common import (
    http_get, http_post, extract_error, resolve_pipeline_tokens,
    make_table, print_table, fix_column_width, trunc, flex_width,
    organization, project, SEMVER_RE, parse_pipelines_env,
    print_subcommand_usage,
)

log = logging.getLogger(__name__)


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
    build_url = (
        f"https://dev.azure.com/{organization}/{requests.utils.quote(project, safe='')}/"
        f"_build/results?buildId={build_id}"
    )
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
            log.info("Still waiting (%s, %ds elapsed)... %s", build.get("status", "unknown"), int(time.time() - started), build_url)
        else:
            log.error("Polling build %s: HTTP %s - %s", build_id, resp.status_code, extract_error(resp.text))
        time.sleep(poll_interval)
    return "timed_out", "polling timed out", None

def cmd_build(args, headers):
    branch_or_tag = os.getenv("BRANCH_OR_TAG", "main")

    if SEMVER_RE.match(branch_or_tag):
        source_branch = f"refs/tags/{branch_or_tag}"
        log.debug("BRANCH_OR_TAG='%s' matches semver - treating as a tag", branch_or_tag)
    else:
        source_branch = f"refs/heads/{branch_or_tag}"
        log.debug("BRANCH_OR_TAG='%s' does not match semver - treating as a branch", branch_or_tag)
    log.debug("Building from: %s", source_branch)

    all_defs = fetch_build_pipelines(headers)

    tokens = parse_pipelines_env()
    if not tokens:
        print_subcommand_usage("build_pipelines")
        log.error("Missing required environment variable: PIPELINES")
        sys.exit(1)

    folder_defs, _ = resolve_pipeline_tokens(headers, tokens, all_defs, fetch_build_def_repo, "build pipeline")
    if not folder_defs:
        log.error("No build pipelines matched PIPELINES='%s'", os.getenv("PIPELINES"))
        sys.exit(1)

    log.debug("Found %d pipeline(s) to build", len(folder_defs))

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
    any_unsuccessful = failure_count > 0
    for pid, pname, bot, build_url, duration, status, reason in poll_results:
        if status == "succeeded":
            log.info("%s: %s", pname, status)
        else:
            any_unsuccessful = True
            log.error("%s: %s - %s", pname, status, reason)
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

    pipelines, _ = resolve_pipeline_tokens(headers, tokens, all_defs, fetch_build_def_repo, "build pipeline")
    if not pipelines:
        log.error("No build pipelines matched REPOSITORIES='%s'", os.getenv("REPOSITORIES"))
        sys.exit(1)
    pipelines.sort(key=lambda d: d["name"].lower())

    # Get repository for each pipeline
    with ThreadPoolExecutor() as executor:
        repo_futures = {d["id"]: executor.submit(fetch_build_def_repo, headers, d["id"]) for d in pipelines}
        repos = {did: f.result() for did, f in repo_futures.items()}

    log.info("Found %d build pipeline(s) matching REPOSITORIES='%s':", len(pipelines), os.getenv("REPOSITORIES"))

    name_w = max((len(d["name"]) for d in pipelines), default=len("pipeline_name"))
    repo_w = max((len(repos.get(d["id"], "-")) for d in pipelines), default=len("repository"))
    table = make_table("pipeline_id", "pipeline_name", "repository")
    fix_column_width(table, "pipeline_name", name_w)
    fix_column_width(table, "repository", repo_w)

    for d in pipelines:
        repo = repos.get(d["id"], "-")
        table.add_row([d["id"], d["name"], repo])

    print_table(table, args)
    sys.exit(0)
