#!/bin/bash
# =============================================================================
# E*TRADE Stock Monitor - Setup Script
# Installs Homebrew Python and creates a virtual environment
# =============================================================================

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  E*TRADE Stock Monitor - Setup${NC}"
echo -e "${GREEN}========================================${NC}"

# --- Step 1: Check/Install Homebrew ---
echo -e "\n${YELLOW}[1/5] Checking Homebrew...${NC}"
if ! command -v brew &> /dev/null; then
    echo "Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for Apple Silicon Macs
    if [[ -f "/opt/homebrew/bin/brew" ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
    fi
else
    echo -e "${GREEN}Homebrew already installed.${NC}"
fi

# --- Step 2: Install Python via Homebrew ---
echo -e "\n${YELLOW}[2/5] Installing Python via Homebrew...${NC}"
if brew list python@3.12 &> /dev/null; then
    echo -e "${GREEN}Python 3.12 already installed via Homebrew.${NC}"
else
    brew install python@3.12
fi

PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12"
echo -e "Using Python: ${GREEN}$($PYTHON_BIN --version)${NC}"

# --- Step 3: Navigate to project directory ---
echo -e "\n${YELLOW}[3/5] Setting up project directory...${NC}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"
echo "Project directory: $PROJECT_DIR"

# --- Step 4: Create virtual environment ---
echo -e "\n${YELLOW}[4/5] Creating virtual environment...${NC}"
if [[ -d ".venv" ]]; then
    echo -e "${YELLOW}Virtual environment already exists. Recreating...${NC}"
    rm -rf .venv
fi

$PYTHON_BIN -m venv .venv
source .venv/bin/activate
echo -e "${GREEN}Virtual environment created and activated.${NC}"
echo -e "Python: $(python --version)"
echo -e "pip: $(pip --version)"

# --- Step 5: Install dependencies ---
echo -e "\n${YELLOW}[5/5] Installing dependencies...${NC}"
pip install --upgrade pip
pip install -r requirements.txt

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  Setup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Next steps:"
echo "  1. Copy .env.example to .env and fill in your credentials:"
echo "     cp .env.example .env"
echo ""
echo "  2. Activate the virtual environment:"
echo "     source .venv/bin/activate"
echo ""
echo "  3. Run the monitor:"
echo "     python -m src.main"
echo ""
echo "  4. Or test a single run:"
echo "     python -m src.main --once"
