"""GitHub Actions run-dispatch/poll helpers shared by the terraform and flyway
command modules -- neither one owns these outright, so they live here rather
than in either."""
import logging
import time
from datetime import datetime

from kald_devops.common import http_get, extract_error

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
