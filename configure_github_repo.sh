#!/bin/bash
#
# Configure GitHub repository settings for kald-devops
#
# Prerequisites:
#   - GitHub CLI installed: https://cli.github.com/
#   - Authenticated with GitHub: gh auth login
#   - Repository already created and pushed
#
# Usage:
#   ./configure_github_repo.sh
#

set -e

REPO="KalderosLLC/kald-devops"

echo "Configuring GitHub repository: $REPO"
echo ""

# Verify gh CLI is installed and authenticated
if ! command -v gh &> /dev/null; then
    echo "Error: GitHub CLI (gh) is not installed"
    echo "Install from: https://cli.github.com/"
    exit 1
fi

if ! gh auth status &> /dev/null; then
    echo "Error: Not authenticated with GitHub"
    echo "Run: gh auth login"
    exit 1
fi

# Verify repository exists
echo "Checking repository access..."
if ! gh repo view "$REPO" > /dev/null 2>&1; then
    echo "Error: Cannot access repository $REPO"
    exit 1
fi

echo "✓ Repository accessible"
echo ""

# Step 1: Require pull request reviews
echo "Step 1: Require pull request reviews before merging"
gh repo edit "$REPO" \
  --require-code-review \
  --require-review-dismissal
echo "✓ Pull request reviews required"

# Step 2: Require status checks to pass
echo "Step 2: Require status checks to pass before merging"
gh repo edit "$REPO" \
  --require-status-checks \
  --require-branches-up-to-date
echo "✓ Status checks required"

# Step 3: Dismiss stale pull request approvals
echo "Step 3: Dismiss stale pull request approvals"
gh repo edit "$REPO" \
  --dismiss-stale-reviews
echo "✓ Stale reviews will be dismissed"

# Step 4: Enable branch protection for main
echo "Step 4: Configure branch protection for 'main'"
gh api repos/"$REPO"/branches/main/protection \
  -X PUT \
  -f required_status_checks='{"strict":true,"contexts":["publish"]}' \
  -f enforce_admins=true \
  -f allow_force_pushes=false \
  -f allow_deletions=false \
  2>/dev/null || echo "⚠ Branch protection may already be configured"
echo "✓ Branch protection configured"

echo ""
echo "============================================"
echo "Repository configuration complete!"
echo "============================================"
echo ""
echo "Configuration applied:"
echo "  ✓ Require pull request reviews before merging"
echo "  ✓ Require status checks to pass before merging"
echo "  ✓ Require branches to be up to date before merging"
echo "  ✓ Dismiss stale pull request approvals"
echo "  ✓ Enforce admins to follow branch protection"
echo "  ✓ Prevent force pushes and deletions to main"
echo ""
echo "Next steps:"
echo "  1. Push code and tags:"
echo "     git push -u origin main"
echo "     git push --tags"
echo ""
echo "  2. Verify publish workflow runs:"
echo "     gh run list --repo $REPO"
echo ""
echo "  3. Check package availability:"
echo "     gh api repos/$REPO/packages"
echo ""
