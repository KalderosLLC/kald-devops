"""Trigger the flywayMigration workflow in phoenix-data-gateway."""
import logging
import os
import sys
from datetime import datetime, timezone

import requests

from kald_devops.common import http_get, http_post, make_gh_headers, SEMVER_RE, parse_env_list, require_env_vars, extract_gh_error
from kald_devops.github_actions import fetch_triggered_run_url, poll_gh_run, trigger_and_poll_environments

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

    require_env_vars("apply_flyway", ENVIRONMENTS=raw_envs, GITHUB_TOKEN=github_token)

    environments_list = parse_env_list(raw_envs)

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

    trigger_and_poll_environments(
        args, environments_list,
        trigger_fn=lambda env: _trigger_flyway_env(env, branch_or_tag, gh_headers),
        poll_fn=lambda run_id: poll_gh_run(gh_headers, "KalderosLLC/phoenix-data-gateway", run_id),
        extra_column="branch_or_tag", extra_value=branch_or_tag, log_tool="flyway",
    )
