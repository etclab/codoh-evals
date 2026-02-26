#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage: ./setup-benchmark.sh

Sets up the Python environment for running the page-load benchmarks.

Steps:
  1. Creates a Python virtual environment in ./venv
  2. Installs playwright (the only third-party dependency)
  3. Installs Chromium browser via playwright
EOF
    exit 0
fi

echo "=== Setting up benchmark environment ==="

# Step 1: Create venv
if [[ -d "$VENV_DIR" ]]; then
    echo "Virtual environment already exists at $VENV_DIR, skipping creation."
else
    echo "Creating virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
fi

# Step 2: Install playwright
echo "Installing playwright..."
"$VENV_DIR/bin/pip" install --quiet playwright

# Step 3: Install Chromium browser and dependencies
echo "Installing Chromium browser for playwright..."
"$VENV_DIR/bin/playwright" install chromium
"$VENV_DIR/bin/playwright" install-deps chromium

echo ""
echo "=== Setup complete ==="
echo "Activate the venv with: source venv/bin/activate"
echo "Then run: python benchmark-har.py [vanilla|odoh]"
