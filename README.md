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

### Local Setup

Clone the repository and install in editable mode:

```bash
git clone https://github.com/KalderosLLC/kald-devops.git
cd kald-devops
pip install -e .
```

This installs the package in "editable" mode, so changes to source code are immediately reflected without reinstalling.

### Develop & Test Locally

Edit source files in `src/kald_devops/`:
- `devops.py` - Main devops CLI
- `postgres_util.py` - PostgreSQL utilities
- `sqlserver_util.py` - SQL Server utilities
- `__init__.py` - Package metadata and version

Test your changes immediately:

```bash
# Test main CLI
kald-devops --help
kald-devops usage

# Test PostgreSQL utilities
kald-postgres-util --help

# Test SQL Server utilities
kald-sqlserver-util --help
```

### Running Tests

```bash
python -m pytest tests/
```

### Building a Local Wheel

Build a wheel matching what gets published:

```bash
pip install build
python -m build --wheel
```

The wheel is created in `dist/kald_devops-VERSION-py3-none-any.whl`

## Publishing

### Automatic Publishing

The package is automatically published to GitHub Pages on tag pushes:

1. Update version in `pyproject.toml`:
   ```toml
   version = "0.1.3"
   ```

2. Commit and create a tag:
   ```bash
   git add pyproject.toml
   git commit -m "Release v0.1.3"
   git tag v0.1.3
   git push origin main
   git push origin v0.1.3
   ```

3. GitHub Actions automatically:
   - Builds the wheel
   - Creates a GitHub Release with the wheel as an asset
   - Publishes to GitHub Pages PyPI index at `https://kalderosllc.github.io/kald-devops/simple/`

### Verifying Published Package

After publishing, verify the package is available:

```bash
# Check available versions
pip index versions kald-devops --index-url https://kalderosllc.github.io/kald-devops/simple/

# Install specific version
pip install kald-devops==0.1.3 --index-url https://kalderosllc.github.io/kald-devops/simple/
```

## Environment Variables

See the documentation for each subcommand in the utility scripts for required environment variables.

## License

MIT
