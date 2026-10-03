"""List Jira tickets referenced by merge commits between two refs."""
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

from kald_devops.common import (
    http_get, make_gh_headers, parse_semver, flex_width, make_table, trunc,
    print_table, require_env_vars, extract_gh_error,
)

log = logging.getLogger(__name__)

JIRA_PATTERN = re.compile(r'\b((?:CES|T340B)-\d+)\b')


def resolve_from_tag(gh_headers, repository, prefix, major, minor):
    target_minor = minor - 1
    tag_prefix = f"{prefix}{major}.{target_minor}."
    log.debug("resolve_from_tag: repository=%s prefix=%r major=%s minor=%s -- searching for tags starting with %r",
              repository, prefix, major, minor, tag_prefix)
    matching = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repository}/tags?per_page=100&page={page}"
        log.debug("GET %s", url)
        resp = http_get(url, headers=gh_headers)
        log.debug("Response: HTTP %s", resp.status_code)
        if resp.status_code != 200:
            log.debug("Response headers: %s", dict(resp.headers))
            log.debug("Response body: %s", resp.text[:500])
            return None, f"Failed to fetch tags: {resp.status_code} - {extract_gh_error(resp)}"
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
        return None, f"{resp.status_code} - {extract_gh_error(resp)}"
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
            return None, f"{resp.status_code} - {extract_gh_error(resp)}"
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

    require_env_vars("git_tickets", REPOSITORY=repository, TAG=tag, GITHUB_TOKEN=github_token)

    if "/" not in repository or repository.startswith("/") or repository.endswith("/"):
        log.error("REPOSITORY '%s' must be in the format organization/repo_name (e.g. KalderosLLC/phoenix)", repository)
        sys.exit(1)

    parsed_tag = parse_semver(tag)
    if not parsed_tag:
        log.error("TAG '%s' does not conform to semantic versioning (e.g. v1.21.0)", tag)
        sys.exit(1)
    prefix, major, minor, patch = parsed_tag
    log.debug("Parsed TAG '%s' -- prefix=%r major=%s minor=%s patch=%s", tag, prefix, major, minor, patch)

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
    for jira_id, authors in rows:
        table.add_row([jira_id, trunc(summaries[jira_id], summary_w), trunc(", ".join(authors), authors_w)])
    print_table(table, args)
    sys.exit(0)
