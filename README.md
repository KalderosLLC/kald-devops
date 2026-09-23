# kald-devops

Kalderos DevOps utilities for managing Phoenix pipelines, GitHub workflows, and database utilities.

## Installation

Install the package from GitHub Packages:

```bash
pip install kald-devops
```

### Local Development Installation

To install from a local repository:

```bash
pip install -e .
```

## Usage

### kald-devops

Unified CLI for managing Kalderos Phoenix pipelines and GitHub workflows.

```bash
kald-devops [-v] <subcommand>
```

All configuration is supplied via environment variables. See `kald-devops usage` for detailed subcommand documentation.

**Subcommands:**
- `usage` - Show usage information
- `list_repositories` - List all repositories in the KalderosLLC GitHub org
- `tag_repository` - Create and push a git tag from a source branch
- `create_release_notes` - Create a GitHub release with auto-generated notes
- `git_tickets` - List Jira tickets from merge commits between two refs
- `list_environments` - List all unique environments across release pipelines
- `list_pipelines` - List release definitions with repository and recent refs
- `build_pipelines` - Trigger Azure DevOps build pipelines
- `apply_terraform` - Trigger the terraform-apply-eastus workflow
- `apply_flyway` - Trigger the flywayMigration workflow
- `deploy_pipelines` - Trigger Azure DevOps release pipeline deployments
- `slack_release` - Prepare a release notification message for Slack

### kald-postgres-util

PostgreSQL database utilities and operations.

```bash
kald-postgres-util [-v] <subcommand>
```

**Subcommands include:**
- `apply_pr` - Apply pending migrations or release procedures (exposed as a workflow)
- And other database operation utilities

### kald-sqlserver-util

SQL Server database utilities and operations.

```bash
kald-sqlserver-util [-v] <subcommand>
```

## Architecture

This package is structured as follows:

- `src/kald_devops/` - Main package directory
  - `devops.py` - Phoenix pipeline and GitHub workflow management
  - `postgres_util.py` - PostgreSQL utilities
  - `sqlserver_util.py` - SQL Server utilities

## CI/CD Workflows

The repository includes GitHub Actions workflows for each major operation:

- `.github/workflows/build_pipelines.yml` - Trigger Azure DevOps builds
- `.github/workflows/deploy_pipelines.yml` - Trigger Azure DevOps deployments
- `.github/workflows/apply_terraform.yml` - Apply Terraform configurations
- `.github/workflows/apply_flyway.yml` - Apply Flyway migrations
- `.github/workflows/postgres_apply_pr.yml` - Apply PostgreSQL migrations
- `.github/workflows/publish.yml` - Publish package to GitHub Packages

## Requirements

- Python 3.9+
- `requests`
- `prettytable`
- `slack-sdk`

## Development

### Running Tests

```bash
python -m pytest tests/
```

### Building the Package

```bash
pip install build
python -m build
```

## Publishing

The package is automatically published to GitHub Packages on new releases. Manual publishing:

```bash
pip install twine
python -m build
twine upload --repository github dist/*
```

## Environment Variables

See the documentation for each subcommand in the utility scripts for required environment variables.

## License

MIT
