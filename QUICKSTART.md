# Quick Start Guide

Get `kald-devops` installed and running in minutes.

## 1. Get a GitHub Personal Access Token

Create a token at: https://github.com/settings/tokens

**Required scopes:**
- ✅ `packages:read` - to install the package
- ✅ `packages:write` - to publish (if you publish releases)

**Save your token somewhere safe** — you'll use it for authentication.

---

## 2. Choose Your Installation Method

### Option A: Automated Setup (Recommended)

Run the setup script:

```bash
./setup_local_install.sh
```

Follow the prompts to:
1. Enter your GitHub token
2. Configure pip automatically
3. Optionally install the package

---

### Option B: Manual Configuration

Create `~/.config/pip/pip.conf`:

```ini
[global]
index-url = https://__token__:YOUR_GITHUB_PAT@pypi.pkg.github.com/KalderosLLC/simple/
```

Replace `YOUR_GITHUB_PAT` with your actual token.

Then install:

```bash
pip install kald-devops
```

---

### Option C: One-Time Install (No Configuration)

```bash
pip install kald-devops \
  --index-url https://__token__:YOUR_GITHUB_PAT@pypi.pkg.github.com/KalderosLLC/simple/
```

---

### Option D: Docker

Build the image with your token:

```bash
docker build \
  --build-arg GITHUB_TOKEN=$GITHUB_TOKEN \
  -f Dockerfile.example \
  -t kald-devops:latest \
  .
```

Run a command:

```bash
docker run \
  -e AZURE_DEVOPS_EXT_PAT=$AZURE_DEVOPS_EXT_PAT \
  -e GITHUB_TOKEN=$GITHUB_TOKEN \
  kald-devops:latest \
  list_repositories
```

---

## 3. Verify Installation

After installation, verify everything works:

```bash
# Check version
python -c "import kald_devops; print(kald_devops.__version__)"

# View help for main CLI
kald-devops --help

# View help for other CLIs
kald-postgres-util --help
kald-sqlserver-util --help
```

---

## 4. Use the Tools

### Example 1: List Repositories

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxx
kald-devops list_repositories
```

### Example 2: Build Pipelines

```bash
export AZURE_DEVOPS_EXT_PAT=xxxxxxxxxxxx
export PIPELINES="123,456"
export BRANCH_OR_TAG="v1.0.0"

kald-devops build_pipelines
```

### Example 3: Deploy Pipelines

```bash
export PIPELINES="KalderosLLC/phoenix"
export ENVIRONMENTS="Stage,Prod"
export BRANCH_OR_TAG="v1.0.0"
export AZURE_DEVOPS_EXT_PAT=xxxxxxxxxxxx

kald-devops deploy_pipelines
```

---

## 5. Update to Latest Version

```bash
pip install --upgrade kald-devops
```

---

## 6. Troubleshooting

### "401 Unauthorized" Error

**Problem:** Token is invalid or expired

**Solution:**
1. Create a new token at https://github.com/settings/tokens
2. Update your `~/.config/pip/pip.conf` or run `setup_local_install.sh` again

### "No module named 'kald_devops'"

**Problem:** Package didn't install correctly

**Solution:**
```bash
pip install --upgrade --force-reinstall kald-devops \
  --index-url https://__token__:YOUR_GITHUB_PAT@pypi.pkg.github.com/KalderosLLC/simple/
```

### "Cannot find module requests/prettytable/slack_sdk"

**Problem:** Dependencies not installed

**Solution:**
```bash
pip install requests prettytable slack-sdk
```

### SSL Certificate Issues

**Problem:** Corporate proxy or firewall

**Solution:**
```bash
pip install --trusted-host pypi.pkg.github.com kald-devops
```

---

## Next Steps

- **Read full documentation:** See [DISTRIBUTION.md](DISTRIBUTION.md)
- **View CLI usage:** Run `kald-devops usage`
- **Set up GitHub Actions:** Use workflows in `.github/workflows/`
- **Development:** See [README.md](README.md#development)

---

## Common Commands

```bash
# List all repositories
kald-devops list_repositories

# List all environments
kald-devops list_environments

# List pipelines
kald-devops list_pipelines

# Tag a repository
kald-devops tag_repository

# Build pipelines
kald-devops build_pipelines

# Deploy pipelines
kald-devops deploy_pipelines

# Apply Terraform
kald-devops apply_terraform

# Apply Flyway migrations
kald-devops apply_flyway

# PostgreSQL utilities
kald-postgres-util apply_pr

# SQL Server utilities
kald-sqlserver-util --help
```

---

## Getting Help

1. **View tool help:** `kald-devops --help`
2. **View subcommand usage:** `kald-devops <subcommand> --help`
3. **Check GitHub Issues:** https://github.com/KalderosLLC/kald-devops/issues
4. **Read documentation:** [DISTRIBUTION.md](DISTRIBUTION.md), [README.md](README.md)

---

## Key Files

| File | Purpose |
|------|---------|
| `.pypirc.example` | Example ~/.pypirc configuration |
| `.pip.conf.example` | Example pip configuration |
| `setup_local_install.sh` | Automated setup script |
| `Dockerfile.example` | Docker container example |
| `docker-compose.example.yml` | Docker Compose configuration |
| `requirements-github.txt` | Requirements file for pip |
| `DISTRIBUTION.md` | Detailed distribution guide |
| `README.md` | Full documentation |
