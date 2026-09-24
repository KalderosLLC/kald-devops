#!/bin/bash
#
# Setup/remove branch protection for main branch
#
# Usage: ./setup_branch_protection.sh [-r|--remove]
#

set -e

REPO="KalderosLLC/kald-devops"
BRANCH="main"
REMOVE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    -r|--remove)
      REMOVE=true
      shift
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

if [[ "$REMOVE" == true ]]; then
  echo "Removing branch protection from $REPO / $BRANCH"
else
  echo "Setting up branch protection for $REPO / $BRANCH"
fi
echo ""

if [[ "$REMOVE" == true ]]; then
  echo "Removing branch protection..."

  gh api \
    -X DELETE \
    "repos/$REPO/branches/$BRANCH/protection"

  echo ""
  echo "✅ Branch protection removed!"
  echo ""
  echo "Verify at: https://github.com/$REPO/settings/branches"
else
  # Create a temporary JSON file for the API request
  TEMP_JSON=$(mktemp)
  trap "rm -f $TEMP_JSON" EXIT

  # Write the branch protection configuration to JSON file
  cat > "$TEMP_JSON" << 'EOF'
{
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "required_status_checks": null,
  "enforce_admins": true,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true,
  "required_linear_history": false,
  "allow_auto_merge": false,
  "required_deployments": false
}
EOF

  echo "Applying branch protection..."

  # Apply the protection using the JSON file
  gh api \
    --input "$TEMP_JSON" \
    -X PUT \
    "repos/$REPO/branches/$BRANCH/protection"

  echo ""
  echo "✅ Branch protection configured!"
  echo ""
  echo "Settings applied to '$BRANCH':"
  echo "  ✓ Require 1 pull request review"
  echo "  ✓ Dismiss stale approvals"
  echo "  ✓ Enforce admins"
  echo "  ✓ Prevent force pushes"
  echo "  ✓ Prevent deletions"
  echo "  ✓ Require conversation resolution"
  echo ""
  echo "Verify at: https://github.com/$REPO/settings/branches"
fi
