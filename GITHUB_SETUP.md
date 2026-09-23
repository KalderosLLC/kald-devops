# GitHub Repository Setup - CLI Commands

Quick CLI commands to configure the GitHub repository after creation.

## Prerequisites

```bash
# Install GitHub CLI if needed
brew install gh  # macOS
# or visit: https://cli.github.com/

# Authenticate with GitHub
gh auth login
```

---

## Option 1: Automated Setup (Recommended)

Run the configuration script:

```bash
./configure_github_repo.sh
```

This handles all settings in one command.

---

## Option 2: Manual CLI Commands

### 1. Create the Repository on GitHub

```bash
gh repo create kald-devops \
  --public \
  --source=. \
  --remote=origin \
  --push
```

Or use the web UI: https://github.com/new

### 2. Push Code to GitHub

```bash
cd /home/david.lindsay/projects/github.com/KalderosLLC/kald-devops

# Add remote (if not already set)
git remote add origin https://github.com/KalderosLLC/kald-devops.git

# Push main branch
git push -u origin main

# Push tags
git push --tags
```

### 3. Configure Repository Settings

```bash
REPO="KalderosLLC/kald-devops"

# Require pull request reviews before merging
gh repo edit $REPO --require-code-review

# Require status checks to pass before merging
gh repo edit $REPO --require-status-checks

# Require branches to be up to date
gh repo edit $REPO --require-branches-up-to-date

# Dismiss stale pull request approvals
gh repo edit $REPO --dismiss-stale-reviews

# Protect main branch
gh api repos/$REPO/branches/main/protection \
  -X PUT \
  -f required_status_checks='{"strict":true}' \
  -f enforce_admins=true \
  -f allow_force_pushes=false
```

---

## Verification Commands

### View Repository Settings

```bash
REPO="KalderosLLC/kald-devops"

# View basic repo info
gh repo view $REPO

# View branch protection rules
gh api repos/$REPO/branches/main/protection

# View pull request requirements
gh repo view $REPO --json pullRequestTemplate
```

### Check Workflow Status

```bash
REPO="KalderosLLC/kald-devops"

# List recent workflow runs
gh run list --repo $REPO --limit 10

# Watch latest run
gh run list --repo $REPO --limit 1 --watch

# View specific run details
gh run view <run-id> --repo $REPO
```

### Check Package Publishing

```bash
REPO="KalderosLLC/kald-devops"

# List all packages
gh api repos/$REPO/packages

# Get package details
gh api repos/$REPO/packages/kald-devops
```

### Check Releases

```bash
REPO="KalderosLLC/kald-devops"

# List releases
gh release list --repo $REPO

# View specific release
gh release view v0.1.0 --repo $REPO

# Download release assets
gh release download v0.1.0 --repo $REPO
```

---

## Complete Setup Workflow

```bash
# 1. Navigate to repo
cd /home/david.lindsay/projects/github.com/KalderosLLC/kald-devops

# 2. Create repo on GitHub and push
git remote add origin https://github.com/KalderosLLC/kald-devops.git
git push -u origin main
git push --tags

# 3. Configure repository settings
REPO="KalderosLLC/kald-devops"
gh repo edit $REPO --require-code-review
gh repo edit $REPO --require-status-checks
gh repo edit $REPO --require-branches-up-to-date
gh repo edit $REPO --dismiss-stale-reviews

# 4. Verify workflow runs
gh run list --repo $REPO --limit 5

# 5. Check if package is available
gh api repos/$REPO/packages

# 6. Verify releases
gh release list --repo $REPO
```

---

## Adding Secrets (If Needed)

Add secrets for workflow operations:

```bash
REPO="KalderosLLC/kald-devops"

# Add Azure DevOps token
gh secret set AZURE_DEVOPS_EXT_PAT \
  --repo $REPO \
  --body "your-azure-devops-pat"

# Add Slack webhook (optional)
gh secret set SLACK_WEBHOOK \
  --repo $REPO \
  --body "https://hooks.slack.com/services/..."

# Add PostgreSQL credentials (optional)
gh secret set POSTGRES_PROD_HOST \
  --repo $REPO \
  --body "postgres.example.com"

gh secret set POSTGRES_PROD_USER \
  --repo $REPO \
  --body "db_user"

gh secret set POSTGRES_PROD_PASSWORD \
  --repo $REPO \
  --body "secure_password"
```

List secrets:

```bash
gh secret list --repo KalderosLLC/kald-devops
```

---

## Debugging

### View Full Workflow Run Logs

```bash
# Get latest run ID
RUN_ID=$(gh run list --repo KalderosLLC/kald-devops --limit 1 --json databaseId --jq '.[0].databaseId')

# View logs
gh run view $RUN_ID --repo KalderosLLC/kald-devops --log
```

### Monitor Workflow in Real-Time

```bash
# Watch latest run as it progresses
gh run watch --repo KalderosLLC/kald-devops
```

### Check API Rate Limits

```bash
gh api rate_limit
```

---

## Useful gh Commands Reference

| Command | Purpose |
|---------|---------|
| `gh repo view` | View repository details |
| `gh repo edit` | Edit repository settings |
| `gh run list` | List workflow runs |
| `gh run view` | View run details and logs |
| `gh run watch` | Watch run in real-time |
| `gh release list` | List releases |
| `gh release view` | View release details |
| `gh release download` | Download release assets |
| `gh secret set` | Add a secret |
| `gh secret list` | List secrets |
| `gh api` | Make raw API calls |

---

## Next Steps

After configuration:

1. ✅ Push code and tags
2. ✅ Verify workflow runs successfully
3. ✅ Check package appears in GitHub Packages
4. ✅ Test local installation
5. ✅ Share setup instructions with team

See: [QUICKSTART.md](QUICKSTART.md) and [DISTRIBUTION.md](DISTRIBUTION.md)
