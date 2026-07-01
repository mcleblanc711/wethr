#!/usr/bin/env python3
"""
Wethr — weather prediction market trading agent.

Usage:
    python run.py scan              # Discover markets + find edges
    python run.py trade             # Scan + place paper trades
    python run.py loop              # Continuous scan/trade/settle loop
    python run.py settle [DATE]     # Settle trades for a date
    python run.py report            # Show performance report
    python run.py pending           # Show pending trades
    python run.py train --all       # Train EMOS calibration
    python run.py emos              # Show EMOS parameters
    python run.py diagnose          # Run API diagnostics
"""
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Auto-activate venv if we're not already in it.
#
# This means `python3 run.py scan` works whether or not you've run
# `source .venv/bin/activate` — it finds the adjacent .venv and re-execs.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_VENV_DIR = _HERE / ".venv"
_VENV_PYTHON = _VENV_DIR / "bin" / "python"

if _VENV_PYTHON.exists() and Path(sys.prefix).resolve() != _VENV_DIR.resolve():
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON)] + sys.argv)

# ---------------------------------------------------------------------------
# Route to the right module
# ---------------------------------------------------------------------------
if len(sys.argv) > 1 and sys.argv[1] == "diagnose":
    sys.argv = sys.argv[1:]
    from src.diagnose import main
    main()
else:
    from src.main import main
    main()
