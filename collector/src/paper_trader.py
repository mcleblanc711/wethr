"""
Paper trading engine with performance tracking.

Records simulated trades, tracks P&L, computes Brier scores,
and maintains calibration data for validating the probability model.

Uses SQLite for persistence — survives restarts, easy to query.

Brier score = mean((forecast_prob - outcome)²)
    Perfect = 0.0, Climatology ≈ 0.25, Random = 0.33
    Our target: < 0.20 before going live.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Generator

import numpy as np

from . import config
from .probability import BracketProbability
from .sizing import PositionSize

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    bracket_label TEXT NOT NULL,
    bracket_lower REAL,
    bracket_upper REAL,
    bracket_unit TEXT NOT NULL,
    side TEXT NOT NULL,              -- YES or NO
    entry_price REAL NOT NULL,
    size_usd REAL NOT NULL,
    model_prob REAL NOT NULL,
    market_prob REAL NOT NULL,
    edge REAL NOT NULL,
    member_count INTEGER NOT NULL,
    total_members INTEGER NOT NULL,
    confidence REAL NOT NULL,
    kelly_full REAL NOT NULL,
    kelly_frac REAL NOT NULL,
    -- Settlement fields (filled on resolution)
    settled INTEGER NOT NULL DEFAULT 0,
    settled_at TEXT,
    outcome INTEGER,                 -- 1 = YES resolved true, 0 = NO
    pnl REAL,
    token_id TEXT,
    condition_id TEXT,
    market_volume REAL,             -- Event volume at time of trade
    redeemed INTEGER NOT NULL DEFAULT 0,
    redeemed_at TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    bracket_label TEXT NOT NULL,
    model_prob REAL NOT NULL,
    market_prob REAL NOT NULL,
    edge REAL NOT NULL,
    member_count INTEGER NOT NULL,
    total_members INTEGER NOT NULL,
    market_volume REAL,             -- Event volume at time of signal
    ensemble_mean REAL,             -- Ensemble mean (°C) for EMOS training
    ensemble_std REAL,              -- Ensemble std dev (°C) for EMOS training
    -- Outcome tracking for Brier score
    outcome INTEGER,                 -- 1 or 0 after settlement
    brier_score REAL,                -- (model_prob - outcome)²
    resolved_value REAL,             -- Observed temp midpoint of winning bracket (°C)
    -- One signal per city/date/bracket — updated in place on each scan
    UNIQUE(city, target_date, bracket_label)
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    trades_placed INTEGER NOT NULL DEFAULT 0,
    trades_settled INTEGER NOT NULL DEFAULT 0,
    gross_pnl REAL NOT NULL DEFAULT 0.0,
    bankroll REAL NOT NULL,
    signals_generated INTEGER NOT NULL DEFAULT 0,
    brier_score REAL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(target_date);
CREATE INDEX IF NOT EXISTS idx_trades_settled ON trades(settled);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(target_date);
"""


@contextmanager
def get_db(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections."""
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    """Initialize database schema."""
    with get_db(db_path) as conn:
        conn.executescript(_SCHEMA)
        # Migrate existing DBs: add columns if missing
        for table in ("trades", "signals"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if "market_volume" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN market_volume REAL")
        # Trades-only migrations for auto-redeem tracking
        trade_cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        if "redeemed" not in trade_cols:
            conn.execute("ALTER TABLE trades ADD COLUMN redeemed INTEGER NOT NULL DEFAULT 0")
        if "redeemed_at" not in trade_cols:
            conn.execute("ALTER TABLE trades ADD COLUMN redeemed_at TEXT")
        # Signals-only migrations for EMOS support
        sig_cols = [r[1] for r in conn.execute("PRAGMA table_info(signals)").fetchall()]
        for col in ("ensemble_mean", "ensemble_std", "resolved_value"):
            if col not in sig_cols:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col} REAL")
        # Create settings table if missing (migration for existing DBs)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
    log.info(f"Database initialized: {db_path or config.DB_PATH}")


# ---------------------------------------------------------------------------
# Trade recording
# ---------------------------------------------------------------------------

def record_paper_trade(
    city: str,
    target_date: date,
    ps: PositionSize,
    market_volume: float = 0.0,
    db_path: Path | None = None,
) -> int | None:
    """
    Record a paper trade with city/date context. Returns trade ID,
    or None if a trade already exists for this bracket.
    """
    bp = ps.bracket_prob
    b = bp.bracket

    with get_db(db_path) as conn:
        # Dedup: skip if we already have a pending trade for this bracket
        existing = conn.execute(
            """
            SELECT id FROM trades 
            WHERE city = ? AND target_date = ? AND bracket_label = ? AND settled = 0
            """,
            (city, target_date.isoformat(), b.label),
        ).fetchone()

        if existing:
            log.debug(
                f"  DEDUP: Trade already exists for {city} {target_date} "
                f"{b.label} (trade #{existing['id']})"
            )
            return None

        cursor = conn.execute(
            """
            INSERT INTO trades (
                city, target_date, bracket_label, bracket_lower, bracket_upper,
                bracket_unit, side, entry_price, size_usd,
                model_prob, market_prob, edge,
                member_count, total_members, confidence,
                kelly_full, kelly_frac, token_id, condition_id,
                market_volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                city,
                target_date.isoformat(),
                b.label,
                b.lower,
                b.upper,
                b.unit,
                ps.side,
                ps.entry_price,
                ps.capped_size_usd,
                bp.model_prob,
                bp.market_prob,
                bp.edge,
                bp.member_count,
                bp.total_members,
                bp.confidence,
                ps.full_kelly,
                ps.fractional_kelly,
                b.token_id,
                b.condition_id,
                market_volume,
            ),
        )
        trade_id = cursor.lastrowid

    log.info(
        f"📝 Paper trade #{trade_id}: {city} {target_date} "
        f"{b.label} {ps.side} @ {ps.entry_price:.2f} "
        f"${ps.capped_size_usd:.2f} (edge={bp.edge:+.1%}, vol=${market_volume:,.0f})"
    )
    return trade_id


# ---------------------------------------------------------------------------
# Signal recording (for Brier score — ALL signals, not just traded)
# ---------------------------------------------------------------------------

def record_signal(
    city: str,
    target_date: date,
    bp: BracketProbability,
    market_volume: float = 0.0,
    ensemble_mean: float | None = None,
    ensemble_std: float | None = None,
    db_path: Path | None = None,
) -> None:
    """
    Record or update a signal for Brier score tracking.

    Uses INSERT OR REPLACE on the unique (city, target_date, bracket_label)
    constraint. Each scan cycle updates the model_prob with the latest
    ensemble estimate. Brier score is computed against the FINAL model_prob
    at settlement time — this is correct because we want to score our
    best estimate, not our first one.
    """
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO signals (
                city, target_date, bracket_label,
                model_prob, market_prob, edge,
                member_count, total_members, market_volume,
                ensemble_mean, ensemble_std
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(city, target_date, bracket_label) DO UPDATE SET
                updated_at = datetime('now'),
                model_prob = excluded.model_prob,
                market_prob = excluded.market_prob,
                edge = excluded.edge,
                member_count = excluded.member_count,
                total_members = excluded.total_members,
                market_volume = excluded.market_volume,
                ensemble_mean = excluded.ensemble_mean,
                ensemble_std = excluded.ensemble_std
            """,
            (
                city,
                target_date.isoformat(),
                bp.bracket.label,
                bp.model_prob,
                bp.market_prob,
                bp.edge,
                bp.member_count,
                bp.total_members,
                market_volume,
                ensemble_mean,
                ensemble_std,
            ),
        )


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def settle_trade(
    trade_id: int,
    outcome: bool,
    db_path: Path | None = None,
) -> float:
    """
    Settle a paper trade. Returns P&L.
    
    outcome = True means the bracket was the correct one (YES resolved true).
    """
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT side, entry_price, size_usd FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()

        if not row:
            raise ValueError(f"Trade {trade_id} not found")

        side = row["side"]
        entry = row["entry_price"]
        size = row["size_usd"]

        # Contracts = size / entry_price (number of contracts bought)
        contracts = size / entry if entry > 0 else 0

        if side == "YES":
            # Bought YES at entry_price, resolves at 1.0 (win) or 0.0 (loss)
            pnl = contracts * (1.0 - entry) if outcome else -size
        else:
            # Bought NO at (1 - market_price), resolves at 1.0 (win) or 0.0 (loss)
            pnl = contracts * (1.0 - (1.0 - entry)) if not outcome else -size
            # Simpler: NO wins when outcome is False
            # Wait, let me reconsider. If we buy NO:
            #   We pay (1 - market_prob) per contract
            #   We win if outcome = False (bracket didn't hit)
            #   Payout = 1.0 per contract if we win
            pnl = contracts * entry if not outcome else -size
            # Actually: bought NO at price (1-market_prob), which is entry_price
            # If NO resolves true (outcome=False): payout = $1/contract, profit = (1-entry)*contracts
            # If NO resolves false (outcome=True): lose everything
            if not outcome:
                pnl = contracts * (1.0 - entry)
            else:
                pnl = -size

        # Actually let me simplify this properly.
        # We buy `side` at `entry_price`. 
        # Contracts = size / entry_price.
        # If our side wins: each contract pays $1, so PnL = contracts * (1 - entry_price)
        # If our side loses: each contract pays $0, so PnL = -size
        our_side_wins = (side == "YES" and outcome) or (side == "NO" and not outcome)
        if our_side_wins:
            pnl = contracts * (1.0 - entry)
        else:
            pnl = -size

        conn.execute(
            """
            UPDATE trades SET
                settled = 1,
                settled_at = datetime('now'),
                outcome = ?,
                pnl = ?
            WHERE id = ?
            """,
            (1 if outcome else 0, round(pnl, 4), trade_id),
        )

    log.info(
        f"{'✅' if pnl > 0 else '❌'} Trade #{trade_id} settled: "
        f"{'WIN' if our_side_wins else 'LOSS'} ${pnl:+.2f}"
    )
    return pnl


def mark_trades_redeemed(
    condition_id: str,
    db_path: Path | None = None,
) -> int:
    """Mark all settled trades for a given condition_id as on-chain redeemed."""
    with get_db(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE trades SET
                redeemed = 1,
                redeemed_at = datetime('now')
            WHERE condition_id = ? AND settled = 1 AND redeemed = 0
            """,
            (condition_id,),
        )
        return cur.rowcount


def get_winning_unredeemed_conditions(
    db_path: Path | None = None,
) -> list[str]:
    """Return distinct condition_ids of winning, settled, unredeemed trades."""
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT condition_id FROM trades
            WHERE settled = 1
              AND redeemed = 0
              AND condition_id IS NOT NULL
              AND condition_id != ''
              AND (
                  (side = 'YES' AND outcome = 1)
                  OR (side = 'NO' AND outcome = 0)
              )
            """
        ).fetchall()
    return [r["condition_id"] for r in rows]


def settle_signal(
    signal_id: int,
    outcome: bool,
    db_path: Path | None = None,
) -> None:
    """Settle a signal for Brier score computation."""
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT model_prob FROM signals WHERE id = ?",
            (signal_id,),
        ).fetchone()
        if not row:
            return

        model_prob = row["model_prob"]
        # Brier score component: (forecast - outcome)²
        brier = (model_prob - (1.0 if outcome else 0.0)) ** 2

        conn.execute(
            """
            UPDATE signals SET outcome = ?, brier_score = ?
            WHERE id = ?
            """,
            (1 if outcome else 0, round(brier, 6), signal_id),
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass
class TradingStats:
    total_trades: int = 0
    settled_trades: int = 0
    pending_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    avg_pnl: float = 0.0
    avg_edge: float = 0.0
    brier_score: float | None = None
    brier_n: int = 0
    bankroll: float = config.INITIAL_BANKROLL


def get_stats(db_path: Path | None = None) -> TradingStats:
    """Get overall trading statistics."""
    stats = TradingStats()

    with get_db(db_path) as conn:
        # Trade counts
        row = conn.execute("SELECT COUNT(*) as n FROM trades").fetchone()
        stats.total_trades = row["n"]

        row = conn.execute(
            "SELECT COUNT(*) as n FROM trades WHERE settled = 1"
        ).fetchone()
        stats.settled_trades = row["n"]
        stats.pending_trades = stats.total_trades - stats.settled_trades

        if stats.settled_trades > 0:
            row = conn.execute(
                "SELECT COUNT(*) as n FROM trades WHERE settled = 1 AND pnl > 0"
            ).fetchone()
            stats.wins = row["n"]
            stats.losses = stats.settled_trades - stats.wins
            stats.win_rate = stats.wins / stats.settled_trades

            row = conn.execute(
                "SELECT SUM(pnl) as total, AVG(pnl) as avg_pnl, AVG(edge) as avg_edge "
                "FROM trades WHERE settled = 1"
            ).fetchone()
            stats.gross_pnl = row["total"] or 0.0
            stats.avg_pnl = row["avg_pnl"] or 0.0
            stats.avg_edge = row["avg_edge"] or 0.0

        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'bankroll_adjustment'"
        ).fetchone()
        adjustment = float(row["value"]) if row else 0.0
        stats.bankroll = config.INITIAL_BANKROLL + adjustment + stats.gross_pnl

        # Brier score from signals
        row = conn.execute(
            "SELECT AVG(brier_score) as brier, COUNT(*) as n "
            "FROM signals WHERE brier_score IS NOT NULL"
        ).fetchone()
        if row["n"] > 0:
            stats.brier_score = row["brier"]
            stats.brier_n = row["n"]

    return stats


def reset_bankroll(
    target: float = 10_000.0,
    db_path: Path | None = None,
) -> float:
    """
    Reset the displayed bankroll to `target` without touching any trade data.

    Stores a `bankroll_adjustment` in the settings table so that:
        bankroll = INITIAL_BANKROLL + adjustment + gross_pnl == target

    All historical trades, signals, and P&L records are preserved.
    Returns the adjustment value written.
    """
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT SUM(pnl) as total FROM trades WHERE settled = 1"
        ).fetchone()
        gross_pnl = row["total"] or 0.0

        # adjustment = target - INITIAL_BANKROLL - gross_pnl
        adjustment = target - config.INITIAL_BANKROLL - gross_pnl

        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES ('bankroll_adjustment', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (str(adjustment),),
        )

    log.info(
        f"Bankroll reset to ${target:,.2f} "
        f"(adjustment={adjustment:+,.2f}, gross_pnl={gross_pnl:,.2f})"
    )
    return adjustment


def get_pending_trades(db_path: Path | None = None) -> list[dict]:
    """Get all unsettled trades."""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE settled = 0 ORDER BY target_date"
        ).fetchall()
        return [dict(r) for r in rows]


def get_daily_pnl(trade_date: date | None = None, db_path: Path | None = None) -> float:
    """Get P&L for a specific date (defaults to today)."""
    d = (trade_date or datetime.now(timezone.utc).date()).isoformat()
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as pnl FROM trades "
            "WHERE settled = 1 AND DATE(settled_at) = ?",
            (d,),
        ).fetchone()
        return row["pnl"]


def get_calibration_data(
    db_path: Path | None = None,
    n_bins: int = 10,
) -> list[dict]:
    """
    Get calibration data: predicted probability vs observed frequency.
    
    Groups signals into probability bins and compares predicted vs actual.
    A well-calibrated model has predicted ≈ observed in each bin.
    """
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT model_prob, outcome FROM signals "
            "WHERE outcome IS NOT NULL"
        ).fetchall()

    if not rows:
        return []

    probs = np.array([r["model_prob"] for r in rows])
    outcomes = np.array([r["outcome"] for r in rows])

    bin_edges = np.linspace(0, 1, n_bins + 1)
    calibration = []

    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        mask = (probs >= lo) & (probs < hi)
        n = mask.sum()
        if n == 0:
            continue
        calibration.append({
            "bin_lower": round(lo, 2),
            "bin_upper": round(hi, 2),
            "bin_center": round((lo + hi) / 2, 2),
            "predicted_mean": round(float(probs[mask].mean()), 4),
            "observed_freq": round(float(outcomes[mask].mean()), 4),
            "count": int(n),
        })

    return calibration


def print_report(db_path: Path | None = None) -> str:
    """Generate a text report of trading performance."""
    stats = get_stats(db_path)
    lines = [
        "═══════════════════════════════════════════",
        "  WETHR Paper Trading Report",
        "═══════════════════════════════════════════",
        f"  Trades:     {stats.total_trades} total, {stats.settled_trades} settled, {stats.pending_trades} pending",
        f"  Win rate:   {stats.win_rate:.1%} ({stats.wins}W / {stats.losses}L)",
        f"  Gross P&L:  ${stats.gross_pnl:+,.2f}",
        f"  Avg P&L:    ${stats.avg_pnl:+,.2f} per trade",
        f"  Avg edge:   {stats.avg_edge:+.1%}",
        f"  Bankroll:   ${stats.bankroll:,.2f}",
    ]

    if stats.brier_score is not None:
        lines.append(f"  Brier score: {stats.brier_score:.4f} (n={stats.brier_n})")
        if stats.brier_score < 0.15:
            lines.append("  📊 Calibration: EXCELLENT")
        elif stats.brier_score < 0.20:
            lines.append("  📊 Calibration: GOOD — approaching live-ready")
        elif stats.brier_score < 0.25:
            lines.append("  📊 Calibration: FAIR — needs improvement")
        else:
            lines.append("  📊 Calibration: POOR — do not go live")

    cal = get_calibration_data(db_path)
    if cal:
        lines.append("")
        lines.append("  Calibration (predicted → observed):")
        for c in cal:
            lines.append(
                f"    [{c['bin_lower']:.0%}-{c['bin_upper']:.0%}] "
                f"predicted={c['predicted_mean']:.1%} "
                f"observed={c['observed_freq']:.1%} "
                f"(n={c['count']})"
            )

    lines.append("═══════════════════════════════════════════")
    report = "\n".join(lines)
    return report
