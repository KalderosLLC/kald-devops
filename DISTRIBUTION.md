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

**Two locations:**

1. **GitHub Releases** (Direct Download)
   - **URL:** `https://github.com/KalderosLLC/kald-devops/releases`
   - **Package:** Wheel attached to each release
   - **Format:** `kald_devops-0.1.0-py3-none-any.whl`

2. **GitHub Pages** (PyPI-Compatible Index)
   - **URL:** `https://KalderosLLC.github.io/kald-devops/simple/`
   - **Purpose:** Enables `pip install` and `pip install --upgrade`
   - **Authentication:** None required (public)

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

**None!** The GitHub Pages index is public and requires no authentication.

### Option 1: Automated Setup (Recommended)

Run the setup script:

```bash
./configure_local_pip.sh
```

This creates `~/.config/pip/pip.conf` automatically.

Then install:
```bash
pip install kald-devops
```

### Option 2: Manual Configuration

Create or edit `~/.config/pip/pip.conf`:

```ini
[install]
index-url = https://KalderosLLC.github.io/kald-devops/simple/

[global]
index-url = https://KalderosLLC.github.io/kald-devops/simple/
```

Then install:
```bash
pip install kald-devops
```

### Option 3: One-Time Install (No Configuration)

```bash
pip install kald-devops \
  --index-url https://KalderosLLC.github.io/kald-devops/simple/
```

### Option 4: Via Requirements File

Create `requirements.txt`:
```txt
-i https://KalderosLLC.github.io/kald-devops/simple/
kald-devops
```

Then:
```bash
pip install -r requirements.txt
```

### Option 5: Docker / Container Setup

In a `Dockerfile`:
```dockerfile
FROM python:3.11-slim

RUN pip install kald-devops \
  --index-url https://KalderosLLC.github.io/kald-devops/simple/
```

Build:
```bash
docker build -t my-app .
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

### "Index not available yet"

**Cause:** No releases published yet or GitHub Pages not enabled

**Solution:**
```bash
# Wait for the first release to be published
# GitHub Pages is auto-enabled; may take 1-2 minutes to activate

# Check if index is available
curl https://KalderosLLC.github.io/kald-devops/simple/kald-devops/
```

### "Package not found"

**Cause:** Package hasn't been released yet or wrong index URL

**Solution:**
```bash
# Check available versions
pip index versions kald-devops

# Verify index URL is correct
echo $PYTHONPATH
cat ~/.config/pip/pip.conf
```

### SSL Certificate Issues

**Cause:** Corporate proxy or firewall intercepting HTTPS

**Solution:**
```bash
# Temporarily disable SSL verification (NOT recommended for production)
pip install kald-devops --trusted-host github.com

# OR: Add corporate certificate to pip trust store
pip install --cert /path/to/cert.pem kald-devops
```

### "Requirement already satisfied" when upgrading

**Cause:** Version not newer than installed

**Solution:**
```bash
# Force reinstall latest
pip install --upgrade --force-reinstall kald-devops
```

### Reset pip configuration

**To revert to default pip behavior:**
```bash
rm ~/.config/pip/pip.conf
# Or restore from backup
cp ~/.config/pip/pip.conf.backup.* ~/.config/pip/pip.conf
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
| **Release Location** | GitHub Releases |
| **PyPI Index** | GitHub Pages: `https://KalderosLLC.github.io/kald-devops/simple/` |
| **Authentication** | None (public index) |
| **Setup Script** | Run `./configure_local_pip.sh` |
| **Installation** | `pip install kald-devops` |
| **Upgrade** | `pip install --upgrade kald-devops` |
| **Trigger** | Tag creation (e.g., `git tag v0.2.0`) |
| **Version Source** | `pyproject.toml` |
| **CLI Entry Points** | `kald-devops`, `kald-postgres-util`, `kald-sqlserver-util` |
