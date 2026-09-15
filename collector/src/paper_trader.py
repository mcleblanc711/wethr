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
import re
import sqlite3
from os import PathLike
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
    # WAL allows concurrent readers but still serialises writers, and the loop
    # service now shares this file with two calibration timers, so wait for the
    # lock instead of failing the scan outright.
    conn = sqlite3.connect(str(path), timeout=config.DB_BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(config.DB_BUSY_TIMEOUT_S * 1000)}")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    ddl: str,
) -> None:
    """Add a SQLite column, tolerating concurrent init_db calls."""
    from .ledger import add_column_if_missing
    add_column_if_missing(conn, table, column, ddl)


def init_db(db_path: Path | None = None) -> None:
    """Initialize database schema."""
    with get_db(db_path) as conn:
        conn.executescript(_SCHEMA)
        # Migrate existing DBs: add columns if missing. These guards also
        # tolerate two Wethr processes starting at the same time.
        _add_column_if_missing(
            conn, "trades", "market_volume",
            "ALTER TABLE trades ADD COLUMN market_volume REAL",
        )
        _add_column_if_missing(
            conn, "signals", "market_volume",
            "ALTER TABLE signals ADD COLUMN market_volume REAL",
        )
        _add_column_if_missing(
            conn, "trades", "redeemed",
            "ALTER TABLE trades ADD COLUMN redeemed INTEGER NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(
            conn, "trades", "redeemed_at",
            "ALTER TABLE trades ADD COLUMN redeemed_at TEXT",
        )
        for col in ("ensemble_mean", "ensemble_std", "resolved_value"):
            _add_column_if_missing(
                conn, "signals", col,
                f"ALTER TABLE signals ADD COLUMN {col} REAL",
            )
        # Create settings table if missing (migration for existing DBs)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Additive calibration ledger migration. Existing report tables stay
        # intact and are explicitly tagged as legacy provenance.
        from .ledger import init_calibration_ledger
        init_calibration_ledger(conn)
        _init_strategy_epochs(conn)
    log.info(f"Database initialized: {db_path or config.DB_PATH}")


def _coerce_legacy_db_path_arg(
    market_volume: float | PathLike[str] | str,
    db_path: Path | None,
) -> tuple[float, Path | None]:
    """Support older positional calls that passed db_path as market_volume."""
    if db_path is None and isinstance(market_volume, (str, PathLike)):
        return 0.0, Path(market_volume)
    return float(market_volume or 0.0), db_path


# ---------------------------------------------------------------------------
# Strategy epochs
# ---------------------------------------------------------------------------

LEGACY_STRATEGY_VERSION = "legacy-v0"
CURRENT_EPOCH_SETTING_KEY = "current_strategy_epoch"
_EPOCH_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class StrategyEpoch:
    label: str
    started_at: str
    starting_bankroll: float
    notes: str | None = None
    params: dict | None = None


def _init_strategy_epochs(conn: sqlite3.Connection) -> None:
    """Create the epoch registry and seed the legacy epoch once.

    ``legacy-v0`` starts at ``INITIAL_BANKROLL + bankroll_adjustment`` so its
    bankroll matches what ``get_stats`` reported before epochs existed.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_epochs (
            label TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            starting_bankroll REAL NOT NULL,
            notes TEXT,
            params_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    adjustment = conn.execute(
        "SELECT value FROM settings WHERE key = 'bankroll_adjustment'"
    ).fetchone()
    conn.execute(
        """
        INSERT OR IGNORE INTO strategy_epochs (label, started_at, starting_bankroll, notes)
        VALUES (
            ?,
            COALESCE(
                (SELECT MIN(created_at) FROM trades WHERE strategy_version = ?),
                datetime('now')
            ),
            ?,
            'Pre-calibration ledger (seeded from bankroll_adjustment)'
        )
        """,
        (
            LEGACY_STRATEGY_VERSION,
            LEGACY_STRATEGY_VERSION,
            config.INITIAL_BANKROLL + (float(adjustment[0]) if adjustment else 0.0),
        ),
    )


def _epoch_from_row(row: sqlite3.Row) -> StrategyEpoch:
    return StrategyEpoch(
        label=row["label"],
        started_at=row["started_at"],
        starting_bankroll=float(row["starting_bankroll"]),
        notes=row["notes"],
        params=json.loads(row["params_json"] or "{}"),
    )


def _current_epoch(conn: sqlite3.Connection) -> StrategyEpoch:
    setting = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (CURRENT_EPOCH_SETTING_KEY,)
    ).fetchone()
    label = setting["value"] if setting else LEGACY_STRATEGY_VERSION
    row = conn.execute("SELECT * FROM strategy_epochs WHERE label = ?", (label,)).fetchone()
    if row is None and label != LEGACY_STRATEGY_VERSION:
        log.warning(f"Unknown strategy epoch {label!r} in settings; using {LEGACY_STRATEGY_VERSION}")
        row = conn.execute(
            "SELECT * FROM strategy_epochs WHERE label = ?", (LEGACY_STRATEGY_VERSION,)
        ).fetchone()
    if row is None:
        return StrategyEpoch(LEGACY_STRATEGY_VERSION, "", config.INITIAL_BANKROLL)
    return _epoch_from_row(row)


def get_current_epoch(db_path: Path | None = None) -> StrategyEpoch:
    """Return the epoch new paper trades are recorded under."""
    with get_db(db_path) as conn:
        return _current_epoch(conn)


def list_epochs(db_path: Path | None = None) -> list[StrategyEpoch]:
    """Return every strategy epoch, legacy first, then oldest first."""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_epochs ORDER BY label != ?, started_at, label",
            (LEGACY_STRATEGY_VERSION,),
        ).fetchall()
    return [_epoch_from_row(row) for row in rows]


def start_epoch(
    label: str,
    starting_bankroll: float | None = None,
    notes: str | None = None,
    db_path: Path | None = None,
) -> StrategyEpoch:
    """Register a new strategy epoch and make it current.

    Epochs are additive: existing trades keep their ``strategy_version``.
    """
    if not _EPOCH_LABEL_RE.match(label or ""):
        raise ValueError(
            "epoch label must be 1-64 characters of letters, digits, '.', '_' or '-'"
        )
    bankroll = config.INITIAL_BANKROLL if starting_bankroll is None else float(starting_bankroll)
    if bankroll <= 0:
        raise ValueError("starting bankroll must be positive")
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM strategy_epochs WHERE label = ?", (label,)).fetchone():
            raise ValueError(f"strategy epoch {label!r} already exists")
        conn.execute(
            "INSERT INTO strategy_epochs (label, started_at, starting_bankroll, notes) "
            "VALUES (?, ?, ?, ?)",
            (label, started_at, bankroll, notes),
        )
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (CURRENT_EPOCH_SETTING_KEY, label),
        )
    log.info(f"Started strategy epoch {label} with bankroll ${bankroll:,.2f}")
    return StrategyEpoch(label, started_at, bankroll, notes, {})


# ---------------------------------------------------------------------------
# Trade recording
# ---------------------------------------------------------------------------

def record_paper_trade(
    city: str,
    target_date: date,
    ps: PositionSize,
    market_volume: float = 0.0,
    db_path: Path | None = None,
    model_version_id: str | None = None,
    prediction_snapshot_id: int | None = None,
    strategy_version: str | None = None,
) -> int | None:
    """
    Record a paper trade with city/date context. Returns trade ID,
    or None if a trade already exists for this bracket.

    ``strategy_version`` defaults to the current strategy epoch.
    """
    market_volume, db_path = _coerce_legacy_db_path_arg(market_volume, db_path)
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

        if model_version_id is None:
            model_version_id = "legacy-emos-2026-04-08"
        if prediction_snapshot_id is None:
            prediction = conn.execute(
                """
                SELECT p.id FROM prediction_snapshots p
                JOIN market_snapshots m ON m.id=p.market_snapshot_id
                WHERE p.model_version_id=? AND m.city=? AND m.target_date=?
                  AND m.bracket_label=?
                ORDER BY p.generated_at DESC LIMIT 1
                """,
                (model_version_id, city, target_date.isoformat(), b.label),
            ).fetchone()
            prediction_snapshot_id = prediction["id"] if prediction else None
        if strategy_version is None:
            strategy_version = _current_epoch(conn).label

        cursor = conn.execute(
            """
            INSERT INTO trades (
                city, target_date, bracket_label, bracket_lower, bracket_upper,
                bracket_unit, side, entry_price, size_usd,
                model_prob, market_prob, edge,
                member_count, total_members, confidence,
                kelly_full, kelly_frac, token_id, condition_id,
                market_volume, model_version_id, prediction_snapshot_id, strategy_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                model_version_id,
                prediction_snapshot_id,
                strategy_version,
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
    market_volume, db_path = _coerce_legacy_db_path_arg(market_volume, db_path)
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
    settled_stake: float = 0.0
    avg_pnl: float = 0.0
    avg_edge: float = 0.0
    brier_score: float | None = None
    brier_n: int = 0
    bankroll: float = config.INITIAL_BANKROLL


def get_stats(
    db_path: Path | None = None,
    strategy_version: str | None = None,
) -> TradingStats:
    """Get trading statistics, optionally for one strategy epoch.

    Filtered stats report that epoch's bankroll (its starting bankroll plus
    its realized P/L). Unfiltered stats cover the whole ledger and report the
    current epoch's bankroll, which is what sizing uses.
    """
    stats = TradingStats()
    where = "WHERE strategy_version = ?" if strategy_version is not None else "WHERE 1 = 1"
    params: tuple = (strategy_version,) if strategy_version is not None else ()

    with get_db(db_path) as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(settled = 1), 0) AS settled,
                   COALESCE(SUM(settled = 1 AND pnl > 0), 0) AS wins,
                   SUM(CASE WHEN settled = 1 THEN pnl END) AS total,
                   SUM(CASE WHEN settled = 1 THEN size_usd END) AS settled_stake,
                   AVG(CASE WHEN settled = 1 THEN pnl END) AS avg_pnl,
                   AVG(CASE WHEN settled = 1 THEN edge END) AS avg_edge
            FROM trades {where}
            """,
            params,
        ).fetchone()
        stats.total_trades = row["n"]
        stats.settled_trades = row["settled"]
        stats.pending_trades = stats.total_trades - stats.settled_trades
        if stats.settled_trades > 0:
            stats.wins = row["wins"]
            stats.losses = stats.settled_trades - stats.wins
            stats.win_rate = stats.wins / stats.settled_trades
            stats.gross_pnl = row["total"] or 0.0
            stats.settled_stake = row["settled_stake"] or 0.0
            stats.avg_pnl = row["avg_pnl"] or 0.0
            stats.avg_edge = row["avg_edge"] or 0.0

        epoch = _current_epoch(conn)
        if strategy_version is None or strategy_version == epoch.label:
            starting = epoch.starting_bankroll
            epoch_label = epoch.label
        else:
            found = conn.execute(
                "SELECT starting_bankroll FROM strategy_epochs WHERE label = ?",
                (strategy_version,),
            ).fetchone()
            starting = float(found["starting_bankroll"]) if found else config.INITIAL_BANKROLL
            epoch_label = strategy_version
        if strategy_version is None:
            epoch_pnl = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE settled = 1 AND strategy_version = ?",
                (epoch_label,),
            ).fetchone()[0]
        else:
            epoch_pnl = stats.gross_pnl
        stats.bankroll = starting + epoch_pnl

        # Brier score from signals (signals are not tagged by epoch)
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
    Reset the current epoch's bankroll to `target` without touching trades.

    Adjusts the epoch's `starting_bankroll` so that
        starting_bankroll + epoch realized P&L == target

    All historical trades, signals, and P&L records are preserved.
    Returns the change applied to the starting bankroll.
    """
    with get_db(db_path) as conn:
        epoch = _current_epoch(conn)
        epoch_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE settled = 1 AND strategy_version = ?",
            (epoch.label,),
        ).fetchone()[0]
        new_start = target - epoch_pnl
        adjustment = new_start - epoch.starting_bankroll
        conn.execute(
            "UPDATE strategy_epochs SET starting_bankroll = ? WHERE label = ?",
            (new_start, epoch.label),
        )

    log.info(
        f"Bankroll for epoch {epoch.label} reset to ${target:,.2f} "
        f"(adjustment={adjustment:+,.2f}, epoch_pnl={epoch_pnl:,.2f})"
    )
    return adjustment


def get_pending_trades(db_path: Path | None = None) -> list[dict]:
    """Get all unsettled trades."""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE settled = 0 ORDER BY target_date"
        ).fetchall()
        return [dict(r) for r in rows]


def get_daily_pnl(
    trade_date: date | None = None,
    db_path: Path | None = None,
    strategy_version: str | None = None,
) -> float:
    """Get P&L settled on a date (defaults to today), optionally for one epoch."""
    d = (trade_date or datetime.now(timezone.utc).date()).isoformat()
    epoch_filter = " AND strategy_version = ?" if strategy_version is not None else ""
    params = (d, strategy_version) if strategy_version is not None else (d,)
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) as pnl FROM trades "
            "WHERE settled = 1 AND DATE(settled_at) = ?" + epoch_filter,
            params,
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
        f"  Bankroll:   ${stats.bankroll:,.2f} (current epoch)",
    ]

    current = get_current_epoch(db_path)
    lines.append("")
    lines.append("  Strategy epochs:")
    for epoch in list_epochs(db_path):
        epoch_stats = get_stats(db_path, strategy_version=epoch.label)
        marker = "*" if epoch.label == current.label else " "
        lines.append(
            f"   {marker}{epoch.label}: ${epoch_stats.gross_pnl:+,.2f} "
            f"({epoch_stats.wins}W / {epoch_stats.losses}L, {epoch_stats.pending_trades} open), "
            f"bankroll ${epoch_stats.bankroll:,.2f}"
        )

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
