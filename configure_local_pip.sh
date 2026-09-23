#!/bin/bash
#
# Configure local pip to install kald-devops from GitHub Pages
#
# Usage:
#   ./configure_local_pip.sh
#
# This script:
# 1. Creates ~/.config/pip/pip.conf
# 2. Adds GitHub Pages as an extra index (searches PyPI first, then kald-devops)
# 3. Enables 'pip install --upgrade kald-devops' to pull latest automatically
#

set -e

GITHUB_ORG="kalderosllc"
REPO_NAME="kald-devops"
INDEX_URL="https://${GITHUB_ORG}.github.io/${REPO_NAME}/simple/"
PIP_CONFIG_DIR="$HOME/.config/pip"
PIP_CONFIG_FILE="$PIP_CONFIG_DIR/pip.conf"

# Color codes
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Configure Local Pip for kald-devops ===${NC}\n"

# Create pip config directory if it doesn't exist
echo -e "${YELLOW}Creating pip configuration directory${NC}"
mkdir -p "$PIP_CONFIG_DIR"

# Backup existing config if it exists
if [ -f "$PIP_CONFIG_FILE" ]; then
    echo "Backing up existing pip.conf..."
    cp "$PIP_CONFIG_FILE" "${PIP_CONFIG_FILE}.backup.$(date +%s)"
fi

# Create/update pip.conf
echo -e "${YELLOW}Configuring pip to use GitHub Pages index${NC}"

cat > "$PIP_CONFIG_FILE" << EOF
[install]
extra-index-url = ${INDEX_URL}

[global]
extra-index-url = ${INDEX_URL}
EOF

chmod 600 "$PIP_CONFIG_FILE"
echo -e "${GREEN}✓ Pip configuration saved to $PIP_CONFIG_FILE${NC}"

# Display configuration
echo ""
echo -e "${YELLOW}Configuration details:${NC}"
echo "  • Config file: $PIP_CONFIG_FILE"
echo "  • Index URL: $INDEX_URL"
echo "  • Package: $REPO_NAME"
echo ""

# Test pip configuration
echo -e "${YELLOW}Testing pip configuration${NC}"
if pip index versions "$REPO_NAME" 2>/dev/null | grep -q "Available versions"; then
    echo -e "${GREEN}✓ Pip can access the index${NC}"
else
    echo -e "${YELLOW}⚠ Pip index may not be available yet${NC}"
    echo "  (This is normal if no releases have been published)"
fi

# Summary
echo ""
echo -e "${GREEN}=== Setup Complete ===${NC}"
echo ""
echo "You can now use pip commands:"
echo ""
echo "  # Install kald-devops"
echo "  pip install $REPO_NAME"
echo ""
echo "  # Upgrade to latest version"
echo "  pip install --upgrade $REPO_NAME"
echo ""
echo "  # Install specific version"
echo "  pip install ${REPO_NAME}==0.1.0"
echo ""
echo "Verify installation:"
echo "  kald-devops --help"
echo "  kald-postgres-util --help"
echo "  kald-sqlserver-util --help"
echo ""

# Optional uninstall instructions
echo -e "${YELLOW}To revert this configuration:${NC}"
echo "  rm $PIP_CONFIG_FILE"
if ls "${PIP_CONFIG_FILE}".backup.* 1> /dev/null 2>&1; then
    echo "  Or restore from backup: cp ${PIP_CONFIG_FILE}.backup.* $PIP_CONFIG_FILE"
fi
echo ""
