# Quick Start Guide

Get `kald-devops` installed and running in minutes.

## 1. Configure Local Pip (One-Time Setup)

Run the setup script:

```bash
./configure_local_pip.sh
```

This script:
1. Creates `~/.config/pip/pip.conf`
2. Points pip to the GitHub Pages PyPI index
3. Enables automatic upgrades with `pip install --upgrade`
4. Backs up your existing config (if any)

---

## 2. Install kald-devops

After running the setup script:

```bash
# Install latest version
pip install kald-devops

# Upgrade to latest anytime
pip install --upgrade kald-devops

# Install specific version
pip install kald-devops==0.1.0
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

## 5. Troubleshooting

### "Index not available yet"

**Problem:** Script says index may not be available

**Solution:** This is normal if no releases have been published. Try:
```bash
pip install kald-devops
```

If it fails, the first release is still being built. Check back in a minute.

### "No module named 'kald_devops'"

**Problem:** Package didn't install correctly

**Solution:**
```bash
pip install --upgrade --force-reinstall kald-devops
```

### "Cannot find module requests/prettytable/slack_sdk"

**Problem:** Dependencies not installed

**Solution:**
```bash
pip install requests prettytable slack-sdk
```

### Reset pip configuration

To revert the pip setup:
```bash
rm ~/.config/pip/pip.conf
# Or restore from backup if one was created
```

---

## Next Steps

- **Read full documentation:** See [DISTRIBUTION.md](DISTRIBUTION.md)
- **View CLI usage:** Run `kald-devops usage`
- **Set up GitHub:** Use [GITHUB_SETUP.md](GITHUB_SETUP.md)
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
