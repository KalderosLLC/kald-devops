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

Creates a PR from current branch, optionally merges it with admin override.

${BLUE}OPTIONS:${NC}
  -b, --branch BRANCH_NAME    Feature branch name (default: auto-generated)
  -t, --title TITLE           PR title (required)
  -d, --description TEXT      PR description (stdin if not provided)
  -f, --file FILE             Read description from file
  --no-merge                  Create PR without auto-merging (review only)
  --no-squash                 Merge without squashing (keep all commits)
  -h, --help                  Show this help message

${BLUE}EXAMPLES:${NC}
  # Standard: create and auto-merge with squash
  $(basename "$0") -t "My changes" -d "Description here"

  # Create PR for review only (no auto-merge)
  $(basename "$0") -t "My changes" -d "Description" --no-merge

  # Auto-merge without squashing
  $(basename "$0") -t "My changes" -d "Description" --no-squash

USAGE
  exit 0
}

# Default values (auto-merge with squash by default)
BRANCH_NAME=""
PR_TITLE=""
DESCRIPTION=""
AUTO_MERGE=true
SQUASH=true

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    -b|--branch)
      BRANCH_NAME="$2"
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

# Validate required arguments
if [[ -z "$PR_TITLE" ]]; then
  echo -e "${RED}Error: PR title is required (-t or --title)${NC}"
  usage
fi

# Detect if we started on main branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
STARTED_ON_MAIN=false
ALREADY_ON_FEATURE_BRANCH=false

if [[ "$CURRENT_BRANCH" == "main" ]]; then
  STARTED_ON_MAIN=true
else
  ALREADY_ON_FEATURE_BRANCH=true
fi

# Auto-generate branch name if not provided and on main
if [[ -z "$BRANCH_NAME" ]]; then
  if [[ "$STARTED_ON_MAIN" == true ]]; then
    TIMESTAMP=$(date +%s)
    BRANCH_NAME="feature/$(date +%s)"
  else
    BRANCH_NAME="$CURRENT_BRANCH"
  fi
fi

echo -e "${BLUE}Creating PR to merge into main...${NC}\n"

# If on main, stash changes and create feature branch
if [[ "$STARTED_ON_MAIN" == true ]]; then
  echo "📦 Stashing changes from main..."
  git stash

  echo "📌 Creating feature branch: $BRANCH_NAME"
  git checkout -b "$BRANCH_NAME"

  echo "📥 Applying stashed changes..."
  git stash pop

  # Stage and commit changes if any
  if [[ -n $(git status -s) ]]; then
    git add -A
    git commit -m "$PR_TITLE"
  fi
elif [[ "$ALREADY_ON_FEATURE_BRANCH" == true ]]; then
  # Already on feature branch - just verify changes are committed
  if [[ -n $(git status -s) ]]; then
    echo "⚠️  Uncommitted changes detected. Staging and committing..."
    git add -A
    git commit -m "$PR_TITLE"
  else
    echo "✓ Feature branch already has committed changes"
  fi
fi

# Push the branch to origin
echo "📤 Pushing branch to origin..."
git push -u origin "$BRANCH_NAME" 2>/dev/null || git push origin "$BRANCH_NAME"

# Create the PR
echo -e "\n${BLUE}Creating pull request...${NC}"
if [[ -n "$DESCRIPTION" ]]; then
  PR_URL=$(gh pr create --title "$PR_TITLE" --body "$DESCRIPTION" --base main)
else
  PR_URL=$(gh pr create --title "$PR_TITLE" --base main)
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
  echo "Main branch has been updated"

  # Clean up: return to main and sync only if we started on main
  if [[ "$STARTED_ON_MAIN" == true ]]; then
    echo -e "\n${BLUE}Cleaning up...${NC}"
    git checkout main
    git pull --no-edit origin main
    echo -e "${GREEN}✅ Local main synced with origin/main${NC}\n"
    git branch -vv
  else
    echo -e "\n${GREEN}✅ Done! Your branch is ready.${NC}"
  fi
else
  echo -e "${GREEN}✅ PR ready for review${NC}"
  echo "Review the PR and merge manually when ready"
fi
