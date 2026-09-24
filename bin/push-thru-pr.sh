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
  --no-squash                 Merge without squashing (keep all commits)
  -h, --help                  Show this help message

${BLUE}EXAMPLES:${NC}
  # Create PR from current branch to main (auto-merge with squash)
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
SQUASH=true

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
      SQUASH=false
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
    gh pr merge "$PR_URL" --admin
  fi

  echo -e "\n${GREEN}✅ PR merged successfully!${NC}"
  echo "✓ Changes merged to $BASE_BRANCH"
  echo "✓ You're still on: $CURRENT_BRANCH"
else
  echo -e "${GREEN}✅ PR ready for review${NC}"
  echo "Review at: $PR_URL"
fi
