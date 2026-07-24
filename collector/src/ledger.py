"""Immutable calibration ledger and model lifecycle primitives.

The legacy ``signals`` and ``historical_*`` tables remain available for old
reports, but they are not a safe training source.  New calibration work must
flow through the tables and helpers in this module.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from statistics import mean, stdev
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from . import config

SCHEMA_VERSION = 3
LEGACY_MODEL_VERSION = "legacy-emos-2026-04-08"
LEGACY_PROVENANCE = "legacy-v0"
QUALITY_OK = "ok"
QUALITY_LEGACY = "legacy_unverified"

LEDGER_TABLES = (
    "forecast_snapshots",
    "market_snapshots",
    "prediction_snapshots",
    "station_observations",
    "daily_observations",
    "market_resolutions",
    "model_versions",
    "model_transitions",
    "collection_runs",
    "resolution_retry_state",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_versions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    lead_basis TEXT NOT NULL DEFAULT 'unknown',
    lead_bucket TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    training_cutoff TEXT,
    dataset_manifest_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('candidate','shadow','active','retired')),
    predecessor_id TEXT REFERENCES model_versions(id),
    shadow_since TEXT,
    activated_at TEXT,
    retired_at TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS forecast_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    capture_cutoff_at TEXT,
    lead_basis TEXT NOT NULL DEFAULT 'run_time',
    target_timezone TEXT NOT NULL,
    target_day_start_utc TEXT NOT NULL,
    lead_hours REAL NOT NULL,
    lead_bucket TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    content_version INTEGER NOT NULL,
    member_count INTEGER NOT NULL,
    member_values_json TEXT NOT NULL,
    mean_c REAL NOT NULL,
    spread_c REAL NOT NULL,
    std_c REAL NOT NULL,
    q10_c REAL NOT NULL,
    q25_c REAL NOT NULL,
    q50_c REAL NOT NULL,
    q75_c REAL NOT NULL,
    q90_c REAL NOT NULL,
    source_endpoint TEXT NOT NULL,
    issue_time_verified INTEGER NOT NULL CHECK(issue_time_verified IN (0,1)),
    quality_status TEXT NOT NULL,
    quality_detail TEXT,
    schema_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    sighting_identity TEXT NOT NULL UNIQUE,
    canonical_hash TEXT NOT NULL,
    UNIQUE(city, target_date, provider, model, sighting_identity)
);
CREATE INDEX IF NOT EXISTS idx_forecast_target
    ON forecast_snapshots(target_date, city, lead_bucket);
CREATE INDEX IF NOT EXISTS idx_forecast_seen
    ON forecast_snapshots(first_seen_at, last_seen_at);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    bracket_label TEXT NOT NULL,
    bracket_lower REAL,
    bracket_upper REAL,
    bracket_unit TEXT NOT NULL,
    yes_best_bid REAL,
    yes_best_ask REAL,
    no_best_bid REAL,
    no_best_ask REAL,
    midpoint REAL,
    last_price REAL,
    volume REAL,
    liquidity REAL,
    spread REAL,
    resolution_url TEXT,
    declared_precision REAL,
    source_endpoint TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    canonical_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_market_target
    ON market_snapshots(target_date, city, condition_id, captured_at);

CREATE TABLE IF NOT EXISTS prediction_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    model_version_id TEXT NOT NULL REFERENCES model_versions(id),
    market_snapshot_id INTEGER NOT NULL REFERENCES market_snapshots(id),
    forecast_snapshot_ids_json TEXT NOT NULL,
    bracket_probability REAL NOT NULL CHECK(bracket_probability BETWEEN 0 AND 1),
    raw_probability REAL NOT NULL CHECK(raw_probability BETWEEN 0 AND 1),
    executable_side TEXT NOT NULL CHECK(executable_side IN ('YES','NO')),
    executable_ask REAL NOT NULL CHECK(executable_ask BETWEEN 0 AND 1),
    edge REAL NOT NULL,
    data_quality TEXT NOT NULL,
    canonical_hash TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_model
    ON prediction_snapshots(model_version_id, generated_at);

CREATE TABLE IF NOT EXISTS station_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    station_id TEXT NOT NULL,
    city TEXT NOT NULL,
    temperature_c REAL NOT NULL,
    unit_reported TEXT NOT NULL,
    precision REAL,
    source_url TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    quality_status TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    canonical_hash TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_station_obs_day
    ON station_observations(city, station_id, observed_at);

CREATE TABLE IF NOT EXISTS daily_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    timezone TEXT NOT NULL,
    provider TEXT NOT NULL,
    station_id TEXT NOT NULL,
    max_temperature_c REAL NOT NULL,
    rounded_temperature REAL,
    rounded_unit TEXT,
    declared_precision REAL,
    source_observation_ids_json TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    revision INTEGER NOT NULL,
    quality_status TEXT NOT NULL,
    reconciled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    canonical_hash TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    UNIQUE(city, target_date, provider, station_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_daily_obs_target
    ON daily_observations(target_date, city, quality_status);

CREATE TABLE IF NOT EXISTS market_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    condition_id TEXT,
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    winning_label TEXT NOT NULL,
    winning_lower REAL,
    winning_upper REAL,
    winning_unit TEXT NOT NULL,
    exact_rounded_value REAL,
    resolution_url TEXT NOT NULL,
    declared_station TEXT,
    normalized_station_id TEXT,
    resolution_provider TEXT,
    declared_precision REAL,
    resolved_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL,
    reconciliation_detail TEXT,
    reconciled_daily_observation_id INTEGER REFERENCES daily_observations(id),
    source_json TEXT NOT NULL,
    canonical_hash TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resolution_target
    ON market_resolutions(target_date, city, resolved_at);

CREATE TABLE IF NOT EXISTS resolution_retry_state (
    city TEXT NOT NULL,
    target_date TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    last_result TEXT,
    next_retry_at TEXT NOT NULL,
    PRIMARY KEY(city, target_date)
);
CREATE INDEX IF NOT EXISTS idx_resolution_retry_due
    ON resolution_retry_state(next_retry_at, target_date, city);

CREATE TABLE IF NOT EXISTS model_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL,
    from_model_id TEXT REFERENCES model_versions(id),
    to_model_id TEXT NOT NULL REFERENCES model_versions(id),
    action TEXT NOT NULL CHECK(action IN ('promote','rollback')),
    gate_report_json TEXT NOT NULL
);

CREATE VIEW IF NOT EXISTS paper_trades_compatible AS
    SELECT t.*, COALESCE(t.model_version_id, 'legacy-emos-2026-04-08') AS effective_model_version
    FROM trades t;
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def lead_bucket(lead_hours: float) -> str:
    if lead_hours < 0:
        return "in_day"
    if lead_hours < 24:
        return "0_24h"
    if lead_hours < 48:
        return "24_48h"
    if lead_hours < 72:
        return "48_72h"
    return "72h_plus"


def target_day_start_utc(target_date: date, timezone_name: str) -> datetime:
    local = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        tzinfo=ZoneInfo(timezone_name),
    )
    return local.astimezone(timezone.utc)


def quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires at least one value")
    pos = (len(ordered) - 1) * fraction
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return float(ordered[low])
    weight = pos - low
    return float(ordered[low] * (1 - weight) + ordered[high] * weight)


def round_to_precision(value: float, precision: float) -> float:
    if precision <= 0:
        raise ValueError("precision must be positive")
    step = Decimal(str(precision))
    rounded_steps = (Decimal(str(value)) / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(rounded_steps * step)


def exact_value_for_interval(
    lower: float | None,
    upper: float | None,
    precision: float | None,
) -> float | None:
    """Return a value only for a singleton rounded bracket, never a midpoint."""
    if lower is None or upper is None or precision is None:
        return None
    if math.isclose(upper - lower, precision, rel_tol=0.0, abs_tol=1e-9):
        return lower
    return None


def value_in_interval(value: float, lower: float | None, upper: float | None) -> bool:
    return (lower is None or value >= lower) and (upper is None or value < upper)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    """Add a SQLite column, tolerating a concurrent migration of the same table.

    Two Wethr processes can call ``init_db`` at once — the loop service and a
    calibration timer — so losing the race must be a no-op, not a crash.
    """
    if column in _column_names(conn, table):
        return
    try:
        conn.execute(ddl)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return
        raise


_add_column = add_column_if_missing


def init_calibration_ledger(conn: sqlite3.Connection) -> None:
    """Apply the additive, idempotent ledger migration to an open connection."""
    _add_column(
        conn, "trades", "model_version_id",
        "ALTER TABLE trades ADD COLUMN model_version_id TEXT",
    )
    _add_column(
        conn, "trades", "prediction_snapshot_id",
        "ALTER TABLE trades ADD COLUMN prediction_snapshot_id INTEGER",
    )
    _add_column(
        conn, "trades", "strategy_version",
        "ALTER TABLE trades ADD COLUMN strategy_version TEXT NOT NULL DEFAULT 'legacy-v0'",
    )
    _add_column(
        conn, "signals", "provenance",
        "ALTER TABLE signals ADD COLUMN provenance TEXT NOT NULL DEFAULT 'legacy-v0'",
    )
    if "historical_forecasts" in {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        _add_column(
            conn, "historical_forecasts", "quality_status",
            "ALTER TABLE historical_forecasts ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'legacy_unverified'",
        )
        _add_column(
            conn, "historical_forecasts", "training_eligible",
            "ALTER TABLE historical_forecasts ADD COLUMN training_eligible INTEGER NOT NULL DEFAULT 0",
        )
    conn.executescript(_SCHEMA)
    # Schema v2: separate the capture cutoff from a verified provider run time.
    _add_column(
        conn, "forecast_snapshots", "capture_cutoff_at",
        "ALTER TABLE forecast_snapshots ADD COLUMN capture_cutoff_at TEXT",
    )
    # Rows written before v2 asserted a verified issue time they did not have.
    _add_column(
        conn, "forecast_snapshots", "lead_basis",
        "ALTER TABLE forecast_snapshots ADD COLUMN lead_basis TEXT NOT NULL DEFAULT 'unknown'",
    )
    _add_column(
        conn, "forecast_snapshots", "content_hash",
        "ALTER TABLE forecast_snapshots ADD COLUMN content_hash TEXT",
    )
    _add_column(
        conn, "forecast_snapshots", "sighting_identity",
        "ALTER TABLE forecast_snapshots ADD COLUMN sighting_identity TEXT",
    )
    _add_column(
        conn, "model_versions", "lead_basis",
        "ALTER TABLE model_versions ADD COLUMN lead_basis TEXT NOT NULL DEFAULT 'unknown'",
    )
    _add_column(
        conn, "market_resolutions", "normalized_station_id",
        "ALTER TABLE market_resolutions ADD COLUMN normalized_station_id TEXT",
    )
    _add_column(
        conn, "market_resolutions", "resolution_provider",
        "ALTER TABLE market_resolutions ADD COLUMN resolution_provider TEXT",
    )
    _add_column(
        conn, "market_resolutions", "reconciled_daily_observation_id",
        "ALTER TABLE market_resolutions ADD COLUMN reconciled_daily_observation_id INTEGER REFERENCES daily_observations(id)",
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_forecast_sighting_identity
           ON forecast_snapshots(sighting_identity)
           WHERE sighting_identity IS NOT NULL"""
    )
    conn.execute("DROP INDEX IF EXISTS idx_model_one_active")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_model_one_active_route
           ON model_versions(lead_basis, lead_bucket)
           WHERE status='active' AND lead_basis IN ('run_time','capture_cutoff')"""
    )
    _add_column(
        conn, "model_versions", "shadow_since",
        "ALTER TABLE model_versions ADD COLUMN shadow_since TEXT",
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            expected_markets INTEGER NOT NULL,
            captured_markets INTEGER NOT NULL,
            coverage REAL NOT NULL,
            missing_executable_books INTEGER NOT NULL,
            status TEXT NOT NULL,
            detail_json TEXT NOT NULL
        )
        """
    )
    now = iso_utc()
    active = conn.execute("SELECT id FROM model_versions WHERE status='active'").fetchone()
    status = "active" if active is None else "shadow"
    conn.execute(
        """
        INSERT OR IGNORE INTO model_versions (
            id, created_at, algorithm, scope_type, scope_value, lead_basis, lead_bucket,
            parameters_json, training_cutoff, dataset_manifest_json,
            metrics_json, status, activated_at, schema_version
        ) VALUES (?, ?, 'legacy-emos', 'global', 'all', 'legacy', 'all', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            LEGACY_MODEL_VERSION,
            now,
            canonical_json({"provenance": LEGACY_PROVENANCE}),
            "2026-04-08T23:59:59Z",
            canonical_json({"quality": QUALITY_LEGACY, "automatic_training": False}),
            canonical_json({"control": True}),
            status,
            now if status == "active" else None,
            SCHEMA_VERSION,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO model_versions (
            id,created_at,algorithm,scope_type,scope_value,lead_basis,lead_bucket,
            parameters_json,training_cutoff,dataset_manifest_json,metrics_json,
            status,schema_version
        ) VALUES ('raw-ensemble-v1',?,'raw-ensemble','global','all','legacy','all','{}',NULL,'{"provenance":"member-values"}','{"control":true}','shadow',?)
        """,
        (now, SCHEMA_VERSION),
    )
    conn.execute(
        "UPDATE model_versions SET lead_basis='legacy' WHERE id IN (?,?) AND lead_basis='unknown'",
        (LEGACY_MODEL_VERSION, "raw-ensemble-v1"),
    )
    conn.execute(
        "UPDATE trades SET model_version_id=? WHERE model_version_id IS NULL",
        (LEGACY_MODEL_VERSION,),
    )


@dataclass(frozen=True)
class ForecastSnapshotInput:
    """A forecast capture.

    ``run_time`` is the provider's model run time and must only be set when it
    was actually read from the response.  When it is absent the capture time is
    the sole defensible information cutoff: the row records
    ``lead_basis='capture_cutoff'`` and ``issue_time_verified=0``.

    A capture cutoff is still a sound training basis — it is a conservative
    bound, since we never claim to have known a forecast earlier than we
    fetched it — but it is a *different* quantity from a run time, and the two
    must not be mixed inside one lead bucket.  Training therefore selects a
    single basis rather than trusting a flag.  There is deliberately no way to
    assert a verified issue time without supplying one.
    """
    city: str
    target_date: date
    seen_at: datetime
    provider: str
    model: str
    members_c: Sequence[float]
    source_endpoint: str
    run_time: datetime | None = None
    quality_status: str = QUALITY_OK
    quality_detail: str | None = None

    @property
    def issue_time_verified(self) -> bool:
        return self.run_time is not None

    @property
    def lead_basis(self) -> str:
        return "run_time" if self.run_time is not None else "capture_cutoff"

    @property
    def issued_at(self) -> datetime:
        """The information cutoff used for lead calculations."""
        return self.run_time if self.run_time is not None else self.seen_at


def record_forecast_snapshot(
    conn: sqlite3.Connection,
    item: ForecastSnapshotInput,
) -> tuple[int, bool]:
    city = config.CITIES.get(item.city)
    if city is None:
        raise ValueError(f"unsupported city: {item.city}")
    values = [float(v) for v in item.members_c]
    if len(values) < 2:
        raise ValueError("deterministic forecasts cannot enter the ensemble ledger")
    if not all(math.isfinite(v) for v in values):
        raise ValueError("member values must be finite")
    issued = parse_utc(item.issued_at)
    seen = parse_utc(item.seen_at)
    day_start = target_day_start_utc(item.target_date, city.timezone)
    lead = (day_start - issued).total_seconds() / 3600.0
    bucket = lead_bucket(lead)
    content_digest = canonical_hash({"members_c": values})
    sighting_digest = canonical_hash({
        "city": item.city,
        "target_date": item.target_date.isoformat(),
        "provider": item.provider,
        "model": item.model,
        "lead_basis": item.lead_basis,
        "information_cutoff": iso_utc(issued),
        "lead_bucket": bucket,
        "content_hash": content_digest,
        "schema_version": SCHEMA_VERSION,
    })
    existing = conn.execute(
        "SELECT id FROM forecast_snapshots WHERE sighting_identity=?",
        (sighting_digest,),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE forecast_snapshots SET last_seen_at=? WHERE id=?",
            (iso_utc(seen), existing["id"]),
        )
        return int(existing["id"]), False
    version_row = conn.execute(
        """
        SELECT MIN(CASE WHEN content_hash=? THEN content_version END) AS existing_version,
               COALESCE(MAX(content_version), 0) + 1 AS next_version
        FROM forecast_snapshots WHERE city=? AND target_date=? AND provider=? AND model=?
        """,
        (content_digest, item.city, item.target_date.isoformat(), item.provider, item.model),
    ).fetchone()
    content_version = int(version_row["existing_version"] or version_row["next_version"])
    values_mean = mean(values)
    values_std = stdev(values)
    cur = conn.execute(
        """
        INSERT INTO forecast_snapshots (
            city, target_date, issued_at, first_seen_at, last_seen_at,
            capture_cutoff_at, lead_basis, target_timezone, target_day_start_utc,
            lead_hours, lead_bucket,
            provider, model, content_version, member_count, member_values_json,
            mean_c, spread_c, std_c, q10_c, q25_c, q50_c, q75_c, q90_c,
            source_endpoint, issue_time_verified, quality_status, quality_detail,
            schema_version, content_hash, sighting_identity, canonical_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            item.city, item.target_date.isoformat(), iso_utc(issued), iso_utc(seen),
            iso_utc(seen), iso_utc(seen), item.lead_basis, city.timezone,
            iso_utc(day_start), lead,
            bucket, item.provider, item.model, content_version,
            len(values), canonical_json(values), values_mean,
            max(values) - min(values), values_std,
            quantile(values, .10), quantile(values, .25), quantile(values, .50),
            quantile(values, .75), quantile(values, .90), item.source_endpoint,
            int(item.issue_time_verified), item.quality_status, item.quality_detail,
            SCHEMA_VERSION, content_digest, sighting_digest, sighting_digest,
        ),
    )
    return int(cur.lastrowid), True


def record_station_observation(
    conn: sqlite3.Connection,
    *,
    city: str,
    station_id: str,
    provider: str,
    observed_at: datetime,
    received_at: datetime,
    temperature_c: float,
    unit_reported: str,
    source_url: str,
    raw: Any,
    precision: float | None = None,
    quality_status: str = QUALITY_OK,
) -> tuple[int, bool]:
    payload = {
        "provider": provider,
        "station_id": station_id,
        "observed_at": iso_utc(parse_utc(observed_at)),
        "temperature_c": float(temperature_c),
        "raw": raw,
    }
    digest = canonical_hash(payload)
    row = conn.execute(
        "SELECT id FROM station_observations WHERE canonical_hash=?", (digest,)
    ).fetchone()
    if row:
        return int(row["id"]), False
    cur = conn.execute(
        """
        INSERT INTO station_observations (
            observed_at, received_at, provider, station_id, city, temperature_c,
            unit_reported, precision, source_url, quality_status, raw_json,
            canonical_hash, schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            payload["observed_at"], iso_utc(parse_utc(received_at)), provider,
            station_id, city, float(temperature_c), unit_reported, precision,
            source_url, quality_status, canonical_json(raw), digest, SCHEMA_VERSION,
        ),
    )
    return int(cur.lastrowid), True


def aggregate_daily_observation(
    conn: sqlite3.Connection,
    *,
    city: str,
    target_date: date,
    provider: str,
    station_id: str,
    declared_precision: float | None = None,
    rounded_unit: str | None = None,
    min_readings: int = 12,
    min_span_hours: float = 18.0,
) -> int:
    city_cfg = config.CITIES.get(city)
    if city_cfg is None:
        raise ValueError(f"unsupported city: {city}")
    tz = ZoneInfo(city_cfg.timezone)
    rows = conn.execute(
        """
        SELECT id, observed_at, temperature_c FROM station_observations
        WHERE city=? AND provider=? AND station_id=? AND quality_status='ok'
        ORDER BY observed_at
        """,
        (city, provider, station_id),
    ).fetchall()
    selected = [
        row for row in rows
        if parse_utc(row["observed_at"]).astimezone(tz).date() == target_date
    ]
    if not selected:
        raise ValueError("no quality station readings for local target day")
    # A maximum computed from a fraction of the day understates the true daily
    # max. Such rows are kept for audit but marked so they cannot become
    # station truth: reconciliation and training both require 'ok'.
    observed_times = [parse_utc(row["observed_at"]) for row in selected]
    span_hours = (max(observed_times) - min(observed_times)).total_seconds() / 3600.0
    sufficient = len(selected) >= min_readings and span_hours >= min_span_hours
    quality = QUALITY_OK if sufficient else "partial_day"
    max_c = max(float(row["temperature_c"]) for row in selected)
    display = max_c if (rounded_unit or "C") == "C" else max_c * 9.0 / 5.0 + 32.0
    rounded = (
        round_to_precision(display, declared_precision)
        if declared_precision is not None else None
    )
    revision = int(conn.execute(
        """
        SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM daily_observations
        WHERE city=? AND target_date=? AND provider=? AND station_id=?
        """,
        (city, target_date.isoformat(), provider, station_id),
    ).fetchone()["revision"])
    ids = [int(row["id"]) for row in selected]
    payload = {
        "city": city,
        "target_date": target_date.isoformat(),
        "provider": provider,
        "station_id": station_id,
        "max_temperature_c": max_c,
        "source_ids": ids,
        "precision": declared_precision,
        "unit": rounded_unit,
        "quality": quality,
    }
    digest = canonical_hash(payload)
    existing = conn.execute(
        "SELECT id FROM daily_observations WHERE canonical_hash=?", (digest,)
    ).fetchone()
    if existing:
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO daily_observations (
            city, target_date, timezone, provider, station_id, max_temperature_c,
            rounded_temperature, rounded_unit, declared_precision,
            source_observation_ids_json, calculation_version, revision,
            quality_status, created_at, canonical_hash, schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            city, target_date.isoformat(), city_cfg.timezone, provider, station_id,
            max_c, rounded, rounded_unit, declared_precision, canonical_json(ids),
            "local-day-max-v1", revision, quality, iso_utc(), digest,
            SCHEMA_VERSION,
        ),
    )
    return int(cur.lastrowid)


def normalize_station_id(value: str | None) -> str | None:
    """Return one explicit four-character station identifier, never a guess."""
    if not value:
        return None
    matches = {match.upper() for match in re.findall(r"(?<![A-Z0-9])[A-Z0-9]{4}(?![A-Z0-9])", value.upper())}
    return next(iter(matches)) if len(matches) == 1 else None


def official_resolution_provider(city: str) -> str:
    city_cfg = config.CITIES.get(city)
    if city_cfg is None or city_cfg.resolution_adapter not in {"nws", "aviationweather-metar"}:
        raise ValueError(f"unsupported official resolution adapter for {city}")
    return city_cfg.resolution_adapter


def reconcile_resolution(conn: sqlite3.Connection, resolution_id: int) -> str:
    resolution = conn.execute(
        "SELECT * FROM market_resolutions WHERE id=?", (resolution_id,)
    ).fetchone()
    if not resolution:
        raise ValueError(f"resolution {resolution_id} not found")
    normalized_station = normalize_station_id(resolution["declared_station"])
    provider = official_resolution_provider(str(resolution["city"]))
    city_cfg = config.CITIES[str(resolution["city"])]
    daily = conn.execute(
        """
        SELECT * FROM daily_observations
        WHERE city=? AND target_date=? AND station_id=? AND provider=?
          AND quality_status='ok'
        ORDER BY revision DESC, id DESC LIMIT 1
        """,
        (resolution["city"], resolution["target_date"], normalized_station, provider),
    ).fetchone()
    if normalized_station is None or resolution["declared_precision"] is None:
        status, detail = "missing_resolution_metadata", "Declared station identifier or precision is unavailable"
    elif normalized_station != city_cfg.station.upper():
        status, detail = "station_mismatch", f"Declared station {normalized_station} does not match configured station {city_cfg.station}"
    elif not daily:
        status, detail = "missing_station_truth", f"No quality daily observation for {normalized_station} from {provider}"
    else:
        unit = resolution["winning_unit"]
        value = float(daily["max_temperature_c"])
        if unit == "F":
            value = value * 9.0 / 5.0 + 32.0
        precision = resolution["declared_precision"]
        rounded = round_to_precision(value, float(precision)) if precision else value
        if value_in_interval(rounded, resolution["winning_lower"], resolution["winning_upper"]):
            status, detail = "matched", f"rounded station value {rounded:g}°{unit} is in winning interval"
            conn.execute("UPDATE daily_observations SET reconciled=1 WHERE id=?", (daily["id"],))
        else:
            status = "discrepancy"
            detail = f"rounded station value {rounded:g}°{unit} is outside winning interval"
            conn.execute(
                "UPDATE daily_observations SET quality_status='reconciliation_discrepancy' WHERE id=?",
                (daily["id"],),
            )
    conn.execute(
        """UPDATE market_resolutions
           SET normalized_station_id=?, resolution_provider=?,
               reconciliation_status=?, reconciliation_detail=?,
               reconciled_daily_observation_id=?
           WHERE id=?""",
        (normalized_station, provider, status, detail,
         int(daily["id"]) if daily and status in {"matched", "discrepancy"} else None, resolution_id),
    )
    return status


def training_forecast_rows(
    conn: sqlite3.Connection,
    *,
    lead_bucket_name: str | None = None,
    cutoff: datetime | None = None,
    lead_basis: str = "capture_cutoff",
) -> list[sqlite3.Row]:
    if lead_basis not in {"run_time", "capture_cutoff"}:
        raise ValueError(f"unsupported lead basis: {lead_basis}")
    clauses = [
        # One basis per dataset. Run-time leads and capture-cutoff leads are
        # different quantities; mixing them inside a lead bucket would make the
        # bucket meaningless.
        "f.lead_basis=?",
        "f.schema_version=?",
        "f.content_hash IS NOT NULL",
        "f.sighting_identity IS NOT NULL",
        "f.quality_status='ok'",
        "f.member_count>=2",
        "d.quality_status='ok'",
        "d.reconciled=1",
        "f.first_seen_at < r.resolved_at",
        "f.issued_at < r.resolved_at",
    ]
    params: list[Any] = [lead_basis, SCHEMA_VERSION]
    if lead_bucket_name:
        clauses.append("f.lead_bucket=?")
        params.append(lead_bucket_name)
    if cutoff:
        clauses.append("f.target_date<=?")
        params.append(cutoff.date().isoformat())
    # Join to exactly one observation and one resolution per city/day. Multiple
    # revisions or providers would otherwise multiply every forecast row and
    # silently reweight the training set.
    query = f"""
        SELECT f.*, d.max_temperature_c, d.id AS daily_observation_id,
               r.id AS resolution_id, r.resolved_at
        FROM forecast_snapshots f
        JOIN market_resolutions r ON r.id = (
            SELECT r2.id FROM market_resolutions r2
            WHERE r2.city=f.city AND r2.target_date=f.target_date
              AND r2.reconciliation_status='matched'
              AND r2.reconciled_daily_observation_id IS NOT NULL
            ORDER BY r2.resolved_at ASC, r2.id ASC LIMIT 1
        )
        JOIN daily_observations d ON d.id=r.reconciled_daily_observation_id
          AND d.city=f.city AND d.target_date=f.target_date
          AND d.station_id=r.normalized_station_id
          AND d.provider=r.resolution_provider
        WHERE {' AND '.join(clauses)}
        ORDER BY f.target_date, f.city, f.issued_at
    """
    return list(conn.execute(query, params).fetchall())


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in LEDGER_TABLES:
        counts[table] = int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    return counts


def record_forecast_summary(
    conn: sqlite3.Connection,
    *,
    city: str,
    target_date: date,
    issued_at: datetime,
    seen_at: datetime,
    provider: str,
    model: str,
    value_c: float,
    source_endpoint: str,
    quality_detail: str = "deterministic fixed-lead summary; excluded from ensemble training",
) -> tuple[int, bool]:
    """Store a verifiable deterministic summary as explicitly untrainable."""
    city_cfg = config.CITIES.get(city)
    if city_cfg is None:
        raise ValueError(f"unsupported city: {city}")
    issued = parse_utc(issued_at)
    seen = parse_utc(seen_at)
    start = target_day_start_utc(target_date, city_cfg.timezone)
    lead = (start - issued).total_seconds() / 3600.0
    payload = {
        "city": city,
        "target_date": target_date.isoformat(),
        "provider": provider,
        "model": model,
        "issued_at": iso_utc(issued),
        "value_c": float(value_c),
        "kind": "deterministic-summary",
        "schema_version": SCHEMA_VERSION,
    }
    digest = canonical_hash(payload)
    content_digest = canonical_hash({"value_c": float(value_c)})
    row = conn.execute(
        "SELECT id FROM forecast_snapshots WHERE sighting_identity=?", (digest,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE forecast_snapshots SET last_seen_at=? WHERE id=?",
            (iso_utc(seen), row["id"]),
        )
        return int(row["id"]), False
    version = int(conn.execute(
        """SELECT COALESCE(MAX(content_version),0)+1 AS n
           FROM forecast_snapshots WHERE city=? AND target_date=? AND provider=? AND model=?""",
        (city, target_date.isoformat(), provider, model),
    ).fetchone()["n"])
    cur = conn.execute(
        """
        INSERT INTO forecast_snapshots (
            city,target_date,issued_at,first_seen_at,last_seen_at,capture_cutoff_at,
            lead_basis,target_timezone,
            target_day_start_utc,lead_hours,lead_bucket,provider,model,content_version,
            member_count,member_values_json,mean_c,spread_c,std_c,q10_c,q25_c,q50_c,
            q75_c,q90_c,source_endpoint,issue_time_verified,quality_status,
            quality_detail,schema_version,content_hash,sighting_identity,canonical_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            city, target_date.isoformat(), iso_utc(issued), iso_utc(seen), iso_utc(seen),
            iso_utc(seen), "run_time",
            city_cfg.timezone, iso_utc(start), lead, lead_bucket(lead), provider, model,
            version, 1, canonical_json([float(value_c)]), float(value_c), 0.0, 0.0,
            float(value_c), float(value_c), float(value_c), float(value_c), float(value_c),
            source_endpoint, 1, "deterministic_untrainable", quality_detail,
            SCHEMA_VERSION, content_digest, digest, digest,
        ),
    )
    return int(cur.lastrowid), True


def record_market_snapshot(conn: sqlite3.Connection, **fields: Any) -> tuple[int, bool]:
    """Insert a timestamped executable market quote, deduplicated by content."""
    captured_at = parse_utc(fields.pop("captured_at"))
    payload = {
        key: fields.get(key)
        for key in (
            "condition_id", "event_id", "city", "target_date", "bracket_label",
            "bracket_lower", "bracket_upper", "bracket_unit", "yes_best_bid",
            "yes_best_ask", "no_best_bid", "no_best_ask", "midpoint", "last_price",
            "volume", "liquidity", "spread", "resolution_url", "declared_precision",
        )
    }
    digest = canonical_hash(payload)
    row = conn.execute(
        "SELECT id FROM market_snapshots WHERE canonical_hash=?", (digest,)
    ).fetchone()
    if row:
        return int(row["id"]), False
    cur = conn.execute(
        """
        INSERT INTO market_snapshots (
            captured_at,condition_id,event_id,city,target_date,bracket_label,
            bracket_lower,bracket_upper,bracket_unit,yes_best_bid,yes_best_ask,
            no_best_bid,no_best_ask,midpoint,last_price,volume,liquidity,spread,
            resolution_url,declared_precision,source_endpoint,schema_version,canonical_hash
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            iso_utc(captured_at), fields["condition_id"], fields["event_id"],
            fields["city"], str(fields["target_date"]), fields["bracket_label"],
            fields.get("bracket_lower"), fields.get("bracket_upper"), fields["bracket_unit"],
            fields.get("yes_best_bid"), fields.get("yes_best_ask"),
            fields.get("no_best_bid"), fields.get("no_best_ask"), fields.get("midpoint"),
            fields.get("last_price"), fields.get("volume"), fields.get("liquidity"),
            fields.get("spread"), fields.get("resolution_url"),
            fields.get("declared_precision"), fields.get("source_endpoint", "gamma+clob"),
            SCHEMA_VERSION, digest,
        ),
    )
    return int(cur.lastrowid), True


def record_prediction_snapshot(
    conn: sqlite3.Connection,
    *,
    model_version_id: str,
    market_snapshot_id: int,
    forecast_snapshot_ids: Sequence[int],
    bracket_probability: float,
    raw_probability: float,
    executable_side: str,
    executable_ask: float,
    generated_at: datetime,
    data_quality: str = QUALITY_OK,
) -> tuple[int, bool]:
    if executable_side not in {"YES", "NO"}:
        raise ValueError("executable_side must be YES or NO")
    if not 0 <= executable_ask <= 1:
        raise ValueError("executable ask must be between 0 and 1")
    probability = bracket_probability if executable_side == "YES" else 1 - bracket_probability
    edge = probability - executable_ask
    payload = {
        "model_version_id": model_version_id,
        "market_snapshot_id": market_snapshot_id,
        "forecast_snapshot_ids": sorted(int(v) for v in forecast_snapshot_ids),
        "bracket_probability": float(bracket_probability),
        "raw_probability": float(raw_probability),
        "executable_side": executable_side,
        "executable_ask": float(executable_ask),
        "generated_at": iso_utc(parse_utc(generated_at)),
    }
    digest = canonical_hash(payload)
    row = conn.execute(
        "SELECT id FROM prediction_snapshots WHERE canonical_hash=?", (digest,)
    ).fetchone()
    if row:
        return int(row["id"]), False
    cur = conn.execute(
        """
        INSERT INTO prediction_snapshots (
            generated_at,model_version_id,market_snapshot_id,forecast_snapshot_ids_json,
            bracket_probability,raw_probability,executable_side,executable_ask,edge,
            data_quality,canonical_hash,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            payload["generated_at"], model_version_id, market_snapshot_id,
            canonical_json(payload["forecast_snapshot_ids"]), float(bracket_probability),
            float(raw_probability), executable_side, float(executable_ask), edge,
            data_quality, digest, SCHEMA_VERSION,
        ),
    )
    return int(cur.lastrowid), True


def record_market_resolution(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    condition_id: str | None,
    city: str,
    target_date: date,
    winning_label: str,
    winning_lower: float | None,
    winning_upper: float | None,
    winning_unit: str,
    resolution_url: str,
    declared_station: str | None,
    declared_precision: float | None,
    resolved_at: datetime,
    collected_at: datetime,
    source: Any,
) -> tuple[int, bool]:
    exact = exact_value_for_interval(winning_lower, winning_upper, declared_precision)
    normalized_station = normalize_station_id(declared_station)
    resolution_provider = official_resolution_provider(city)
    # Identity only. ``resolved_at`` is derived from Gamma's mutable updatedAt,
    # so hashing it would mint a second row for the same outcome on every
    # re-collection and double-count that market in every metric.
    payload = {
        "event_id": event_id,
        "condition_id": condition_id,
        "city": city,
        "target_date": target_date.isoformat(),
        "winning_label": winning_label,
        "winning_lower": winning_lower,
        "winning_upper": winning_upper,
        "winning_unit": winning_unit,
    }
    digest = canonical_hash(payload)
    resolved_iso = iso_utc(parse_utc(resolved_at))
    existing = conn.execute(
        "SELECT * FROM market_resolutions WHERE canonical_hash=?", (digest,)
    ).fetchone()
    if existing:
        # The outcome is immutable; the declared metadata around it is not, so
        # corrections are applied in place. The resolution time only ever moves
        # earlier, since a later updatedAt must not loosen the look-ahead filter
        # in training_forecast_rows.
        precision = (
            declared_precision if declared_precision is not None
            else existing["declared_precision"]
        )
        station = declared_station or existing["declared_station"]
        normalized_station = normalize_station_id(station)
        earliest_resolved = iso_utc(min(parse_utc(existing["resolved_at"]), parse_utc(resolved_iso)))
        conn.execute(
            """
            UPDATE market_resolutions
            SET declared_station=?, normalized_station_id=?, resolution_provider=?,
                declared_precision=?, exact_rounded_value=?,
                resolution_url=?, resolved_at=?, source_json=?,
                reconciliation_status='pending', reconciliation_detail=NULL,
                reconciled_daily_observation_id=NULL
            WHERE id=?
            """,
            (
                station, normalized_station, resolution_provider, precision,
                exact_value_for_interval(winning_lower, winning_upper, precision),
                resolution_url, earliest_resolved,
                canonical_json(source), existing["id"],
            ),
        )
        return int(existing["id"]), False
    cur = conn.execute(
        """
        INSERT INTO market_resolutions (
            event_id,condition_id,city,target_date,winning_label,winning_lower,
            winning_upper,winning_unit,exact_rounded_value,resolution_url,
            declared_station,normalized_station_id,resolution_provider,declared_precision,
            resolved_at,collected_at,reconciliation_status,source_json,canonical_hash,schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id, condition_id, city, target_date.isoformat(), winning_label,
            winning_lower, winning_upper, winning_unit, exact, resolution_url,
            declared_station, normalized_station, resolution_provider, declared_precision,
            resolved_iso, iso_utc(parse_utc(collected_at)), "pending", canonical_json(source),
            digest, SCHEMA_VERSION,
        ),
    )
    return int(cur.lastrowid), True
