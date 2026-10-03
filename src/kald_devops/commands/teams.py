"""Prepare a release notification message and post it to Microsoft Teams."""
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from kald_devops.common import SEMVER_RE, post_to_teams_webhook, require_env_vars

log = logging.getLogger(__name__)


def cmd_teams_release(args):
    environments = os.getenv("ENVIRONMENTS")
    branch_or_tag = os.getenv("BRANCH_OR_TAG")
    teams_webhook_url = os.getenv("TEAMS_WEBHOOK_URL")

    require_env_vars("teams_release", ENVIRONMENTS=environments, BRANCH_OR_TAG=branch_or_tag)

    log.debug("ENVIRONMENTS:      %s", environments)
    log.debug("BRANCH_OR_TAG:     %s", branch_or_tag)
    log.debug("TEAMS_WEBHOOK_URL: %s", "set" if teams_webhook_url else "not set")

    today = datetime.now(ZoneInfo("America/New_York")).strftime("%A, %B %-d, %Y - %H:%M %Z")
    is_tag = bool(SEMVER_RE.match(branch_or_tag))
    release_notes_url = (
        f"https://github.com/KalderosLLC/phoenix/releases/tag/{branch_or_tag}"
        if is_tag else
        f"https://github.com/KalderosLLC/phoenix/tree/{branch_or_tag}"
    )

    message = "\n".join([
        f"- Date: {today}",
        f"- Branch or tag: {branch_or_tag}",
        f"- Release notes: {release_notes_url}",
        f"- Environments: {environments}",
    ])
    print("\n".join(["Truzo Release", message]))

    if teams_webhook_url:
        if not post_to_teams_webhook(teams_webhook_url, "Truzo Release", message):
            sys.exit(1)
        log.info("Posted release notification to Teams")

    sys.exit(0)
