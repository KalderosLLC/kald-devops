"""GitHub Actions run-dispatch/poll helpers shared by the terraform and flyway
command modules -- neither one owns these outright, so they live here rather
than in either."""
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from kald_devops.common import http_get, extract_error, trunc, make_table, print_table

log = logging.getLogger(__name__)


def fetch_triggered_run_url(gh_headers, workflow_file, triggered_at, retries=5, delay=2,
                            repo="KalderosLLC/phoenix"):
    """Return (html_url, run_id) for the newly-dispatched workflow run, or (None, None).

    The GitHub REST API doesn't hand back a run ID from the dispatch call itself, so this
    infers it by polling the workflow's recent runs for the first one created at/after
    triggered_at. That inference is only unambiguous if no other dispatch of the same
    workflow is in flight concurrently -- callers MUST trigger and claim runs one at a time
    (not fire multiple dispatches in parallel and then race to claim), or two runs created
    moments apart can get attributed to the wrong caller."""
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
            return run["html_url"], run["id"]
    return None, None


def poll_gh_run(gh_headers, repo, run_id, poll_interval=10, timeout=1800):
    """Block until a GitHub Actions run completes. Returns (status_str, reason_str_or_None)."""
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}"
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        resp = http_get(url, headers=gh_headers)
        if resp.status_code == 200:
            run = resp.json()
            if run.get("status") == "completed":
                conclusion = run.get("conclusion") or "unknown"
                return conclusion, (None if conclusion == "success" else conclusion)
            log.info("Still waiting (%s, %ds elapsed)... %s", run.get("status", "unknown"), int(time.time() - started), run_url)
        else:
            log.error("Polling %s/actions/runs/%s: HTTP %s - %s", repo, run_id, resp.status_code, extract_error(resp.text))
        time.sleep(poll_interval)
    return "timed_out", "polling timed out"


def trigger_and_poll_environments(args, environments_list, trigger_fn, poll_fn, extra_column, extra_value, log_tool):
    """Shared orchestration for cmd_apply_terraform/cmd_apply_flyway: dispatch one GitHub
    Actions run per environment -- strictly sequentially, via trigger_fn -- then poll all
    triggered runs in parallel via poll_fn, then print a sorted result table and exit.

    trigger_fn(env) -> (env, url_or_None, run_id_or_None); called once per environment, in
    order (never fanned out across threads -- see fetch_triggered_run_url's docstring for
    why concurrent dispatches of the same workflow can't be told apart from one another).
    poll_fn(run_id) -> (status, reason_or_None); called once per successfully-triggered
    environment, in parallel.
    extra_column/extra_value: name and (fixed, same-for-every-row) value of the one extra
    table column each caller needs (e.g. ("pr", pr) or ("branch_or_tag", branch_or_tag)).
    log_tool: short name used in per-row success/failure log lines (e.g. "terraform").

    Always exits: 0 if every triggered environment succeeded and none failed to dispatch,
    1 otherwise. Never returns.
    """
    results = [trigger_fn(env) for env in environments_list]

    triggered = [(env, url, run_id) for env, url, run_id in results if url]
    errors    = [env for env, url, run_id in results if url is None]

    for env, url, _ in triggered:
        log.info("%-20s %s", trunc(f"{env}/{extra_value}", 20), url)
    if errors:
        log.warning("%d environment(s) failed to trigger: %s", len(errors), errors)
    if not triggered:
        sys.exit(1 if errors else 0)

    log.info("Waiting for %d run(s) to complete...", len(triggered))

    def _poll(item):
        env, url, run_id = item
        if run_id:
            status, reason = poll_fn(run_id)
        else:
            status, reason = "unknown", "run ID not captured"
        return env, url, status, reason or "-"

    with ThreadPoolExecutor() as executor:
        poll_results = list(executor.map(_poll, triggered))

    poll_results.sort(key=lambda r: r[0].lower())
    table = make_table("environment", extra_column, "link", "status", "reason")
    any_unsuccessful = bool(errors)
    for env, url, status, reason in poll_results:
        if status == "success":
            log.info("%s / %s: %s", log_tool, env, status)
        else:
            any_unsuccessful = True
            log.error("%s / %s: %s - %s", log_tool, env, status, reason)
        table.add_row([env, extra_value, url, status, reason])
    print_table(table, args)
    sys.exit(1 if any_unsuccessful else 0)
