"""Subcommand implementations, grouped by domain. Each module here imports
its shared infrastructure from kald_devops.common (and kald_devops.github_actions
where applicable) and must not import from devops.py, which imports from
these modules and would otherwise create a cycle."""
