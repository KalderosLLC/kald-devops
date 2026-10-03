"""Create a GitHub release with auto-generated release notes."""
import logging
import os
import sys

from kald_devops.common import http_post, make_gh_headers, require_env_vars, extract_gh_error

log = logging.getLogger(__name__)


def cmd_create_release_notes(args):
    repository = os.getenv("REPOSITORY")
    tag = os.getenv("TAG")
    github_token = os.getenv("GITHUB_TOKEN")

    require_env_vars("create_release_notes", REPOSITORY=repository, TAG=tag, GITHUB_TOKEN=github_token)

    gh_headers = make_gh_headers(github_token)

    url = f"https://api.github.com/repos/{repository}/releases"
    payload = {
        "tag_name": tag,
        "name": tag,
        "generate_release_notes": True,
    }
    resp = http_post(url, headers=gh_headers, json=payload)
    if resp.status_code not in (200, 201):
        log.error("Failed to create release: %s - %s", resp.status_code, extract_gh_error(resp))
        sys.exit(1)

    print(resp.json().get("html_url", "-"))
    sys.exit(0)
