"""Shared infrastructure for kald_devops.devops and its kald_devops.commands.*
submodules: HTTP wrappers with retry, table formatting, header builders, the
pipeline-token resolver, and the declarative subcommand metadata used to
render --help/usage text.

This module must not import anything from kald_devops.commands.* -- every
command submodule imports from here, and a reverse import would create a
cycle. The subcommand *handlers* (actual cmd_* function objects) therefore
deliberately do not live here; see devops.py's HANDLERS dict for why.
"""
import logging
import os
import re
import shutil
import sys
import time
import base64
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from prettytable import PrettyTable

log = logging.getLogger(__name__)

# =============================================================================
# Table / text formatting
# =============================================================================

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

# =============================================================================
# HTTP with retry
# =============================================================================

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
http_put   = _with_retry(requests.put)

# === CONFIGURATION ===
organization = "kalderos"
project = "Drug Discount Management"
folder_path = "\\phoenix"

# =============================================================================
# Shared utilities
# =============================================================================

def extract_error(text):
    match = re.search(r"<title>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return "HTTP error (see response for details)"

def extract_gh_error(resp):
    """Extract a human-readable error message from a GitHub REST API error response,
    falling back to the raw response text if the body isn't JSON."""
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    return body.get("message", resp.text)

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

def resolve_pipeline_tokens(headers, tokens, all_defs, fetch_detail, kind_label, get_repo=lambda v: v):
    """Resolve a mixed list of ID-or-repo-name tokens against all_defs (a list of pipeline/
    build definitions, each a dict with at least "id" and "name"). Numeric tokens are matched
    directly by ID; non-numeric tokens are matched by repository name, resolved concurrently
    via fetch_detail(headers, definition_id) for every definition in all_defs.

    get_repo extracts the comparable repo name from whatever fetch_detail returns (plain repo
    string, or e.g. a (repo, stages) tuple) -- only used when there are non-numeric tokens.

    Returns (matched_defs_deduped_by_id, detail_map), where detail_map maps definition ID to
    fetch_detail's result for every definition in all_defs, or {} if there were no non-numeric
    tokens to resolve (so no fetching was needed)."""
    id_tokens = [t for t in tokens if t.isdigit()]
    repo_tokens = [t for t in tokens if not t.isdigit()]
    raw_defs = []

    for t in id_tokens:
        matches = [d for d in all_defs if str(d["id"]) == t]
        if matches:
            raw_defs.extend(matches)
        else:
            log.warning("No %s found with ID %s", kind_label, t)

    detail_map = {}
    if repo_tokens:
        log.debug("Resolving %s(s) by repository: %s", kind_label, repo_tokens)
        with ThreadPoolExecutor() as executor:
            futures = {d["id"]: executor.submit(fetch_detail, headers, d["id"]) for d in all_defs}
            detail_map = {did: f.result() for did, f in futures.items()}
        for t in repo_tokens:
            matches = [d for d in all_defs if get_repo(detail_map.get(d["id"])).lower() == t.lower()]
            if matches:
                raw_defs.extend(matches)
            else:
                log.warning("No %s found for repository '%s'", kind_label, t)

    seen_ids = set()
    deduped = [d for d in raw_defs if d["id"] not in seen_ids and not seen_ids.add(d["id"])]
    return deduped, detail_map

def _fmt_deploy_dt(raw):
    """Format an ISO-8601 timestamp (Azure DevOps or GitHub Actions) to 'YYYY-MM-DD HH:MM UTC'-
    style local display, in America/New_York. Shared by release.py (deployment timestamps) and
    terraform.py (workflow run timestamps)."""
    if not raw:
        return "-"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return raw

def post_to_teams_webhook(webhook_url, title, message):
    """POST message to a Microsoft Teams Incoming Webhook (or an equivalent Power Automate flow
    trigger configured to accept the same shape) as a MessageCard. Returns True on success;
    logs and returns False on failure."""
    payload = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "summary": title,
        "title": title,
        "text": message,
    }
    resp = http_post(webhook_url, json=payload)
    # A Teams Incoming Webhook returns plain-text "1" on success rather than a JSON body (unlike
    # Slack's {"ok": ...} convention) -- a Power Automate flow trigger typically answers 202 with
    # an empty body instead. Treat any 2xx as success.
    if not (200 <= resp.status_code < 300):
        log.error("Failed to post to Teams webhook: HTTP %d - %s", resp.status_code, resp.text[:300])
        return False
    return True

# =============================================================================
# Version tags and environment ordering
# =============================================================================

SEMVER_RE = re.compile(r'^(v?)(\d+)\.(\d+)\.(\d+)(?:[.\-].+)?$')

def parse_semver(tag):
    m = SEMVER_RE.match(tag)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))

ENV_PRECEDENCE = ["dev", "automated", "perftest", "load", "mt", "demo", "qa", "test", "stage", "preview", "prod"]

def env_sort_key(env_name):
    lower = env_name.lower()
    try:
        return ENV_PRECEDENCE.index(lower)
    except ValueError:
        return len(ENV_PRECEDENCE)

# =============================================================================
# Subcommand metadata -- single source of truth for --help/usage text.
#
# Deliberately holds no handler (function) references: every command module
# below calls print_subcommand_usage, so if this list held handlers it would
# need to import every command module, which imports this one -- a cycle.
# devops.py (the entry point) pairs each name here with its actual handler in
# its own HANDLERS dict, used only for dispatch in main().
# =============================================================================

def _sub(name, argparse_help, usage_desc, needs_azure=False, env_vars=()):
    return {
        "name": name,
        "argparse_help": argparse_help,
        "usage_desc": usage_desc,
        "needs_azure": needs_azure,
        "env_vars": list(env_vars),
    }

SUBCOMMAND_SPECS = [
    _sub("usage", "Show usage information and environment variables", "Show this usage information"),
    _sub("list_environments", "List all unique environments across all release pipelines",
         "List all unique environments across release pipelines", needs_azure=True, env_vars=[
             ("AZURE_DEVOPS_EXT_PAT", "required", "Azure DevOps personal access token"),
         ]),
    _sub("list_recent_builds", "List all release pipelines with recent tags and branches (COUNT, default 8)",
         "List all release pipelines with recent tags and branches", needs_azure=True, env_vars=[
             ("AZURE_DEVOPS_EXT_PAT", "required", "Azure DevOps personal access token"),
             ("COUNT", "optional", "Number of recent builds to show per pipeline (default: 8)"),
         ]),
    _sub("list_build_pipelines", "List all build pipelines (filter via REPOSITORIES)",
         "List Azure DevOps build pipelines with repository", needs_azure=True, env_vars=[
             ("AZURE_DEVOPS_EXT_PAT", "required", "Azure DevOps personal access token"),
             ("REPOSITORIES", "required", "Comma-delimited pipeline IDs or repository paths (e.g. KalderosLLC/phoenix); filters to pipelines with a matching repository path"),
         ]),
    _sub("list_pipelines", "List all release definitions with their git repository (set STAGE for current_version)",
         "List release definitions with repository and recent refs", needs_azure=True, env_vars=[
             ("AZURE_DEVOPS_EXT_PAT", "required", "Azure DevOps personal access token"),
             ("PIPELINES", "required", "Comma-delimited pipeline IDs or repository paths (e.g. KalderosLLC/phoenix); filters to pipelines with a matching repository path"),
             ("ENVIRONMENTS", "optional", "Comma-delimited environments; adds environment and current_version columns, sorted by pipeline then environment"),
         ]),
    _sub("build_pipelines", "Trigger build pipelines (uses BRANCH_OR_TAG if set, otherwise main; filter via PIPELINES)",
         "Trigger Azure DevOps build pipelines", needs_azure=True, env_vars=[
             ("AZURE_DEVOPS_EXT_PAT", "required", "Azure DevOps personal access token"),
             ("BRANCH_OR_TAG", "optional", "Semver tag (e.g. v1.21.0) or branch name (e.g. main) to build from (default: main)"),
             ("PIPELINES", "required", "Comma-delimited pipeline IDs or repository paths (e.g. KalderosLLC/phoenix); filters to pipelines with a matching repository path"),
         ]),
    _sub("calc_pr", "Print the PR whose terraform-plan-eastus.yml run last succeeded (GITHUB_TOKEN)",
         "Print the PR with the latest successful terraform-plan-eastus.yml run", env_vars=[
             ("GITHUB_TOKEN", "required", "GitHub personal access token with repo scope"),
         ]),
    _sub("deploy_pipelines", "Trigger deployments for release pipelines matching BRANCH_OR_TAG to ENVIRONMENTS (filter via PIPELINES)",
         "Trigger Azure DevOps release pipeline deployments", needs_azure=True, env_vars=[
             ("AZURE_DEVOPS_EXT_PAT", "required", "Azure DevOps personal access token"),
             ("BRANCH_OR_TAG", "required", "Semver tag (e.g. v1.21.0) or branch name (e.g. main) to deploy from"),
             ("ENVIRONMENTS", "required", "Comma-delimited list of target environments (e.g. Stage,Prod)"),
             ("PIPELINES", "required", "Comma-delimited pipeline IDs or repository paths (e.g. KalderosLLC/phoenix); filters to pipelines with a matching repository path"),
         ]),
    _sub("add_environment_to_pipelines", "Add a new environment to release pipeline definitions (PIPELINES, ENVIRONMENT)",
         "Add a new environment to one or more release pipeline definitions, cloning an existing environment as a template",
         needs_azure=True, env_vars=[
             ("AZURE_DEVOPS_EXT_PAT", "required", "Azure DevOps personal access token"),
             ("PIPELINES", "required", "Comma-separated list of release pipeline names to add the environment to"),
             ("ENVIRONMENT", "required", "Name of the new environment/stage to add (cloned from an existing 'Preview' environment, or the first available one, as a template)"),
         ]),
    _sub("create_releases_from_artifact", "Create new releases for pipelines, reusing an existing artifact (PIPELINES, SOURCE_RELEASE optional)",
         "Create new releases for pipelines, reusing an existing build artifact",
         needs_azure=True, env_vars=[
             ("AZURE_DEVOPS_EXT_PAT", "required", "Azure DevOps personal access token"),
             ("PIPELINES", "required", "Comma-separated list of release pipeline names to create new releases for"),
             ("SOURCE_RELEASE", "optional", "Name of an existing release to reuse the artifact from (defaults to the first release found with a usable artifact)"),
         ]),
    _sub("tag_repository", "Create and push a git tag from a source branch (BRANCH -> NAME)",
         "Create and push a git tag to one or more repositories; when KalderosLLC/phoenix is listed, also tags phoenix-data-gateway and updates the submodule pointer",
         env_vars=[
             ("GITHUB_TOKEN", "required", "GitHub personal access token with repo scope"),
             ("TAG", "required", "Git tag name to create (e.g. v1.21.0-rc)"),
             ("REPOSITORIES", "required", "Comma-separated list of full GitHub repository paths to tag (e.g. KalderosLLC/phoenix,KalderosLLC/phoenix-snowflake-gateway); when KalderosLLC/phoenix is included, phoenix-data-gateway is tagged implicitly and the submodule pointer is updated before tagging phoenix - do not also list phoenix-data-gateway separately"),
             ("BRANCH", "optional", "Source branch to tag (default: main, e.g. hotfix/v1.19.0); applies to all repositories"),
             ("PDG_TAG_OR_BRANCH", "optional", "Tag or branch to pin phoenix-data-gateway to when KalderosLLC/phoenix is tagged implicitly (default: BRANCH); checked as an existing tag first, then as a branch, and errors out if neither exists in phoenix-data-gateway"),
         ]),
    _sub("list_repositories", "List all repositories under the KalderosLLC GitHub org",
         "List all repositories in the KalderosLLC GitHub org", env_vars=[
             ("GITHUB_TOKEN", "required", "GitHub personal access token with read:org and repo scopes"),
         ]),
    _sub("create_release_notes", "Create a GitHub release with auto-generated release notes (REPOSITORY, TAG)",
         "Create a GitHub release with auto-generated notes", env_vars=[
             ("GITHUB_TOKEN", "required", "GitHub personal access token with repo scope"),
             ("REPOSITORY", "required", "Full GitHub repository path (e.g. KalderosLLC/phoenix)"),
             ("TAG", "required", "Tag name to create the release for"),
         ]),
    _sub("git_tickets", "List Jira tickets (CES-*, T340B-*) from merge commits between FROM_TAG and TAG",
         "List Jira tickets from merge commits between two refs", env_vars=[
             ("GITHUB_TOKEN", "required", "GitHub personal access token with repo scope"),
             ("REPOSITORY", "required", "Full GitHub repository path (e.g. KalderosLLC/phoenix)"),
             ("TAG", "required", "Head tag or branch to inspect"),
             ("FROM_TAG", "optional", "Base tag or branch (defaults to last 100 commits of TAG)"),
             ("JIRA_EMAIL", "optional", "Atlassian account email for fetching ticket summaries"),
             ("JIRA_TOKEN", "optional", "Atlassian API token for fetching ticket summaries (omit to skip summary lookup)"),
         ]),
    _sub("apply_terraform", "Trigger the terraform-apply-eastus workflow for a PR and one or more environments (PR, ENVIRONMENTS)",
         "Trigger the terraform-apply-eastus workflow", env_vars=[
             ("GITHUB_TOKEN", "required", "GitHub personal access token with workflow scope"),
             ("ENVIRONMENTS", "required", "Comma-delimited list of target environments (e.g. Stage,Prod)"),
             ("PR", "required", "Pull request number (use calc_pr to find the PR with the latest successful terraform-plan-eastus.yml run)"),
         ]),
    _sub("apply_flyway", "Trigger the flywayMigration workflow for one or more environments (ENVIRONMENTS, optional BRANCH_OR_TAG)",
         "Trigger the flywayMigration workflow", env_vars=[
             ("GITHUB_TOKEN", "required", "GitHub personal access token with workflow scope"),
             ("ENVIRONMENTS", "required", "Comma-delimited list of target environments (e.g. Stage,Prod)"),
             ("BRANCH_OR_TAG", "optional", "Semver tag (e.g. v1.21.0) or branch name (e.g. main); verified before dispatch (default: main)"),
         ]),
    _sub("teams_release", "Prepare a release notification message, and post it to Teams if TEAMS_WEBHOOK_URL is set (ENVIRONMENTS, BRANCH_OR_TAG)",
         "Prepare a release notification message, and post it to Teams if TEAMS_WEBHOOK_URL is set", env_vars=[
             ("ENVIRONMENTS", "required", "Comma-delimited list of target environments (e.g. Stage,Prod)"),
             ("BRANCH_OR_TAG", "required", "Semver tag (e.g. v1.21.0) or branch name (e.g. main)"),
             ("TEAMS_WEBHOOK_URL", "optional", "Microsoft Teams Incoming Webhook (or equivalent Power Automate flow trigger) URL to post the release notification to"),
         ]),
]

_SUBCOMMAND_SPECS_BY_NAME = {s["name"]: s for s in SUBCOMMAND_SPECS}


def _env_var_order(v):
    var, req, _ = v
    return (0 if req == "required" else 1, 1 if var == "BRANCH_OR_TAG" else 0)


def print_subcommand_usage(subcommand):
    prog = os.path.basename(sys.argv[0])
    sub = _SUBCOMMAND_SPECS_BY_NAME.get(subcommand, {})
    sub_desc = sub.get("usage_desc", "")
    env_vars = sub.get("env_vars", [])
    var_w = max((len(var) for var, _, _ in env_vars), default=8)

    lines = [f"usage: {prog} [-v] {subcommand}", "", f"  {sub_desc}"]
    if env_vars:
        lines.append("")
        lines.append("  environment variables:")
        for var, req, desc_text in sorted(env_vars, key=_env_var_order):
            lines.append(f"    {var:<{var_w}}  ({req})  {desc_text}")
    lines.append("")
    print("\n".join(lines), file=sys.stderr)


def cmd_usage(args):
    prog = os.path.basename(sys.argv[0])
    sub_w = max(len(s["name"]) for s in SUBCOMMAND_SPECS)
    var_w = max((len(var) for s in SUBCOMMAND_SPECS for var, _, _ in s["env_vars"]), default=8)

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
    for s in SUBCOMMAND_SPECS:
        lines.append(f"  {s['name']:<{sub_w}}  {s['usage_desc']}")

    lines.append("")
    lines.append("environment variables:")
    for s in SUBCOMMAND_SPECS:
        if not s["env_vars"]:
            continue
        lines.append("")
        lines.append(f"  {s['name']}")
        for var, req, desc in sorted(s["env_vars"], key=_env_var_order):
            lines.append(f"    {var:<{var_w}}  ({req})  {desc}")

    print("\n".join(lines))
    sys.exit(0)
