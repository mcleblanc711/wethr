"""
Operational helpers for local Wethr maintenance.

These functions intentionally avoid network calls. They are meant for the
"is this machine wired correctly?" path: database location, recent activity,
and the JSON export consumed by n8n.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config
from .paper_trader import init_db


SETTLED_EXPORT_COLUMNS = (
    "id as trade_id",
    "city",
    "target_date",
    "bracket_label",
    "bracket_lower",
    "bracket_upper",
    "bracket_unit",
    "side",
    "entry_price",
    "size_usd",
    "pnl",
    "model_prob",
    "market_prob",
    "edge",
    "outcome",
    "settled_at",
    "market_volume",
)


def default_settled_export_path() -> Path:
    """Return the JSON path mounted into n8n as /data/wethr/settled_trades.json."""
    return config.REPO_ROOT / "n8n-wethr" / "wethr-output" / "settled_trades.json"


def export_settled_trades(
    out_path: Path | None = None,
    since_hours: int = 24,
    db_path: Path | None = None,
) -> tuple[Path, int]:
    """Export recently settled trades as JSON for the n8n audit workflow."""
    if since_hours <= 0:
        raise ValueError("since_hours must be positive")

    db = db_path or config.DB_PATH
    out = out_path or default_settled_export_path()
    init_db(db)

    columns = ", ".join(SETTLED_EXPORT_COLUMNS)
    query = f"""
        SELECT {columns}
        FROM trades
        WHERE settled = 1
          AND settled_at > datetime('now', ?)
        ORDER BY target_date, city, id
    """

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(query, (f"-{since_hours} hours",))]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return out, len(rows)


@dataclass(frozen=True)
class TableSummary:
    name: str
    count: int | None
    latest: str | None = None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _count_table(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _latest_value(conn: sqlite3.Connection, table: str, column: str) -> str | None:
    if not _table_exists(conn, table):
        return None
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        return None
    return conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]


def _summarize_db(path: Path) -> list[TableSummary]:
    if not path.exists():
        return [
            TableSummary("trades", None),
            TableSummary("signals", None),
            TableSummary("historical_forecasts", None),
            TableSummary("historical_observations", None),
        ]

    with sqlite3.connect(str(path)) as conn:
        return [
            TableSummary("trades", _count_table(conn, "trades"), _latest_value(conn, "trades", "created_at")),
            TableSummary("signals", _count_table(conn, "signals"), _latest_value(conn, "signals", "updated_at")),
            TableSummary(
                "historical_forecasts",
                _count_table(conn, "historical_forecasts"),
                _latest_value(conn, "historical_forecasts", "fetched_at"),
            ),
            TableSummary(
                "historical_observations",
                _count_table(conn, "historical_observations"),
                _latest_value(conn, "historical_observations", "fetched_at"),
            ),
        ]


def _json_file_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "count": None, "error": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        count = len(data) if isinstance(data, list) else None
        return {"exists": True, "count": count, "error": None}
    except Exception as exc:
        return {"exists": True, "count": None, "error": str(exc)}


def doctor_report() -> str:
    """Build a local operational status report."""
    db_path = config.DB_PATH
    legacy_db = config.COLLECTOR_ROOT / "data" / "wethr.db"
    export_path = default_settled_export_path()
    export_status = _json_file_status(export_path)

    lines = [
        "Wethr local status",
        f"repo_root: {config.REPO_ROOT}",
        f"db_path:   {db_path} ({'exists' if db_path.exists() else 'missing'})",
        f"export:    {export_path} ({'exists' if export_status['exists'] else 'missing'})",
    ]
    if export_status["count"] is not None:
        lines[-1] += f", {export_status['count']} row(s)"
    if export_status["error"]:
        lines[-1] += f", invalid JSON: {export_status['error']}"

    lines.append("")
    lines.append("primary database:")
    for summary in _summarize_db(db_path):
        count = "missing" if summary.count is None else str(summary.count)
        latest = f", latest={summary.latest}" if summary.latest else ""
        lines.append(f"  {summary.name}: {count}{latest}")

    if legacy_db.exists() and legacy_db.resolve() != db_path.resolve():
        lines.append("")
        lines.append(f"legacy collector database detected: {legacy_db}")
        for summary in _summarize_db(legacy_db):
            count = "missing" if summary.count is None else str(summary.count)
            latest = f", latest={summary.latest}" if summary.latest else ""
            lines.append(f"  {summary.name}: {count}{latest}")

    lines.append("")
    lines.append("n8n expected mount: n8n-wethr/wethr-output -> /data/wethr")
    return "\n".join(lines)
