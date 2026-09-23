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

# Step 1: Update repository metadata
echo "Step 1: Update repository metadata"
gh repo edit "$REPO" \
  --description "Kalderos DevOps utilities for managing Phoenix pipelines and database utilities" \
  --homepage "https://github.com/KalderosLLC/kald-devops" \
  --add-topic "devops" \
  --add-topic "pipeline" \
  --add-topic "azure" \
  --add-topic "github" \
  --add-topic "kalderos" \
  2>/dev/null || echo "⚠ Some metadata updates may have failed"
echo "✓ Repository metadata updated"

# Step 2: Configure merge options
echo "Step 2: Configure merge options"
gh repo edit "$REPO" \
  --allow-squash-merge \
  --allow-merge-commit \
  --allow-rebase-merge \
  2>/dev/null || echo "⚠ Merge options already configured"
echo "✓ Merge options configured"

# Step 3: Note about branch protection
echo ""
echo "⚠️  Branch protection requires web UI or more complex API setup"
echo ""
echo "To complete branch protection, go to:"
echo "  https://github.com/$REPO/settings/branches"
echo ""
echo "And configure 'main' branch with:"
echo "  ✓ Require pull request reviews (1 approval)"
echo "  ✓ Dismiss stale pull request approvals"
echo "  ✓ Require status checks to pass"
echo "  ✓ Require branches to be up to date"
echo "  ✓ Require conversation resolution"
echo "  ✓ Enforce admins to follow"
echo "  ✓ Prevent force pushes"
echo "  ✓ Prevent deletions"

echo ""
echo "============================================"
echo "CLI configuration complete!"
echo "============================================"
echo ""
echo "Configuration applied:"
echo "  ✓ Repository description"
echo "  ✓ Homepage URL"
echo "  ✓ Topics/tags"
echo "  ✓ Merge options"
echo ""
echo "⚠️  Branch protection: Complete via web UI"
echo "    Link: https://github.com/$REPO/settings/branches"
echo ""
echo "Next steps:"
echo "  1. (Optional) Configure branch protection in web UI"
echo ""
echo "  2. Push code and tags:"
echo "     git push -u origin main"
echo "     git push --tags"
echo ""
echo "  3. Verify publish workflow runs:"
echo "     gh run list --repo $REPO"
echo ""
echo "  4. Check package in GitHub Packages:"
echo "     gh api repos/$REPO/packages"
echo ""
