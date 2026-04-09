"""
Reset the paper-trading bankroll to a target value (default $10,000).

All trade history, P&L records, and signals are preserved. A
`bankroll_adjustment` offset is written to the settings table so that:

    displayed_bankroll = INITIAL_BANKROLL + adjustment + cumulative_pnl

Run:
    python scripts/reset_bankroll.py            # reset to $10,000
    python scripts/reset_bankroll.py 5000       # reset to $5,000
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make sure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.paper_trader import get_stats, init_db, reset_bankroll

if __name__ == "__main__":
    target = float(sys.argv[1]) if len(sys.argv) > 1 else 10_000.0

    init_db()
    before = get_stats()
    print(f"Before reset:")
    print(f"  Gross P&L (all history): ${before.gross_pnl:>10,.2f}")
    print(f"  Current bankroll:        ${before.bankroll:>10,.2f}")
    print(f"  Settled trades:          {before.settled_trades}")

    adjustment = reset_bankroll(target=target)
    after = get_stats()

    print(f"\nAfter reset to ${target:,.2f}:")
    print(f"  Bankroll adjustment:     ${adjustment:>+10,.2f}")
    print(f"  New bankroll:            ${after.bankroll:>10,.2f}")
    print(f"  (All {after.settled_trades} settled trades + history preserved)")
