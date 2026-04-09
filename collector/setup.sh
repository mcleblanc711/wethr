#!/usr/bin/env bash
set -euo pipefail

# Wethr setup — creates venv, installs deps, initializes DB
#
# Usage: ./setup.sh
#
# Always uses a venv — never touches system Python.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WORKSPACE="$SCRIPT_DIR"

VENV_DIR="$WORKSPACE/.venv"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

echo "═══════════════════════════════════════════"
echo "  Wethr Setup"
echo "  Workspace: $WORKSPACE"
echo "  Venv:      $VENV_DIR"
echo "═══════════════════════════════════════════"

# --- Ensure python3-venv is available ---
if ! python3 -m venv --help &>/dev/null 2>&1; then
    echo ""
    echo "python3-venv not found. Install it:"
    echo "  sudo apt install python3-venv    # Ubuntu/Debian"
    echo "  apk add python3                  # Alpine (venv included)"
    exit 1
fi

# --- Create venv ---
if [ ! -f "$PYTHON" ]; then
    echo ""
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    echo "  ✅ venv created"
else
    echo "  ✅ venv already exists"
fi

# --- Install dependencies ---
echo ""
echo "Installing Python dependencies..."
"$PIP" install --upgrade pip --quiet 2>/dev/null || true
"$PIP" install -r "$WORKSPACE/requirements.txt" --quiet
"$PYTHON" -c "import httpx, numpy, scipy; print('  ✅ Core deps OK')"

# --- Initialize database ---
echo ""
echo "Initializing database..."
cd "$WORKSPACE"
"$PYTHON" -c "
import sys; sys.path.insert(0, '.')
from src.paper_trader import init_db
init_db()
print('  ✅ Database initialized')
"

# --- Verify modules ---
echo ""
echo "Verifying modules..."
"$PYTHON" -c "
import sys; sys.path.insert(0, '.')
from src import config
print(f'  ✅ {len(config.CITIES)} cities configured')
from src.calibration import crps_gaussian
from src.bma import BMAWeights
from src.latency import LatencyDetector
from src.trading import TradingClient
print('  ✅ All modules OK')
"

# --- Run tests ---
echo ""
echo "Running tests..."
cd "$WORKSPACE"
"$PYTHON" tests/test_core.py 2>&1 | tail -3

# --- Done ---
echo ""
echo "═══════════════════════════════════════════"
echo "  Setup complete!"
echo "═══════════════════════════════════════════"
echo ""
echo "Activate the venv for interactive use:"
echo "  source $VENV_DIR/bin/activate"
echo "  python run.py diagnose"
echo ""
echo "Or run directly without activating:"
echo "  $PYTHON run.py diagnose"
echo "  $PYTHON run.py scan"
echo "  $PYTHON run.py loop"
