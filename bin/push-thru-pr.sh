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

After a successful merge, the current branch is retired and a fresh branch named
feature-<epoch-millis> is cut from the just-updated base branch, pushed, and checked
out -- so you always start the next round of work from a base branch that's actually
current, instead of a long-lived branch that can silently drift out of sync with it.

${BLUE}EXAMPLES:${NC}
  # Create PR from current branch to main (auto-merge with a real merge commit),
  # then rotate onto a new feature-<epoch-millis> branch
  $(basename "$0")

  # Create PR to a different base branch
  $(basename "$0") -B release-1.0

  # Create PR for review only (don't auto-merge, don't rotate branches)
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
  echo "  git checkout -b feature-\$(date +%s%3N)"
  echo "  # Make your changes..."
  echo "  $(basename "$0")"
  echo ""
  exit 1
fi

echo -e "${BLUE}Creating PR from $CURRENT_BRANCH -> $BASE_BRANCH${NC}\n"

# Stage and commit any uncommitted changes
if [[ -n $(git status -s) ]]; then
  echo "Committing changes..."
  git add -A
  git commit -m "$PR_TITLE"
fi

# Push current branch
echo "Pushing $CURRENT_BRANCH to origin..."
git push -u origin "$CURRENT_BRANCH" 2>/dev/null || git push origin "$CURRENT_BRANCH"

# Create the PR
echo -e "\n${BLUE}Creating pull request...${NC}"
if [[ -n "$DESCRIPTION" ]]; then
  PR_URL=$(gh pr create --title "$PR_TITLE" --body "$DESCRIPTION" --base "$BASE_BRANCH")
else
  PR_URL=$(gh pr create --title "$PR_TITLE" --base "$BASE_BRANCH")
fi

echo -e "${GREEN}PR created: $PR_URL${NC}\n"

# Merge if requested
if [[ "$AUTO_MERGE" == true ]]; then
  echo -e "${BLUE}Merging PR...${NC}"

  if [[ "$SQUASH" == true ]]; then
    gh pr merge "$PR_URL" --admin --squash
  else
    gh pr merge "$PR_URL" --admin --merge
  fi

  echo -e "\n${GREEN}PR merged successfully!${NC}"
  echo "Changes merged to $BASE_BRANCH"

  # Retire $CURRENT_BRANCH and rotate onto a fresh feature-<epoch-millis> branch cut from a
  # freshly-fetched $BASE_BRANCH. This is what actually prevents this branch from ever
  # drifting out of sync with $BASE_BRANCH again -- there's no long-lived branch left to
  # drift; every round of work starts from wherever $BASE_BRANCH actually is right now.
  # $CURRENT_BRANCH's remote ref is expected to be deleted automatically by GitHub on merge
  # (repo setting); the local branch is deleted here since it's now fully merged.
  if [[ "$CURRENT_BRANCH" != "$BASE_BRANCH" ]]; then
    NEW_BRANCH="feature-$(date +%s%3N)"
    echo -e "\n${BLUE}Rotating onto $NEW_BRANCH (from $BASE_BRANCH)...${NC}"
    git fetch origin "$BASE_BRANCH"
    git checkout -b "$NEW_BRANCH" "origin/$BASE_BRANCH"
    git push -u origin "$NEW_BRANCH"
    git branch -d "$CURRENT_BRANCH" 2>/dev/null || true
    echo -e "${GREEN}Now on $NEW_BRANCH, tracking origin/$NEW_BRANCH${NC}"
  fi
else
  echo -e "${GREEN}PR ready for review${NC}"
  echo "Review at: $PR_URL"
fi
