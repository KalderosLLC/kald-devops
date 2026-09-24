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
  -m, --merge                 Automatically merge PR with admin override
  -s, --squash                Use squash merge (requires --merge)
  -h, --help                  Show this help message

${BLUE}EXAMPLES:${NC}
  # Create PR without auto-merge
  $(basename "$0") -t "My changes" -d "Description here"

  # Create and auto-merge
  $(basename "$0") -t "My changes" -d "Description" --merge --squash

  # Create with custom branch name
  $(basename "$0") -b "feature/my-feature" -t "My feature" --merge

USAGE
  exit 0
}

# Default values
BRANCH_NAME=""
PR_TITLE=""
DESCRIPTION=""
AUTO_MERGE=false
SQUASH=false

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
    -m|--merge)
      AUTO_MERGE=true
      shift
      ;;
    -s|--squash)
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

# Validate required arguments
if [[ -z "$PR_TITLE" ]]; then
  echo -e "${RED}Error: PR title is required (-t or --title)${NC}"
  usage
fi

# Auto-generate branch name if not provided
if [[ -z "$BRANCH_NAME" ]]; then
  TIMESTAMP=$(date +%s)
  BRANCH_NAME="pr/push-to-main-$TIMESTAMP"
fi

echo -e "${BLUE}Creating PR to merge into main...${NC}\n"

# Create feature branch from current main
echo "📌 Creating branch: $BRANCH_NAME"
git checkout -b "$BRANCH_NAME"

# Push the branch to origin
echo "📤 Pushing branch to origin..."
git push -u origin "$BRANCH_NAME"

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
else
  echo -e "${GREEN}✅ PR ready for review${NC}"
  echo "Review the PR and merge manually when ready"
fi
