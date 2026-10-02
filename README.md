# kald-devops

[![Test](https://github.com/KalderosLLC/kald-devops/actions/workflows/test.yml/badge.svg)](https://github.com/KalderosLLC/kald-devops/actions/workflows/test.yml)
[![Publish](https://github.com/KalderosLLC/kald-devops/actions/workflows/publish.yml/badge.svg)](https://github.com/KalderosLLC/kald-devops/actions/workflows/publish.yml)

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
- `teams_release` - Prepare a release notification message for Microsoft Teams

### kald-postgres-util

PostgreSQL database utilities and operations.

```bash
kald-postgres-util [-v] <subcommand>
```

**Subcommands include:**
- `apply_pr` - Apply pending migrations or release procedures (exposed as a workflow)
- And other database operation utilities

### kald-sqlserver-util

SQL Server database utilities and operations. Requires `pyodbc` for database connectivity.

```bash
kald-sqlserver-util [--log-level {DEBUG|INFO|WARNING|ERROR|CRITICAL}] [--json] <subcommand>
```

**Subcommands:**
- `connect` - Validate connection to a SQL Server database (reads DB_SERVER, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_DRIVER from environment)
- `auth` - Display database role memberships as formatted tables or JSON

## Architecture

This package is structured as follows:

- `src/kald_devops/` - Main package directory
  - `devops.py` - Phoenix pipeline and GitHub workflow management
  - `postgres_util.py` - PostgreSQL utilities
  - `sqlserver_util.py` - SQL Server utilities

## CI/CD Workflows

The repository includes GitHub Actions workflows for each major operation:

- `.github/workflows/test.yml` - Run the unit test suite + coverage on every pull request and push to `main`
- `.github/workflows/publish.yml` - Run tests, then build and publish the package to GitHub Packages
- `.github/workflows/build_pipelines.yml` - Trigger Azure DevOps builds
- `.github/workflows/deploy_pipelines.yml` - Trigger Azure DevOps deployments
- `.github/workflows/apply_terraform.yml` - Apply Terraform configurations
- `.github/workflows/apply_flyway.yml` - Apply Flyway migrations
- `.github/workflows/postgres_apply_pr.yml` - Apply PostgreSQL migrations

`test.yml` and `publish.yml` are thin triggers around plain CLI commands (see
[Testing](#testing) below) -- neither one contains any test or build logic of
its own, so the same checks run identically on a laptop, in these GitHub
Actions workflows, or under any other CI provider.

### Branch Protection

`setup_branch_protection.sh` configures `main` to require the `test`
status check (from `.github/workflows/test.yml`) to pass, and the branch to
be up to date with `main`, before a pull request can be merged -- in addition
to requiring 1 approving review and blocking force pushes/deletions. Someone
with admin access to the repository runs it once:

```bash
setup_branch_protection.sh            # apply
setup_branch_protection.sh --remove   # remove
```

## Requirements

- Python 3.9+
- `requests`
- `prettytable`
- `pyodbc` (optional, only needed for `kald-sqlserver-util`)

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

### Testing

Tests are run with [pytest](https://docs.pytest.org/) and standardized via
[tox](https://tox.wiki/) so the exact same command runs locally and in CI,
regardless of CI provider:

```bash
pip install tox
tox -e test          # or just: tox
```

This installs the package with its `test` extra, runs the full suite with
coverage, and writes:

- `test-results/junit.xml` - machine-readable JUnit XML test report
- `test-results/coverage.xml` - Cobertura-format coverage report
- `test-results/htmlcov/index.html` - browsable HTML coverage report

Extra arguments after `--` are passed straight to pytest, e.g. to run a single file verbosely:

```bash
tox -e test -- -v tests/test_devops.py
```

`.github/workflows/test.yml` runs on every pull request and push to `main`;
it calls `tox -e test` and then archives `test-results/` (JUnit + coverage)
as a downloadable artifact on the run, and writes a human-readable summary
(pass/fail counts + per-file coverage table) directly to the run's Summary
page using `scripts/summarize_tests.py` and `coverage report --format=markdown`.

Without tox, the equivalent is:

```bash
pip install -e ".[test]"
pytest
```

### Building a Local Wheel

Build a wheel matching what gets published, via the standard PEP 517 `python -m build` entry point:

```bash
tox -e build
```

or, without tox:

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
