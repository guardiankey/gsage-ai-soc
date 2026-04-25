#!/usr/bin/env bash
# gSage AI — CLI Client launcher script
#
# Usage:
#   ./run-cli.sh

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  gSage AI — CLI Client                     ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════════╝${NC}"
echo

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo -e "${YELLOW}Virtual environment not found. Creating...${NC}"
    python3 -m venv .venv
fi

# Activate virtual environment
echo -e "${YELLOW}Activating virtual environment...${NC}"
source .venv/bin/activate

# Install/upgrade dependencies
if [ ! -f ".cli_deps_installed" ] || [ requirements-cli.txt -nt .cli_deps_installed ]; then
    echo -e "${YELLOW}Installing CLI dependencies...${NC}"
    pip install -q -r requirements-cli.txt
    touch .cli_deps_installed
fi

# Check for API key
if [ -z "$GSAGE_API_KEY" ]; then
    echo -e "${RED}ERROR: GSAGE_API_KEY environment variable not set${NC}"
    echo
    echo "Please set your API key:"
    echo "  export GSAGE_API_KEY='your-api-key-here'"
    echo
    echo "Optional environment variables:"
    echo "  export GSAGE_API_HOST='http://localhost:8000'  # default"
    echo "  export GSAGE_DEBUG='true'                       # enable debug"
    echo
    exit 1
fi

# Display configuration
echo -e "${GREEN}Configuration:${NC}"
echo "  API Host: ${GSAGE_API_HOST:-http://localhost:8000}"
echo "  API Key: ${GSAGE_API_KEY:0:8}..."
[ -n "$GSAGE_CONVERSATION_ID" ] && echo "  Conversation: $GSAGE_CONVERSATION_ID"
[ "$GSAGE_DEBUG" = "true" ] && echo "  Debug: enabled"
echo

# Run the CLI
python -m cli_client.main
