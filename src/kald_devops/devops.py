#!/usr/bin/env python3
"""Entry point for the kald-devops CLI. Owns argument parsing and dispatch;
all actual command logic lives in kald_devops.commands.*, sharing
kald_devops.common for HTTP/formatting infrastructure and declarative
subcommand metadata (used for --help/usage text).

HANDLERS pairs each subcommand name with its actual function. It lives here
rather than alongside common.SUBCOMMAND_SPECS because every command module
calls common.print_subcommand_usage, so common.py cannot import the command
modules (that would be a cycle) -- it can only describe them declaratively.
"""
import argparse
import logging
import os
import sys

from kald_devops import common
from kald_devops.commands import (
    build, release, tag, repositories, git_tickets, release_notes,
    terraform, flyway, teams,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stderr)
log = logging.getLogger(__name__)

HANDLERS = {
    "list_environments": release.cmd_list_environments,
    "list_recent_builds": release.cmd_list_recent_builds,
    "list_build_pipelines": build.cmd_list_build_pipelines,
    "list_pipelines": release.cmd_list_pipelines,
    "build_pipelines": build.cmd_build,
    "calc_pr": terraform.cmd_calc_pr,
    "deploy_pipelines": release.cmd_deploy,
    "add_environment_to_pipelines": release.cmd_add_environment_to_pipelines,
    "create_releases_from_artifact": release.cmd_create_releases_from_artifact,
    "tag_repository": tag.cmd_tag,
    "list_repositories": repositories.cmd_list_repositories,
    "create_release_notes": release_notes.cmd_create_release_notes,
    "git_tickets": git_tickets.cmd_git_tickets,
    "apply_terraform": terraform.cmd_apply_terraform,
    "apply_flyway": flyway.cmd_apply_flyway,
    "teams_release": teams.cmd_teams_release,
}


def main():
    parser = argparse.ArgumentParser(description="Kalderos DevOps -- manage Phoenix pipelines in Azure DevOps")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging")
    parser.add_argument("-s", "--csv", action="store_true", help="Output tables as CSV")
    parser.add_argument("-j", "--json", action="store_true", help="Output tables as JSON")
    subparsers = parser.add_subparsers(dest="command", required=False)

    for s in common.SUBCOMMAND_SPECS:
        subparsers.add_parser(s["name"], help=s["argparse_help"])
    subparsers.add_parser("help", help="Alias for usage")

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)
        logging.getLogger().handlers[0].setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s [%(funcName)s:%(lineno)d] %(message)s",
                              datefmt="%Y-%m-%d %H:%M:%S")
        )

    if not args.command or args.command in ("usage", "help"):
        common.cmd_usage(args)

    spec = common._SUBCOMMAND_SPECS_BY_NAME.get(args.command)
    handler = HANDLERS.get(args.command)
    if spec is None or handler is None:
        return

    if not spec["needs_azure"]:
        handler(args)
        return

    pat = os.getenv("AZURE_DEVOPS_EXT_PAT")
    if pat is None:
        common.print_subcommand_usage(args.command)
        log.error("Missing required environment variable: AZURE_DEVOPS_EXT_PAT")
        sys.exit(1)

    headers = common.make_headers(pat)
    handler(args, headers)

if __name__ == "__main__":
    main()
