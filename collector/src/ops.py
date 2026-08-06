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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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
    "model_version_id",
    "prediction_snapshot_id",
)


def default_settled_export_path() -> Path:
    """Return the JSON path mounted into n8n as /data/wethr/settled_trades.json."""
    return config.REPO_ROOT / "n8n-wethr" / "wethr-output" / "settled_trades.json"


def default_audit_db_path() -> Path:
    return (
        config.REPO_ROOT
        / "n8n-wethr"
        / "wethr-output"
        / "wethr_audit.db"
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("audit timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def audit_db_status(
    path: Path | None = None,
    *,
    now: datetime | None = None,
    timezone_name: str = "America/Edmonton",
    scheduled_hour: int = 9,
    grace_minutes: int = 15,
) -> dict[str, Any]:
    """Report whether the latest successful n8n run met today's schedule."""
    audit_path = path or default_audit_db_path()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result: dict[str, Any] = {
        "path": str(audit_path),
        "exists": audit_path.exists(),
        "latest_started_at": None,
        "latest_finished_at": None,
        "latest_status": None,
        "expected_since": None,
        "stale": True,
        "age_hours": None,
        "error": None,
    }
    local_now = current.astimezone(ZoneInfo(timezone_name))
    scheduled = local_now.replace(
        hour=scheduled_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local_now < scheduled + timedelta(minutes=grace_minutes):
        scheduled -= timedelta(days=1)
    expected = scheduled.astimezone(timezone.utc)
    result["expected_since"] = expected.isoformat().replace("+00:00", "Z")
    if not audit_path.exists():
        return result
    try:
        uri = f"file:{audit_path}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """SELECT started_at,finished_at,status
                   FROM audit_runs
                   WHERE status='completed'
                   ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
    except (sqlite3.Error, OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    if row is None:
        return result
    try:
        latest = _parse_utc(str(row["started_at"]))
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    result.update({
        "latest_started_at": str(row["started_at"]),
        "latest_finished_at": row["finished_at"],
        "latest_status": str(row["status"]),
        "stale": latest < expected,
        "age_hours": (current - latest).total_seconds() / 3600,
    })
    return result


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
    audit_status = audit_db_status()

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

    from .calibration_ops import collection_status
    status = collection_status(db_path)
    lines.append("")
    lines.append("calibration ledger:")
    lines.append(f"  forecast freshness: {status['forecast_freshness_minutes']} minutes")
    lines.append(f"  stale: {status['forecast_stale']}")
    lines.append(f"  member-count failures: {len(status['member_count_failures'])}")
    lines.append(f"  unresolved dates: {len(status['unresolved_dates'])} ({len(status['unresolved_older_than_3d'])} older than 3d)")
    lines.append(f"  reconciliation discrepancies: {status['reconciliation_discrepancies']}")
    lines.append(f"  7d scan coverage: {status['scan_coverage_7d']:.1%} across {status['scan_days_7d']} day(s)")
    lines.append(
        f"  prospective window uninterrupted: {status['prospective_window_uninterrupted']}"
    )
    lines.append(f"  archive: {status['archive_latest'] or 'missing'} (verified={status['archive_verified']})")
    active = [model['id'] for model in status['models'] if model['status'] == 'active']
    shadow = [model['id'] for model in status['models'] if model['status'] in {'candidate', 'shadow'}]
    lines.append(f"  active model: {active[0] if active else 'none'}")
    lines.append(f"  candidate/shadow models: {', '.join(shadow) if shadow else 'none'}")

    lines.append("")
    lines.append("n8n expected mount: n8n-wethr/wethr-output -> /data/wethr")
    age = audit_status["age_hours"]
    age_text = "unknown" if age is None else f"{age:.1f}h"
    lines.append(
        "n8n audit: "
        f"latest={audit_status['latest_started_at'] or 'missing'}, "
        f"age={age_text}, stale={audit_status['stale']}, "
        f"expected_since={audit_status['expected_since']}"
    )
    if audit_status["error"]:
        lines.append(f"n8n audit error: {audit_status['error']}")
    return "\n".join(lines)
