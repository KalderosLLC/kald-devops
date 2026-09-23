# Artifact Distribution Guide

## Overview

This document explains how the `kald-devops` package is built, published, and installed locally.

## Build & Artifact Pipeline

### 1. What Gets Built

When you push code or create a tag, the `publish.yml` workflow:
- **Builds the Python package** using `python -m build --wheel`
- **Produces a single artifact (wheel only):**
  - `kald_devops-0.1.0-py3-none-any.whl` (binary wheel)

This is located in the `dist/` directory after the build step.

**Why wheels only?** Wheels are pre-built binaries that install faster and more reliably than source distributions.

### 2. Where It Publishes

**GitHub Packages Registry** (PyPI-compatible)
- **Repository URL:** `https://pypi.pkg.github.com/KalderosLLC`
- **Package Name:** `kald-devops`
- **Scope:** Organization-scoped (`KalderosLLC`)

This is an organizational artifact repository that requires authentication to access.

### 3. Publication Triggers

The `publish.yml` workflow publishes on:
- **Any push to `main`** - publishes with current version (useful for development)
- **Tag pushes** (e.g., `git tag v1.0.0`) - creates a GitHub Release with the artifacts

### 4. Version Management

Version is controlled in `pyproject.toml`:
```toml
[project]
version = "0.1.0"
```

To bump the version:
```bash
# Edit pyproject.toml, then:
git add pyproject.toml
git commit -m "Bump version to 0.2.0"
git push
```

---

## Local Installation Setup

### Prerequisites

A GitHub Personal Access Token (PAT) with at least:
- `packages:read` scope (to install)
- `packages:write` scope (to publish)

**Create a token:** https://github.com/settings/tokens

### Option 1: One-Time Install with Token (Simplest)

```bash
pip install kald-devops \
  --index-url https://__token__:YOUR_GITHUB_PAT@pypi.pkg.github.com/KalderosLLC/simple/
```

Replace `YOUR_GITHUB_PAT` with your actual token.

### Option 2: Persistent Configuration (Recommended)

Create or edit `~/.pypirc`:

```ini
[distutils]
index-servers =
    pypi
    github

[github]
repository: https://pypi.pkg.github.com/KalderosLLC
username: __token__
password: YOUR_GITHUB_PAT
```

Then install using:
```bash
pip install kald-devops -i https://pypi.pkg.github.com/KalderosLLC/simple/
```

Or as a default index in `~/.config/pip/pip.conf`:

```ini
[global]
index-url = https://__token__:YOUR_GITHUB_PAT@pypi.pkg.github.com/KalderosLLC/simple/
```

Then simply:
```bash
pip install kald-devops
```

### Option 3: Environment Variables (CI/CD Friendly)

Set environment variables:
```bash
export GITHUB_TOKEN="YOUR_GITHUB_PAT"
```

Then create a pip config or use in scripts:
```bash
pip install kald-devops \
  --index-url https://__token__:${GITHUB_TOKEN}@pypi.pkg.github.com/KalderosLLC/simple/
```

### Option 4: Docker / Container Setup

In a `Dockerfile`:
```dockerfile
FROM python:3.11-slim

ARG GITHUB_TOKEN
RUN pip install kald-devops \
  --index-url https://__token__:${GITHUB_TOKEN}@pypi.pkg.github.com/KalderosLLC/simple/
```

Build with:
```bash
docker build --build-arg GITHUB_TOKEN=$GITHUB_TOKEN -t my-app .
```

---

## Verification

After installation, verify the package:

```bash
# Check version
python -c "import kald_devops; print(kald_devops.__version__)"

# Verify CLI commands are available
kald-devops --help
kald-postgres-util --help
kald-sqlserver-util --help
```

---

## Troubleshooting

### "401 Unauthorized" Error

**Cause:** Invalid or expired GitHub token

**Solution:**
```bash
# Create a new token: https://github.com/settings/tokens
# Verify token has packages:read scope
# Update your pip config with the new token
```

### "Package not found"

**Cause:** Package hasn't been published yet or version mismatch

**Solution:**
```bash
# Check if package is available on GitHub Packages
pip index versions kald-devops \
  --index-url https://__token__:YOUR_GITHUB_PAT@pypi.pkg.github.com/KalderosLLC/simple/

# View available versions
pip search kald-devops  # (limited search)
```

### SSL Certificate Issues

**Cause:** Corporate proxy or firewall intercepting HTTPS

**Solution:**
```bash
# Temporarily disable SSL verification (NOT recommended for production)
pip install kald-devops --trusted-host pypi.pkg.github.com

# OR: Add corporate certificate to pip trust store
pip install --cert /path/to/cert.pem kald-devops
```

### "Requirement already satisfied"

**Cause:** Package already installed from different source

**Solution:**
```bash
# Force reinstall from GitHub Packages
pip install --upgrade --force-reinstall kald-devops \
  --index-url https://__token__:YOUR_GITHUB_PAT@pypi.pkg.github.com/KalderosLLC/simple/
```

---

## Package Contents

The installed package includes:

```
kald_devops/
├── __init__.py           # Version and metadata
├── devops.py             # Phoenix pipeline management CLI
├── postgres_util.py      # PostgreSQL utilities CLI
├── sqlserver_util.py     # SQL Server utilities CLI
└── py.typed              # PEP 561 type hints marker
```

All three CLI commands are available as entry points:
- `kald-devops`
- `kald-postgres-util`
- `kald-sqlserver-util`

---

## Development Installation

For development, install from the repository:

```bash
git clone https://github.com/KalderosLLC/kald-devops.git
cd kald-devops
pip install -e .
```

This installs the package in editable mode, so changes to the source code are immediately reflected.

---

## Publishing a New Version

1. **Update version in `pyproject.toml`**
   ```toml
   version = "0.2.0"
   ```

2. **Create and push a tag**
   ```bash
   git add pyproject.toml
   git commit -m "Release v0.2.0"
   git tag v0.2.0
   git push && git push --tags
   ```

3. **GitHub Actions will automatically:**
   - Build the wheel and source distribution
   - Upload to GitHub Packages
   - Create a GitHub Release with the artifacts

4. **Users can then install:**
   ```bash
   pip install kald-devops==0.2.0 \
     --index-url https://__token__:$GITHUB_TOKEN@pypi.pkg.github.com/KalderosLLC/simple/
   ```

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Push to GitHub                           │
│                  (main branch or tag)                       │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
        ┌────────────────────────────┐
        │  GitHub Actions: publish   │
        │  .github/workflows/         │
        │  publish.yml               │
        └────────────┬───────────────┘
                     │
        ┌────────────▼──────────────┐
        │  Build Python Package     │
        │  - python -m build        │
        │  - Creates .whl + .tar.gz │
        └────────────┬──────────────┘
                     │
        ┌────────────▼──────────────────────┐
        │  Upload to GitHub Packages        │
        │  https://pypi.pkg.github.com/     │
        │  KalderosLLC/kald-devops/         │
        └────────────┬──────────────────────┘
                     │
        ┌────────────▼──────────────────────┐
        │  Create GitHub Release (on tags)  │
        │  - Attach artifacts              │
        │  - Generate release notes         │
        └────────────────────────────────────┘


┌──────────────────────────────────────────────────────────────┐
│              Local Installation                              │
│                                                              │
│  pip install kald-devops \                                  │
│    --index-url https://__token__:TOKEN@               │
│    pypi.pkg.github.com/KalderosLLC/simple/            │
│                                                              │
│  ▼                                                          │
│  GitHub Packages ──▶ Download wheel ──▶ Install to site   │
│  (requires auth)     from pypi.pkg     -packages/         │
└──────────────────────────────────────────────────────────────┘
```

---

## Key Takeaways

| Aspect | Details |
|--------|---------|
| **Artifact Type** | Python wheel (binary only) |
| **Repository** | GitHub Packages (PyPI-compatible) |
| **URL** | `https://pypi.pkg.github.com/KalderosLLC` |
| **Authentication** | GitHub Personal Access Token with `packages:read` |
| **Trigger** | Push to main, or tag creation |
| **Version Source** | `pyproject.toml` |
| **CLI Entry Points** | `kald-devops`, `kald-postgres-util`, `kald-sqlserver-util` |
