"""List repositories under the KalderosLLC GitHub org."""
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from kald_devops.common import http_get, make_gh_headers, make_table, print_table, fix_column_width, trunc, print_subcommand_usage, extract_gh_error

log = logging.getLogger(__name__)

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
            message = extract_gh_error(resp)
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
    desc_w = 48
    table = make_table("last_modified", "repository", "visibility", "description")
    fix_column_width(table, "repository", repo_w)
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
