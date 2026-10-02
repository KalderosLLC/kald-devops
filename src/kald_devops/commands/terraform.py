"""Trigger and resolve the terraform-apply-eastus / terraform-plan-eastus workflows."""
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from kald_devops.common import (
    http_get, http_post, make_gh_headers, make_table, print_table, trunc,
    print_subcommand_usage, _fmt_deploy_dt, extract_gh_error,
)
from kald_devops.github_actions import fetch_triggered_run_url, poll_gh_run

log = logging.getLogger(__name__)


def _trigger_terraform_env(environment, pr, gh_headers):
    """Dispatch terraform-apply-eastus for one environment and claim its run URL/ID before
    returning. Must be called once at a time (not fanned out across a ThreadPoolExecutor) --
    fetch_triggered_run_url can't tell two concurrently-created runs apart, so overlapping
    calls risk attributing the wrong run to the wrong environment."""
    url = "https://api.github.com/repos/KalderosLLC/phoenix/actions/workflows/terraform-apply-eastus.yml/dispatches"
    triggered_at = datetime.now(timezone.utc)
    resp = http_post(url, headers=gh_headers, json={
        "ref": "main",
        "inputs": {"pr-number": pr, "environment": environment.lower()},
    })
    if resp.status_code not in (200, 204):
        msg = f"HTTP {resp.status_code} - {extract_gh_error(resp)}"
        log.error("Failed to trigger terraform-apply-eastus for '%s': %s", environment, msg)
        return environment, None, None
    log.debug("Triggered terraform-apply-eastus for PR #%s in environment '%s'", pr, environment)
    run_url, run_id = fetch_triggered_run_url(gh_headers, "terraform-apply-eastus.yml", triggered_at)
    if not run_url:
        log.warning("Could not determine run URL for environment '%s'", environment)
    return environment, run_url, run_id


def fetch_workflow_runs(gh_headers, workflow_file, repo="KalderosLLC/phoenix", count=12):
    """Return the most recent `count` runs of a GitHub Actions workflow (newest first, GitHub's
    default order), or None if the fetch failed (error already logged)."""
    runs_url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs?per_page={count}"
    resp = http_get(runs_url, headers=gh_headers)
    if resp.status_code != 200:
        log.error("Failed to fetch %s runs: %s - %s", workflow_file, resp.status_code, extract_gh_error(resp))
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

    # Trigger and claim each environment's run one at a time -- see fetch_triggered_run_url's
    # docstring for why fanning these out concurrently risks swapping run URLs between
    # environments.
    results = [_trigger_terraform_env(env, pr, gh_headers) for env in environments_list]

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
            log.error("terraform / %s: %s - %s", env, status, reason)
        table.add_row([env, pr, url, status, reason])
    print_table(table, args)
    sys.exit(1 if any_unsuccessful else 0)
