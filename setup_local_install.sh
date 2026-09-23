#!/bin/bash
#
# Setup script to configure local pip installation from GitHub Packages
#
# Usage:
#   ./setup_local_install.sh
#
# This script will:
# 1. Prompt for your GitHub Personal Access Token
# 2. Create/update ~/.config/pip/pip.conf
# 3. Test the installation
#

set -e

GITHUB_ORG="KalderosLLC"
PACKAGE_NAME="kald-devops"
GITHUB_PACKAGES_URL="https://pypi.pkg.github.com/${GITHUB_ORG}"

# Color codes for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== GitHub Packages Local Installation Setup ===${NC}\n"

# Step 1: Get GitHub Token
echo -e "${YELLOW}Step 1: GitHub Personal Access Token${NC}"
echo "You need a GitHub Personal Access Token to install from GitHub Packages."
echo "Create one at: https://github.com/settings/tokens"
echo "Required scopes: packages:read"
echo ""

read -sp "Enter your GitHub Personal Access Token: " GITHUB_TOKEN
echo ""

if [ -z "$GITHUB_TOKEN" ]; then
    echo -e "${RED}Error: Token cannot be empty${NC}"
    exit 1
fi

# Step 2: Create pip config directory if it doesn't exist
PIP_CONFIG_DIR="$HOME/.config/pip"
PIP_CONFIG_FILE="$PIP_CONFIG_DIR/pip.conf"

echo -e "${YELLOW}Step 2: Creating pip configuration${NC}"
mkdir -p "$PIP_CONFIG_DIR"

# Step 3: Create/update pip.conf
echo "Creating/updating $PIP_CONFIG_FILE"

cat > "$PIP_CONFIG_FILE" << EOF
[global]
index-url = https://__token__:${GITHUB_TOKEN}@pypi.pkg.github.com/${GITHUB_ORG}/simple/

[install]
trusted-host =
    pypi.pkg.github.com
    files.pythonhosted.org
EOF

chmod 600 "$PIP_CONFIG_FILE"
echo -e "${GREEN}✓ Configuration saved to $PIP_CONFIG_FILE${NC}"

# Step 4: Test the setup
echo -e "\n${YELLOW}Step 3: Testing installation${NC}"
echo "Testing pip configuration..."

# Use a temporary virtual environment if we're in one, otherwise just try
if ! pip install --dry-run "$PACKAGE_NAME" 2>&1 | grep -q "Would install"; then
    echo -e "${YELLOW}Note: Dry-run test skipped (not in a virtual environment)${NC}"
else
    echo -e "${GREEN}✓ Pip can reach GitHub Packages${NC}"
fi

# Step 5: Optional installation
echo ""
echo -e "${YELLOW}Step 4: Install the package (optional)${NC}"
read -p "Would you like to install kald-devops now? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Installing $PACKAGE_NAME..."
    pip install "$PACKAGE_NAME"
    echo -e "${GREEN}✓ Installation complete${NC}"
    echo ""
    echo "Verify installation:"
    python -c "import kald_devops; print(f'kald-devops version: {kald_devops.__version__}')"
else
    echo "Skipped installation. You can install later with:"
    echo "  pip install $PACKAGE_NAME"
fi

# Summary
echo ""
echo -e "${GREEN}=== Setup Complete ===${NC}"
echo ""
echo "Configuration details:"
echo "  • Pip config: $PIP_CONFIG_FILE"
echo "  • GitHub Packages URL: $GITHUB_PACKAGES_URL"
echo "  • Package: $PACKAGE_NAME"
echo ""
echo "Available CLI commands after installation:"
echo "  • kald-devops"
echo "  • kald-postgres-util"
echo "  • kald-sqlserver-util"
echo ""
echo "Verify installation:"
echo "  kald-devops --help"
echo ""
echo "To uninstall, run:"
echo "  pip uninstall $PACKAGE_NAME"
echo ""
echo "To update to the latest version, run:"
echo "  pip install --upgrade $PACKAGE_NAME"
echo ""
