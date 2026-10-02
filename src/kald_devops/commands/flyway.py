"""Trigger the flywayMigration workflow in phoenix-data-gateway."""
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from kald_devops.common import http_get, http_post, make_gh_headers, make_table, print_table, trunc, SEMVER_RE, print_subcommand_usage, extract_gh_error
from kald_devops.github_actions import fetch_triggered_run_url, poll_gh_run

log = logging.getLogger(__name__)


def _trigger_flyway_env(environment, branch_or_tag, gh_headers):
    """Dispatch flywayMigration for one environment and claim its run URL/ID before returning.
    Must be called once at a time (not fanned out across a ThreadPoolExecutor) --
    fetch_triggered_run_url can't tell two concurrently-created runs apart, so overlapping
    calls risk attributing the wrong run to the wrong environment."""
    repo = "KalderosLLC/phoenix-data-gateway"
    url = f"https://api.github.com/repos/{repo}/actions/workflows/flywayMigration.yml/dispatches"
    triggered_at = datetime.now(timezone.utc)
    resp = http_post(url, headers=gh_headers, json={"ref": branch_or_tag, "inputs": {"environment": environment.lower()}})
    if resp.status_code not in (200, 204):
        msg = f"HTTP {resp.status_code} - {extract_gh_error(resp)}"
        log.error("Failed to trigger flywayMigration for '%s': %s", environment, msg)
        return environment, None, None
    log.debug("Triggered flywayMigration for environment '%s' from '%s'", environment, branch_or_tag)
    run_url, run_id = fetch_triggered_run_url(gh_headers, "flywayMigration.yml", triggered_at, repo=repo)
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

    # Trigger and claim each environment's run one at a time -- see fetch_triggered_run_url's
    # docstring for why fanning these out concurrently risks swapping run URLs between
    # environments.
    results = [_trigger_flyway_env(env, branch_or_tag, gh_headers) for env in environments_list]

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
            log.error("flyway / %s: %s - %s", env, status, reason)
        table.add_row([env, branch_or_tag, url, status, reason])
    print_table(table, args)
    sys.exit(1 if any_unsuccessful else 0)
