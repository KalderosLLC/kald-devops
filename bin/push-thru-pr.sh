#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Function to display usage
usage() {
  cat << USAGE
${BLUE}Usage: $(basename "$0") [OPTIONS]${NC}

Push current branch and create a PR to main (or specified branch).

${BLUE}OPTIONS:${NC}
  -B, --base-branch BRANCH    Base branch for PR (default: main)
  -t, --title TITLE           PR title (default: "My work")
  -d, --description TEXT      PR description (default: "My Description")
  -f, --file FILE             Read description from file
  --no-merge                  Create PR without auto-merging (review only)
  --squash                    Squash-merge instead of a real merge commit (NOT recommended --
                               squash rewrites history, which breaks the ancestor relationship
                               between this branch and the base branch and causes spurious merge
                               conflicts on future PRs; see git history around 2026-09-26)
  -h, --help                  Show this help message

${BLUE}EXAMPLES:${NC}
  # Create PR from current branch to main (auto-merge with a real merge commit)
  $(basename "$0")

  # Create PR to develop-dlindsay
  $(basename "$0") -B develop-dlindsay

  # Create PR for review only (don't auto-merge)
  $(basename "$0") --no-merge

  # Custom title and description
  $(basename "$0") -t "Add new feature" -d "This adds X functionality"

USAGE
  exit 0
}

# Default values
PR_TITLE="My work"
DESCRIPTION="My Description"
BASE_BRANCH="main"
AUTO_MERGE=true
SQUASH=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    -B|--base-branch)
      BASE_BRANCH="$2"
      shift 2
      ;;
    -t|--title)
      PR_TITLE="$2"
      shift 2
      ;;
    -d|--description)
      DESCRIPTION="$2"
      shift 2
      ;;
    -f|--file)
      if [[ -f "$2" ]]; then
        DESCRIPTION=$(cat "$2")
      else
        echo -e "${RED}Error: File not found: $2${NC}"
        exit 1
      fi
      shift 2
      ;;
    --no-merge)
      AUTO_MERGE=false
      shift
      ;;
    --no-squash)
      # Kept for backwards compatibility -- this is now the default behavior.
      SQUASH=false
      shift
      ;;
    --squash)
      SQUASH=true
      shift
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo -e "${RED}Unknown option: $1${NC}"
      usage
      ;;
  esac
done

# Get current branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

# Prevent PRs from main to main
if [[ "$CURRENT_BRANCH" == "main" ]] && [[ "$BASE_BRANCH" == "main" ]]; then
  echo -e "${RED}Error: Cannot create PR from main to main${NC}"
  echo ""
  echo -e "${BLUE}To work on changes:${NC}"
  echo "  git checkout -b develop-dlindsay"
  echo "  # Make your changes..."
  echo "  $(basename "$0")"
  echo ""
  exit 1
fi

echo -e "${BLUE}Creating PR from $CURRENT_BRANCH → $BASE_BRANCH${NC}\n"

# Stage and commit any uncommitted changes
if [[ -n $(git status -s) ]]; then
  echo "📝 Committing changes..."
  git add -A
  git commit -m "$PR_TITLE"
fi

# Push current branch
echo "📤 Pushing $CURRENT_BRANCH to origin..."
git push -u origin "$CURRENT_BRANCH" 2>/dev/null || git push origin "$CURRENT_BRANCH"

# Create the PR
echo -e "\n${BLUE}Creating pull request...${NC}"
if [[ -n "$DESCRIPTION" ]]; then
  PR_URL=$(gh pr create --title "$PR_TITLE" --body "$DESCRIPTION" --base "$BASE_BRANCH")
else
  PR_URL=$(gh pr create --title "$PR_TITLE" --base "$BASE_BRANCH")
fi

echo -e "${GREEN}✅ PR created: $PR_URL${NC}\n"

# Merge if requested
if [[ "$AUTO_MERGE" == true ]]; then
  echo -e "${BLUE}Merging PR...${NC}"

  if [[ "$SQUASH" == true ]]; then
    gh pr merge "$PR_URL" --admin --squash
  else
    gh pr merge "$PR_URL" --admin --merge
  fi

  echo -e "\n${GREEN}✅ PR merged successfully!${NC}"
  echo "✓ Changes merged to $BASE_BRANCH"

  # Sync $CURRENT_BRANCH forward from $BASE_BRANCH so it never lags behind what it just fed
  # into. With a real (non-squash) merge, $BASE_BRANCH's new tip has $CURRENT_BRANCH's own
  # commits as an ancestor, so this is always a clean fast-forward -- it's what keeps the two
  # branches from diverging cycle over cycle. If --squash was used instead, this fast-forward
  # is expected to fail, since squashing creates a new commit with no shared history; that's
  # exactly the divergence squashing causes, not a bug in this sync step.
  if [[ "$CURRENT_BRANCH" != "$BASE_BRANCH" ]]; then
    echo -e "\n${BLUE}Syncing $CURRENT_BRANCH from $BASE_BRANCH...${NC}"
    git fetch origin "$BASE_BRANCH"
    if git merge --ff-only "origin/$BASE_BRANCH"; then
      git push origin "$CURRENT_BRANCH"
      echo -e "${GREEN}✅ $CURRENT_BRANCH is now in sync with $BASE_BRANCH${NC}"
    else
      echo -e "${RED}⚠ Could not fast-forward $CURRENT_BRANCH from $BASE_BRANCH.${NC}"
      if [[ "$SQUASH" == true ]]; then
        echo "  This is expected with --squash: it rewrites history, so $CURRENT_BRANCH can't"
        echo "  fast-forward from it. Consider dropping --squash to avoid this permanently."
      else
        echo "  $BASE_BRANCH may have moved (e.g. another PR merged) since this PR was created."
        echo "  Sync manually: git fetch origin $BASE_BRANCH && git merge origin/$BASE_BRANCH"
      fi
    fi
  fi

  echo "✓ You're still on: $CURRENT_BRANCH"
else
  echo -e "${GREEN}✅ PR ready for review${NC}"
  echo "Review at: $PR_URL"
fi
