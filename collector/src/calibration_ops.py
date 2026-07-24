"""Collection, reconciliation, training, promotion, and archive operations."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import sqlite3
from collections import Counter, defaultdict
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

import httpx
import numpy as np

from . import config
from .calibration import EMOSParams, TrainingData, crps_gaussian, train_emos
from .ensemble import _fetch_batch_model
from .ledger import (
    ForecastSnapshotInput,
    LEGACY_MODEL_VERSION,
    SCHEMA_VERSION,
    aggregate_daily_observation,
    canonical_hash,
    canonical_json,
    iso_utc,
    parse_utc,
    reconcile_resolution,
    record_forecast_snapshot,
    record_forecast_summary,
    record_market_resolution,
    record_station_observation,
    table_counts,
    target_day_start_utc,
    training_forecast_rows,
    utc_now,
)
from .markets import extract_resolution_metadata, match_city, parse_bracket_label, parse_target_date
from .paper_trader import get_db, init_db
from .settlement import fetch_resolved_weather_events

log = logging.getLogger(__name__)

PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
AVIATION_WEATHER_URL = "https://aviationweather.gov/api/data/metar"
LEAD_BUCKETS = ("in_day", "0_24h", "24_48h", "48_72h", "72h_plus")
RETRY_BACKOFF_MAX_DAYS = 7


def _parse_timestamp(value: Any, *, assume_utc: bool = False) -> datetime | None:
    """Parse a provider timestamp, or return None.

    Never substitutes a stand-in time: an observation stamped with the moment we
    happened to fetch it is not an observation, and silently recording one
    corrupts every lead and local-day calculation downstream.  ``assume_utc`` is
    only for providers that document naive timestamps as UTC.
    """
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return parse_utc(text)
    except ValueError:
        pass
    if not assume_utc:
        return None
    try:
        naive = datetime.fromisoformat(text)
    except ValueError:
        return None
    return naive.replace(tzinfo=timezone.utc) if naive.tzinfo is None else naive.astimezone(timezone.utc)


def _dt(value: Any, fallback: datetime | None = None) -> datetime:
    """Parse a timestamp, falling back only where the caller has a defensible default."""
    parsed = _parse_timestamp(value)
    if parsed is not None:
        return parsed
    return fallback or utc_now()


async def collect_prospective_forecasts(
    client: httpx.AsyncClient,
    city_slugs: Sequence[str] | None = None,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Capture complete member values from the live Ensemble API."""
    slugs = list(city_slugs or config.CITIES)
    seen = utc_now()
    inserted = unchanged = failed = 0
    for model in config.ENSEMBLE_MODELS:
        batches = await _fetch_batch_model(client, slugs, model)
        with get_db(db_path) as conn:
            for city, by_date in batches.items():
                for target, members in by_date.items():
                    values = [member.daily_max for member in members if member.model == model]
                    try:
                        _, created = record_forecast_snapshot(
                            conn,
                            ForecastSnapshotInput(
                                city=city,
                                target_date=target,
                                seen_at=seen,
                                provider="open-meteo-ensemble",
                                model=model,
                                members_c=values,
                                source_endpoint=config.ENSEMBLE_API_BASE,
                                # The Ensemble API does not expose a run time, so
                                # the capture cutoff is the lead basis and the row
                                # records issue_time_verified=0.
                                run_time=None,
                                quality_detail="prospective capture cutoff; provider run time not exposed by endpoint",
                            ),
                        )
                    except ValueError as exc:
                        failed += 1
                        log.warning(
                            f"Rejected {model} forecast for {city} {target}: {exc}"
                        )
                    else:
                        inserted += int(created)
                        unchanged += int(not created)
    return {"inserted": inserted, "unchanged": unchanged, "failed": failed}


async def collect_previous_run_summaries(
    client: httpx.AsyncClient,
    city_slug: str,
    start: date,
    end: date,
    db_path: Path | None = None,
    model: str = "ecmwf_ifs025",
) -> dict[str, int]:
    """Collect deterministic fixed-lead summaries with verifiable lead offsets.

    These rows are useful for deterministic audit/bias work, but are marked
    ``deterministic_untrainable`` and cannot enter an ensemble dataset.
    """
    city = config.CITIES[city_slug]
    variables = [f"temperature_2m_previous_day{n}" for n in range(1, 8)]
    params = {
        "latitude": city.lat,
        "longitude": city.lon,
        "hourly": ",".join(variables),
        "models": model,
        "temperature_unit": "celsius",
        "timezone": city.timezone,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    response = await client.get(PREVIOUS_RUNS_URL, params=params, timeout=60)
    response.raise_for_status()
    hourly = response.json().get("hourly", {})
    times = hourly.get("time", [])
    per_day: dict[tuple[date, int], list[float]] = defaultdict(list)
    for idx, time_text in enumerate(times):
        local_date = datetime.fromisoformat(time_text).date()
        for offset, variable in enumerate(variables, start=1):
            values = hourly.get(variable, [])
            if idx < len(values) and values[idx] is not None:
                per_day[(local_date, offset)].append(float(values[idx]))
    inserted = unchanged = 0
    seen = utc_now()
    with get_db(db_path) as conn:
        for (target, offset), values in per_day.items():
            if not values:
                continue
            issued = target_day_start_utc(target, city.timezone) - timedelta(days=offset)
            _, created = record_forecast_summary(
                conn,
                city=city_slug,
                target_date=target,
                issued_at=issued,
                seen_at=seen,
                provider="open-meteo-previous-runs",
                model=f"{model}:previous_day{offset}",
                value_c=max(values),
                source_endpoint=PREVIOUS_RUNS_URL,
            )
            inserted += int(created)
            unchanged += int(not created)
    return {"inserted": inserted, "unchanged": unchanged}


async def collect_nws_observations(
    client: httpx.AsyncClient,
    city_slug: str,
    target: date,
    db_path: Path | None = None,
) -> int:
    city = config.CITIES[city_slug]
    tz = ZoneInfo(city.timezone)
    start = datetime(target.year, target.month, target.day, tzinfo=tz)
    end = start + timedelta(days=1)
    url = f"https://api.weather.gov/stations/{city.station}/observations"
    response = await client.get(
        url,
        params={"start": iso_utc(start), "end": iso_utc(end)},
        headers={"Accept": "application/geo+json", "User-Agent": config.USER_AGENT},
        timeout=config.HTTP_TIMEOUT,
    )
    response.raise_for_status()
    inserted = 0
    received = utc_now()
    with get_db(db_path) as conn:
        for feature in response.json().get("features", []):
            props = feature.get("properties", {})
            value = props.get("temperature", {}).get("value")
            stamp = props.get("timestamp")
            if value is None or not stamp:
                continue
            _, created = record_station_observation(
                conn,
                city=city_slug,
                station_id=city.station,
                provider="nws",
                observed_at=parse_utc(stamp),
                received_at=received,
                temperature_c=float(value),
                unit_reported="C",
                precision=0.1,
                source_url=url,
                raw=feature,
            )
            inserted += int(created)
        try:
            aggregate_daily_observation(
                conn, city=city_slug, target_date=target, provider="nws",
                station_id=city.station, declared_precision=1.0,
                rounded_unit=city.temp_unit,
            )
        except ValueError as exc:
            log.info(f"NWS {city_slug} {target}: no daily observation ({exc})")
    return inserted


async def collect_metar_observations(
    client: httpx.AsyncClient,
    city_slug: str,
    target: date,
    db_path: Path | None = None,
) -> int:
    city = config.CITIES[city_slug]
    if target < utc_now().date() - timedelta(days=15):
        return 0
    end = datetime.combine(target + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    response = await client.get(
        AVIATION_WEATHER_URL,
        params={"ids": city.station, "format": "json", "hours": 36, "date": iso_utc(end)},
        headers={"User-Agent": config.USER_AGENT},
        timeout=config.HTTP_TIMEOUT,
    )
    response.raise_for_status()
    received = utc_now()
    inserted = 0
    with get_db(db_path) as conn:
        data = response.json()
        if not isinstance(data, list):
            data = []
        unparseable = 0
        for item in data:
            # obsTime is an unambiguous epoch; reportTime is a naive UTC string,
            # so prefer the former and only ever treat the latter as UTC.
            stamp = item.get("obsTime")
            if stamp is None:
                stamp = item.get("reportTime")
            temp = item.get("temp")
            if stamp is None or temp is None:
                continue
            observed = _parse_timestamp(stamp, assume_utc=True)
            if observed is None:
                unparseable += 1
                continue
            if observed.astimezone(ZoneInfo(city.timezone)).date() != target:
                continue
            _, created = record_station_observation(
                conn,
                city=city_slug,
                station_id=city.station,
                provider="aviationweather-metar",
                observed_at=observed,
                received_at=received,
                temperature_c=float(temp),
                unit_reported="C",
                precision=1.0,
                source_url=AVIATION_WEATHER_URL,
                raw=item,
            )
            inserted += int(created)
        if unparseable:
            log.warning(
                f"METAR {city_slug} {target}: dropped {unparseable} readings with "
                f"unparseable timestamps"
            )
        try:
            aggregate_daily_observation(
                conn, city=city_slug, target_date=target,
                provider="aviationweather-metar", station_id=city.station,
                declared_precision=1.0, rounded_unit=city.temp_unit,
            )
        except ValueError as exc:
            log.info(f"METAR {city_slug} {target}: no daily observation ({exc})")
    return inserted


def _json_list(raw: Any) -> list[Any]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return raw if isinstance(raw, list) else []


async def collect_gamma_resolutions(
    client: httpx.AsyncClient,
    target: date,
    db_path: Path | None = None,
) -> dict[str, int]:
    events = await fetch_resolved_weather_events(client, target)
    inserted = unchanged = 0
    collected = utc_now()
    with get_db(db_path) as conn:
        for event in events:
            city_slug = match_city(event.get("title", ""))
            event_date = parse_target_date(event.get("title", ""))
            if not city_slug or event_date != target:
                continue
            winners: list[dict[str, Any]] = []
            for market in event.get("markets", []):
                if market.get("umaResolutionStatus") != "resolved":
                    continue
                prices = _json_list(market.get("outcomePrices", []))
                if len(prices) >= 2 and float(prices[0]) > 0.5:
                    winners.append(market)
            if len(winners) != 1:
                # 0 winners = not fully resolved yet; 2+ = contradictory Gamma
                # data. Either way, never guess which bracket won.
                log.warning(
                    f"Gamma resolution for {city_slug} {target} has {len(winners)} "
                    f"winning markets; skipped (event {event.get('id')})"
                )
                continue
            winner = winners[0]
            label = winner.get("groupItemTitle") or winner.get("question") or ""
            lower, upper, unit = parse_bracket_label(label)
            if not unit:
                continue
            metadata_url, declared_station, declared_precision = extract_resolution_metadata(event, city_slug)
            resolution_url = (
                winner.get("resolutionSource") or metadata_url
                or f"https://polymarket.com/event/{event.get('slug', '')}"
            )
            resolved = _dt(
                winner.get("resolvedAt") or winner.get("updatedAt")
                or event.get("updatedAt"), collected,
            )
            rid, created = record_market_resolution(
                conn,
                event_id=str(event.get("id", "")),
                condition_id=winner.get("conditionId"),
                city=city_slug,
                target_date=target,
                winning_label=label,
                winning_lower=lower,
                winning_upper=upper,
                winning_unit=unit,
                resolution_url=resolution_url,
                declared_station=declared_station,
                declared_precision=declared_precision,
                resolved_at=resolved,
                collected_at=collected,
                source=event,
            )
            inserted += int(created)
            unchanged += int(not created)
            reconcile_resolution(conn, rid)
    return {"inserted": inserted, "unchanged": unchanged}


def _cities_for_date(conn: sqlite3.Connection, target: date) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT city FROM signals WHERE target_date=?
        UNION SELECT DISTINCT city FROM trades WHERE target_date=?
        UNION SELECT DISTINCT city FROM market_snapshots WHERE target_date=?
        """,
        (target.isoformat(), target.isoformat(), target.isoformat()),
    ).fetchall()
    return [str(row["city"]) for row in rows if row["city"] in config.CITIES]


async def backfill_range(
    start: date,
    end: date,
    db_path: Path | None = None,
) -> dict[str, int]:
    if end < start:
        raise ValueError("--to must not precede --from")
    summary: Counter[str] = Counter()
    async with httpx.AsyncClient(headers={"User-Agent": config.USER_AGENT}) as client:
        current = start
        while current <= end:
            with get_db(db_path) as conn:
                cities = _cities_for_date(conn, current)
            try:
                result = await collect_gamma_resolutions(client, current, db_path)
                summary["resolutions"] += result["inserted"]
            except httpx.HTTPError:
                summary["resolution_failures"] += 1
            for city_slug in cities:
                try:
                    if config.CITIES[city_slug].resolution_adapter == "nws":
                        summary["station_readings"] += await collect_nws_observations(
                            client, city_slug, current, db_path
                        )
                    else:
                        summary["station_readings"] += await collect_metar_observations(
                            client, city_slug, current, db_path
                        )
                except httpx.HTTPError:
                    summary["observation_failures"] += 1
                try:
                    result = await collect_previous_run_summaries(
                        client, city_slug, current, current, db_path
                    )
                    summary["forecast_summaries"] += result["inserted"]
                except httpx.HTTPError:
                    summary["forecast_failures"] += 1
            reconciliation = reconcile_all(current, db_path)
            summary.update({f"reconciled_{key}": value for key, value in reconciliation.items()})
            with get_db(db_path) as conn:
                for city_slug in cities:
                    row = conn.execute(
                        """SELECT reconciliation_status FROM market_resolutions
                           WHERE city=? AND target_date=? ORDER BY resolved_at,id LIMIT 1""",
                        (city_slug, current.isoformat()),
                    ).fetchone()
                    record_resolution_retry(
                        conn, city_slug, current,
                        str(row["reconciliation_status"] if row else "resolution_missing"),
                    )
            current += timedelta(days=1)
    return dict(summary)


def reconcile_all(target: date | None = None, db_path: Path | None = None) -> dict[str, int]:
    counts: Counter[str] = Counter()
    with get_db(db_path) as conn:
        if target:
            rows = conn.execute(
                "SELECT id FROM market_resolutions WHERE target_date=?", (target.isoformat(),)
            ).fetchall()
        else:
            rows = conn.execute("SELECT id FROM market_resolutions").fetchall()
        for row in rows:
            counts[reconcile_resolution(conn, int(row["id"]))] += 1
    return dict(counts)


_UNRESOLVED_SQL = """
    WITH expected AS (
        SELECT city,target_date FROM trades
        UNION SELECT city,target_date FROM signals
        UNION SELECT city,target_date FROM market_snapshots
    )
    SELECT e.city,e.target_date,s.attempt_count,s.last_attempt_at,s.last_result,
           s.next_retry_at
    FROM expected e
    LEFT JOIN resolution_retry_state s
      ON s.city=e.city AND s.target_date=e.target_date
    WHERE NOT EXISTS (
        SELECT 1 FROM market_resolutions r
        WHERE r.city=e.city AND r.target_date=e.target_date
          AND r.reconciliation_status='matched'
          AND r.reconciled_daily_observation_id IS NOT NULL
    )
    ORDER BY e.target_date,e.city
"""


def unresolved_city_dates(
    db_path: Path | None = None,
    *,
    now: datetime | None = None,
    due_only: bool = False,
) -> list[dict[str, Any]]:
    now = now or utc_now()
    with get_db(db_path) as conn:
        rows = conn.execute(_UNRESOLVED_SQL).fetchall()
    items = []
    for row in rows:
        next_retry = str(row["next_retry_at"] or iso_utc(now))
        if due_only and parse_utc(next_retry) > now:
            continue
        items.append({
            "city": str(row["city"]),
            "target_date": str(row["target_date"]),
            "attempt_count": int(row["attempt_count"] or 0),
            "last_attempt_at": row["last_attempt_at"],
            "last_result": row["last_result"],
            "next_retry_at": next_retry,
        })
    return items


def record_resolution_retry(
    conn: sqlite3.Connection,
    city: str,
    target: date,
    result: str,
    *,
    now: datetime | None = None,
) -> None:
    now = now or utc_now()
    matched = conn.execute(
        """SELECT 1 FROM market_resolutions
           WHERE city=? AND target_date=? AND reconciliation_status='matched'
             AND reconciled_daily_observation_id IS NOT NULL LIMIT 1""",
        (city, target.isoformat()),
    ).fetchone()
    if matched:
        conn.execute(
            "DELETE FROM resolution_retry_state WHERE city=? AND target_date=?",
            (city, target.isoformat()),
        )
        return
    previous = conn.execute(
        "SELECT attempt_count FROM resolution_retry_state WHERE city=? AND target_date=?",
        (city, target.isoformat()),
    ).fetchone()
    attempts = int(previous["attempt_count"] if previous else 0) + 1
    delay_days = min(2 ** (attempts - 1), RETRY_BACKOFF_MAX_DAYS)
    conn.execute(
        """INSERT INTO resolution_retry_state(
               city,target_date,attempt_count,last_attempt_at,last_result,next_retry_at
           ) VALUES(?,?,?,?,?,?)
           ON CONFLICT(city,target_date) DO UPDATE SET
               attempt_count=excluded.attempt_count,
               last_attempt_at=excluded.last_attempt_at,
               last_result=excluded.last_result,
               next_retry_at=excluded.next_retry_at""",
        (city, target.isoformat(), attempts, iso_utc(now), result,
         iso_utc(now + timedelta(days=delay_days))),
    )


def unresolved_target_dates(
    db_path: Path | None = None,
    max_age_days: int | None = None,
    now: datetime | None = None,
) -> list[date]:
    """All retry-due dates; no unresolved city/date is ever abandoned."""
    del max_age_days
    return sorted({
        date.fromisoformat(item["target_date"])
        for item in unresolved_city_dates(db_path, now=now, due_only=True)
    })


def abandoned_target_dates(
    db_path: Path | None = None,
    max_age_days: int | None = None,
    now: datetime | None = None,
) -> list[date]:
    """Compatibility shim: schema v3 never permanently abandons retries."""
    del db_path, max_age_days, now
    return []


def collection_status(db_path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or utc_now()
    with get_db(db_path) as conn:
        counts = table_counts(conn)
        latest = conn.execute(
            "SELECT MAX(last_seen_at) AS value FROM forecast_snapshots WHERE quality_status='ok' AND schema_version=3"
        ).fetchone()["value"]
        member_rows = conn.execute(
            """SELECT model, member_count, quality_status, last_seen_at
               FROM forecast_snapshots WHERE schema_version=3 AND id IN (
                 SELECT MAX(id) FROM forecast_snapshots WHERE schema_version=3 GROUP BY city, model
               ) ORDER BY model"""
        ).fetchall()
        unresolved_items = unresolved_city_dates(db_path, now=now)
        unresolved = sorted({date.fromisoformat(item["target_date"]) for item in unresolved_items})
        retry_due = unresolved_target_dates(db_path, now=now)
        discrepancies = int(conn.execute(
            "SELECT COUNT(*) AS n FROM market_resolutions WHERE reconciliation_status='discrepancy'"
        ).fetchone()["n"])
        # Markets whose rounding rule or station was never declared can never
        # reconcile, so they need to be visible rather than silently stuck.
        missing_metadata = int(conn.execute(
            """SELECT COUNT(*) AS n FROM market_resolutions
               WHERE reconciliation_status='missing_resolution_metadata'"""
        ).fetchone()["n"])
        models = [dict(row) for row in conn.execute(
            "SELECT id,status,algorithm,lead_basis,lead_bucket,created_at FROM model_versions ORDER BY created_at"
        ).fetchall()]
        recent_runs = conn.execute(
            "SELECT COUNT(DISTINCT substr(completed_at,1,10)) AS days, "
            "SUM(expected_markets) AS expected, SUM(captured_markets) AS captured "
            "FROM collection_runs WHERE completed_at>=?",
            (iso_utc(now - timedelta(days=7)),),
        ).fetchone()
    freshness_minutes = None
    if latest:
        freshness_minutes = (now - parse_utc(latest)).total_seconds() / 60
    expected_missing = []
    for row in member_rows:
        expected = config.MODEL_MEMBER_COUNTS.get(row["model"])
        if expected is not None and row["member_count"] < expected:
            expected_missing.append({"model": row["model"], "expected": expected, "actual": row["member_count"]})
    old_items = [
        item for item in unresolved_items
        if (now.date() - date.fromisoformat(item["target_date"])).days > 3
    ]
    old_unresolved = sorted({item["target_date"] for item in old_items})
    expected = int(recent_runs["expected"] or 0)
    captured = int(recent_runs["captured"] or 0)
    archive_manifests = sorted((config.DATA_DIR / "archive").glob("????-??/manifest.json"))
    return {
        "counts": counts,
        "forecast_freshness_minutes": freshness_minutes,
        "forecast_stale": freshness_minutes is None or freshness_minutes > 30,
        "member_count_failures": expected_missing,
        "unresolved_dates": [d.isoformat() for d in unresolved],
        "unresolved_items": unresolved_items,
        "retry_due_dates": [d.isoformat() for d in retry_due],
        "unresolved_older_than_3d": old_unresolved,
        "unresolved_items_older_than_3d": old_items,
        "reconciliation_discrepancies": discrepancies,
        "missing_resolution_metadata": missing_metadata,
        "models": models,
        "scan_coverage_7d": captured / expected if expected else 0.0,
        "scan_days_7d": int(recent_runs["days"] or 0),
        "archive_latest": str(archive_manifests[-1]) if archive_manifests else None,
        "archive_verified": _verify_archive_manifests(),
    }


def chronological_rolling_splits(
    target_dates: Sequence[date],
    folds: int = 4,
    holdout_days: int = 28,
) -> tuple[list[tuple[set[date], set[date]]], set[date]]:
    """Build expanding-window folds without splitting a target date."""
    unique = sorted(set(target_dates))
    if len(unique) <= holdout_days + folds:
        return [], set(unique[-min(holdout_days, len(unique)):])
    holdout = set(unique[-holdout_days:])
    development = unique[:-holdout_days]
    chunks = [list(chunk) for chunk in np.array_split(development, folds + 1) if len(chunk)]
    splits: list[tuple[set[date], set[date]]] = []
    for idx in range(1, min(len(chunks), folds + 1)):
        train = {d for chunk in chunks[:idx] for d in chunk}
        validation = set(chunks[idx])
        if train and validation:
            splits.append((train, validation))
    return splits[:folds], holdout


def _pooled_rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    """Pool every provider/model member array from one capture sighting.

    A training observation is one city/day/route/cutoff, matching the pooled
    ensemble distribution used by serving. Only the latest eligible sighting
    for each city/day in the requested route is retained.
    """
    sightings: dict[tuple[str, str, str, str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        key = (
            str(row["city"]), str(row["target_date"]), str(row["lead_basis"]),
            str(row["lead_bucket"]), str(row["issued_at"]),
        )
        sightings[key].append(row)
    pooled: list[dict[str, Any]] = []
    for group in sightings.values():
        values = [
            float(value)
            for row in group
            for value in json.loads(row["member_values_json"])
        ]
        if len(values) < 2:
            continue
        item = dict(group[0])
        item.update({
            "id": min(int(row["id"]) for row in group),
            "snapshot_ids": sorted(int(row["id"]) for row in group),
            "provider": "pooled",
            "model": "pooled",
            "member_count": len(values),
            "member_values_json": canonical_json(values),
            "mean_c": float(np.mean(values)),
            "std_c": float(np.std(values, ddof=1)),
            "spread_c": max(values) - min(values),
        })
        pooled.append(item)
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in pooled:
        key = (str(row["city"]), str(row["target_date"]), str(row["lead_bucket"]))
        old = latest.get(key)
        if old is None or parse_utc(row["issued_at"]) > parse_utc(old["issued_at"]):
            latest[key] = row
    return sorted(latest.values(), key=lambda row: (row["target_date"], row["city"]))


def _training_data(rows: Sequence[sqlite3.Row]) -> TrainingData:
    return TrainingData(
        ens_means=np.array([float(row["mean_c"]) for row in rows]),
        ens_stds=np.array([float(row["std_c"]) for row in rows]),
        observations=np.array([float(row["max_temperature_c"]) for row in rows]),
    )


def _score_rows(
    params: EMOSParams, rows: Sequence[sqlite3.Row]
) -> tuple[list[float], list[float]]:
    candidate: list[float] = []
    raw: list[float] = []
    for row in rows:
        obs = float(row["max_temperature_c"])
        mu, sigma = params.predict(float(row["mean_c"]), float(row["std_c"]))
        candidate.append(crps_gaussian(mu, sigma, obs))
        raw.append(crps_gaussian(float(row["mean_c"]), max(float(row["std_c"]), .1), obs))
    return candidate, raw


def _rolling_scores(rows: Sequence[sqlite3.Row], scope: str) -> dict[str, Any]:
    """Expanding-window CRPS plus a scored holdout, for one model scope."""
    dates = [date.fromisoformat(row["target_date"]) for row in rows]
    splits, holdout = chronological_rolling_splits(dates)
    candidate: list[float] = []
    raw: list[float] = []
    for train_dates, validation_dates in splits:
        train_rows = [r for r in rows if date.fromisoformat(r["target_date"]) in train_dates]
        validation_rows = [r for r in rows if date.fromisoformat(r["target_date"]) in validation_dates]
        if len(train_rows) < 10 or not validation_rows:
            continue
        fold_candidate, fold_raw = _score_rows(
            train_emos(_training_data(train_rows), city=scope), validation_rows
        )
        candidate += fold_candidate
        raw += fold_raw
    # The reserved holdout is scored rather than merely set aside: fit once on
    # everything preceding it, then predict it.
    holdout_candidate: list[float] = []
    holdout_raw: list[float] = []
    development = [r for r in rows if date.fromisoformat(r["target_date"]) not in holdout]
    holdout_rows = [r for r in rows if date.fromisoformat(r["target_date"]) in holdout]
    if len(development) >= 10 and holdout_rows:
        holdout_candidate, holdout_raw = _score_rows(
            train_emos(_training_data(development), city=scope), holdout_rows
        )
    return {
        "candidate": candidate, "raw": raw, "folds": len(splits),
        "holdout_candidate": holdout_candidate, "holdout_raw": holdout_raw,
        "holdout": holdout,
    }


def _rolling_metrics(groups: dict[str, Sequence[sqlite3.Row]]) -> dict[str, Any]:
    """Validate each stored model with its own CV, then pool the scores.

    Scoring one pooled model while storing per-city models would report metrics
    for a model that is never served, so every scope in ``groups`` is validated
    with exactly the model that scope will use.
    """
    candidate: list[float] = []
    raw: list[float] = []
    holdout_candidate: list[float] = []
    holdout_raw: list[float] = []
    folds = 0
    holdout: set[date] = set()
    per_scope: dict[str, float] = {}
    for scope, rows in sorted(groups.items()):
        scored = _rolling_scores(rows, scope)
        candidate += scored["candidate"]
        raw += scored["raw"]
        holdout_candidate += scored["holdout_candidate"]
        holdout_raw += scored["holdout_raw"]
        folds = max(folds, scored["folds"])
        holdout |= scored["holdout"]
        if scored["candidate"]:
            per_scope[scope] = float(np.mean(scored["candidate"]))

    def mean_or_none(values: Sequence[float]) -> float | None:
        return float(np.mean(values)) if values else None

    return {
        "rolling_folds": folds,
        "validation_scopes": sorted(groups),
        "validation_crps": mean_or_none(candidate),
        "raw_validation_crps": mean_or_none(raw),
        "per_scope_validation_crps": per_scope,
        "holdout_crps": mean_or_none(holdout_candidate),
        "raw_holdout_crps": mean_or_none(holdout_raw),
        "holdout_dates": len(holdout),
        "holdout_start": min(holdout).isoformat() if holdout else None,
    }


def dataset_quality(
    conn: sqlite3.Connection, snapshot_ids: Sequence[int]
) -> dict[str, Any]:
    """Measure provenance, reconciliation, and completeness from the ledger.

    These feed promotion gates, so they are measured rather than asserted: a
    gate seeded with a literal ``True`` is not a gate.
    """
    empty = {
        "provenance_complete": False,
        "provenance_missing": 0,
        "reconciliation_checks_pass": False,
        "reconciliation_discrepancies": 0,
        "data_completeness": 0.0,
        "resolutions_in_window": 0,
        "covered_city_days": 0,
    }
    if not snapshot_ids:
        return empty
    ids = [int(v) for v in snapshot_ids]
    placeholders = ",".join("?" * len(ids))
    provenance = conn.execute(
        f"""SELECT COUNT(*) AS total,
                   SUM(CASE WHEN city IS NULL OR target_date IS NULL OR provider IS NULL
                                 OR model IS NULL OR source_endpoint IS NULL
                                 OR canonical_hash IS NULL OR content_hash IS NULL
                                 OR sighting_identity IS NULL OR schema_version != 3
                                 OR issued_at IS NULL
                                 OR lead_basis IS NULL OR lead_basis NOT IN
                                    ('run_time','capture_cutoff')
                            THEN 1 ELSE 0 END) AS missing
            FROM forecast_snapshots WHERE id IN ({placeholders})""",
        ids,
    ).fetchone()
    if not provenance["total"]:
        return empty
    window = conn.execute(
        f"""SELECT MIN(target_date) AS first, MAX(target_date) AS last
            FROM forecast_snapshots WHERE id IN ({placeholders})""",
        ids,
    ).fetchone()
    resolutions = conn.execute(
        """SELECT COUNT(DISTINCT city || '|' || target_date) AS total,
                  SUM(CASE WHEN reconciliation_status='discrepancy' THEN 1 ELSE 0 END) AS bad
           FROM market_resolutions WHERE target_date BETWEEN ? AND ?""",
        (window["first"], window["last"]),
    ).fetchone()
    covered = int(conn.execute(
        f"""SELECT COUNT(DISTINCT city || '|' || target_date) AS n
            FROM forecast_snapshots WHERE id IN ({placeholders})""",
        ids,
    ).fetchone()["n"])
    resolved_city_days = int(resolutions["total"] or 0)
    discrepancies = int(resolutions["bad"] or 0)
    return {
        # Every training row carries a full provenance chain.
        "provenance_complete": not int(provenance["missing"] or 0),
        "provenance_missing": int(provenance["missing"] or 0),
        # No resolved market in the window disagrees with station truth.
        "reconciliation_checks_pass": discrepancies == 0 and resolved_city_days > 0,
        "reconciliation_discrepancies": discrepancies,
        # What fraction of resolved city-days in the window we can actually train on.
        "data_completeness": (
            min(covered / resolved_city_days, 1.0) if resolved_city_days else 0.0
        ),
        "resolutions_in_window": resolved_city_days,
        "covered_city_days": covered,
    }


def train_candidate(
    lead: str,
    db_path: Path | None = None,
    lead_basis: str = "capture_cutoff",
) -> str:
    if lead not in LEAD_BUCKETS:
        raise ValueError(f"invalid lead bucket: {lead}")
    if lead_basis not in {"run_time", "capture_cutoff"}:
        raise ValueError(f"invalid lead basis: {lead_basis}")
    cutoff = utc_now()
    with get_db(db_path) as conn:
        rows = _pooled_rows(training_forecast_rows(
            conn, lead_bucket_name=lead, cutoff=cutoff, lead_basis=lead_basis
        ))
        window_start = (cutoff - timedelta(days=365)).date()
        rows = [row for row in rows if date.fromisoformat(row["target_date"]) >= window_start]
        by_city: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            by_city[str(row["city"])].append(row)
        eligible_city: dict[str, list[sqlite3.Row]] = {}
        for city, city_rows in by_city.items():
            days = sorted(date.fromisoformat(row["target_date"]) for row in city_rows)
            if len(days) >= 90 and (days[-1] - days[0]).days >= 89:
                eligible_city[city] = city_rows
        included_group = {city: city_rows for city, city_rows in by_city.items() if len(city_rows) >= 15}
        grouped_rows = [row for city_rows in included_group.values() for row in city_rows]
        grouped_dates = {row["target_date"] for row in grouped_rows}
        parameters: dict[str, Any] = {"city": {}, "grouped": None}
        snapshot_ids = sorted({
            snapshot_id for row in rows for snapshot_id in row["snapshot_ids"]
        })
        metrics: dict[str, Any] = {
            "resolved_dates": len({row["target_date"] for row in rows}),
            "independent_city_days": len(rows),
            # Measured against the ledger, never asserted.
            **dataset_quality(conn, snapshot_ids),
        }
        algorithm = "raw-ensemble-fallback"
        for city, city_rows in eligible_city.items():
            params = train_emos(_training_data(city_rows), city=city)
            parameters["city"][city] = params.to_dict()
        if eligible_city:
            algorithm = "emos-city"
            metrics.update(_rolling_metrics(eligible_city))
        elif len(grouped_rows) >= 120 and len(grouped_dates) >= 60:
            params = train_emos(_training_data(grouped_rows), city="grouped")
            parameters["grouped"] = params.to_dict()
            algorithm = "emos-grouped"
            metrics.update(_rolling_metrics({"grouped": grouped_rows}))
        else:
            metrics.update({"rolling_folds": 0, "validation_crps": None, "raw_validation_crps": None, "holdout_dates": 0})
        raw_crps = metrics.get("raw_validation_crps")
        model_crps = metrics.get("validation_crps")
        metrics["crps_improvement_vs_raw"] = (
            (raw_crps - model_crps) / raw_crps
            if raw_crps and model_crps is not None else 0.0
        )
        manifest = {
            "forecast_snapshot_ids": snapshot_ids,
            "row_count": len(rows),
            "independent_city_days": len(rows),
            "sha256": canonical_hash(snapshot_ids),
            "cutoff": iso_utc(cutoff),
            "lead_basis": lead_basis,
            "lead_bucket": lead,
            "legacy_tables_included": False,
        }
        body = {
            "algorithm": algorithm,
            "lead_basis": lead_basis,
            "lead_bucket": lead,
            "parameters": parameters,
            "manifest": manifest,
            "cutoff": iso_utc(cutoff),
        }
        version_id = f"{algorithm}-{canonical_hash(body)[:16]}"
        conn.execute(
            """
            INSERT OR IGNORE INTO model_versions (
                id,created_at,algorithm,scope_type,scope_value,lead_basis,lead_bucket,
                parameters_json,training_cutoff,dataset_manifest_json,metrics_json,
                status,schema_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'candidate',?)
            """,
            (
                version_id, iso_utc(), algorithm, "multi-city", "eligible",
                lead_basis, lead, canonical_json(parameters), iso_utc(cutoff),
                canonical_json(manifest), canonical_json(metrics), SCHEMA_VERSION,
            ),
        )
    return version_id


def _verify_archive_manifests() -> bool:
    archive_root = config.DATA_DIR / "archive"
    manifests = sorted(archive_root.glob("????-??/manifest.json"))
    if not manifests:
        return False
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for item in manifest.get("tables", {}).values():
                file_path = manifest_path.parent / item["file"]
                if not file_path.exists():
                    return False
                if hashlib.sha256(file_path.read_bytes()).hexdigest() != item["sha256"]:
                    return False
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return False
    return True


def operational_acceptance(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate the seven-day pre-promotion collection acceptance contract."""
    now = now or utc_now()
    start = iso_utc(now - timedelta(days=7))
    runs = conn.execute(
        "SELECT * FROM collection_runs WHERE completed_at>=? ORDER BY completed_at", (start,)
    ).fetchall()
    run_days = {str(row["completed_at"])[:10] for row in runs}
    expected = sum(int(row["expected_markets"]) for row in runs)
    captured = sum(int(row["captured_markets"]) for row in runs)
    coverage = captured / expected if expected else 0.0
    provenance = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN city IS NULL OR target_date IS NULL OR provider IS NULL
                                OR model IS NULL OR source_endpoint IS NULL
                                OR canonical_hash IS NULL OR content_hash IS NULL
                                OR sighting_identity IS NULL THEN 1 ELSE 0 END) AS missing
           FROM forecast_snapshots WHERE quality_status='ok' AND schema_version=3"""
    ).fetchone()
    resolution_rows = conn.execute(
        "SELECT resolved_at,collected_at FROM market_resolutions"
    ).fetchall()
    timely = sum(
        (parse_utc(row["collected_at"]) - parse_utc(row["resolved_at"])).total_seconds() <= 48 * 3600
        for row in resolution_rows
    )
    resolution_ratio = timely / len(resolution_rows) if resolution_rows else 0.0
    latest = conn.execute(
        "SELECT MAX(last_seen_at) AS latest FROM forecast_snapshots WHERE quality_status='ok' AND schema_version=3"
    ).fetchone()["latest"]
    freshness = (now - parse_utc(latest)).total_seconds() / 60 if latest else math.inf
    result = {
        "run_days": len(run_days),
        "scan_coverage": coverage,
        "provenance_complete": bool(provenance["total"] and not provenance["missing"]),
        "forecast_fresh": freshness <= 30,
        "resolution_within_48h": resolution_ratio,
        "archive_verified": _verify_archive_manifests(),
    }
    result["passed"] = bool(
        result["run_days"] >= 7
        and result["scan_coverage"] >= .95
        and result["provenance_complete"]
        and result["forecast_fresh"]
        and result["resolution_within_48h"] >= .95
        and result["archive_verified"]
    )
    return result


def _binary_metrics(probabilities: Sequence[float], outcomes: Sequence[int]) -> dict[str, float | int | None]:
    if not probabilities:
        return {"n": 0, "brier": None, "log_loss": None, "reliability_error": None}
    p = np.clip(np.array(probabilities, dtype=float), 1e-9, 1 - 1e-9)
    y = np.array(outcomes, dtype=float)
    brier = float(np.mean((p - y) ** 2))
    log_loss = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    errors = []
    for lower in np.arange(0.0, 1.0, .1):
        mask = (p >= lower) & (p < lower + .1)
        if mask.any():
            errors.append(abs(float(p[mask].mean()) - float(y[mask].mean())) * int(mask.sum()))
    return {"n": len(p), "brier": brier, "log_loss": log_loss, "reliability_error": sum(errors) / len(p)}


def evaluate_model(
    model_version: str,
    db_path: Path | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    manager = nullcontext(conn) if conn is not None else get_db(db_path)
    with manager as conn:
        model = conn.execute("SELECT * FROM model_versions WHERE id=?", (model_version,)).fetchone()
        if not model:
            raise ValueError(f"unknown model version: {model_version}")
        active_row = active_model_for_route(
            conn, str(model["lead_basis"]), str(model["lead_bucket"])
        )
        active_id = active_row["id"]
        predictions = conn.execute(
            """
            SELECT p.*,m.target_date,m.city,m.bracket_lower,m.bracket_upper,m.bracket_unit,
                   r.winning_lower,r.winning_upper,r.winning_unit
            FROM prediction_snapshots p
            JOIN market_snapshots m ON m.id=p.market_snapshot_id
            JOIN market_resolutions r ON r.id = (
                SELECT r2.id FROM market_resolutions r2
                WHERE r2.city=m.city AND r2.target_date=m.target_date
                  AND r2.reconciliation_status='matched'
                  AND r2.reconciled_daily_observation_id IS NOT NULL
                ORDER BY r2.resolved_at ASC, r2.id ASC LIMIT 1
            )
            WHERE p.model_version_id=? AND p.generated_at < r.resolved_at
            """,
            (model_version,),
        ).fetchall()
        control_by_market: dict[int, float] = {}
        if active_id and active_id != model_version:
            control_by_market = {
                int(row["market_snapshot_id"]): float(row["bracket_probability"])
                for row in conn.execute(
                    "SELECT market_snapshot_id,bracket_probability FROM prediction_snapshots WHERE model_version_id=?",
                    (active_id,),
                ).fetchall()
            }
        route_by_forecast = {
            int(row["id"]): (str(row["lead_basis"]), str(row["lead_bucket"]))
            for row in conn.execute(
                "SELECT id,lead_basis,lead_bucket FROM forecast_snapshots"
            ).fetchall()
        }
        probs: list[float] = []
        raw_probs: list[float] = []
        outcomes: list[int] = []
        control_probs: list[float] = []
        control_outcomes: list[int] = []
        segments: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
        pnl = 0.0
        city_days: set[tuple[str, str]] = set()
        dates: set[str] = set()
        for row in predictions:
            forecast_routes = {
                route_by_forecast.get(int(forecast_id))
                for forecast_id in json.loads(row["forecast_snapshot_ids_json"])
            } - {None}
            if forecast_routes != {(str(model["lead_basis"]), str(model["lead_bucket"]))}:
                continue
            won = int(
                row["bracket_unit"] == row["winning_unit"]
                and row["bracket_lower"] == row["winning_lower"]
                and row["bracket_upper"] == row["winning_upper"]
            )
            probability = float(row["bracket_probability"])
            probs.append(probability)
            raw_probs.append(float(row["raw_probability"]))
            outcomes.append(won)
            control_probability = control_by_market.get(int(row["market_snapshot_id"]))
            if control_probability is not None:
                control_probs.append(control_probability)
                control_outcomes.append(won)
                segments[f"city:{row['city']}"].append((probability, control_probability, won))
                for _, bucket in forecast_routes:
                    segments[f"lead:{bucket}"].append((probability, control_probability, won))
            side_won = won if row["executable_side"] == "YES" else 1 - won
            ask = float(row["executable_ask"])
            pnl += (1.0 - ask) / ask if side_won and ask > 0 else -1.0
            city_days.add((row["city"], row["target_date"]))
            dates.add(row["target_date"])
        candidate = _binary_metrics(probs, outcomes)
        raw = _binary_metrics(raw_probs, outcomes)
        control = _binary_metrics(control_probs, control_outcomes)
        degradations = []
        for values in segments.values():
            if len(values) < 30:
                continue
            candidate_brier = float(np.mean([(p - outcome) ** 2 for p, _, outcome in values]))
            control_brier = float(np.mean([(p - outcome) ** 2 for _, p, outcome in values]))
            if control_brier > 0:
                degradations.append((candidate_brier - control_brier) / control_brier)
        metrics = json.loads(model["metrics_json"])
        acceptance = operational_acceptance(conn)
        # Re-measure data quality now rather than trusting what training wrote:
        # the ledger may have gained discrepancies since the candidate was built.
        manifest = json.loads(model["dataset_manifest_json"] or "{}")
        metrics.update({
            "gamma": candidate,
            "raw_gamma": raw,
            "control_gamma": control,
            "segment_max_degradation": max(degradations, default=0.0),
            "economic_unit_pnl": pnl,
            "resolved_dates": len(dates),
            "independent_city_days": len(city_days),
            "collection_acceptance": acceptance,
            "archive_verified": acceptance["archive_verified"],
            **dataset_quality(conn, manifest.get("forecast_snapshot_ids", [])),
        })
        # Elapsed shadow time, so a backfill cannot satisfy the duration gate by
        # importing history in a single afternoon.
        now = utc_now()
        shadow_since = model["shadow_since"] or iso_utc(now)
        metrics["shadow_days"] = (now - parse_utc(shadow_since)).total_seconds() / 86400.0
        conn.execute(
            """
            UPDATE model_versions
            SET metrics_json=?,
                status=CASE WHEN status='candidate' THEN 'shadow' ELSE status END,
                shadow_since=COALESCE(shadow_since, ?)
            WHERE id=?
            """,
            (canonical_json(metrics), shadow_since, model_version),
        )
    return metrics


def promotion_gates(candidate_metrics: dict[str, Any], control_metrics: dict[str, Any]) -> dict[str, bool]:
    gamma = candidate_metrics.get("gamma", {})
    raw = candidate_metrics.get("raw_gamma", {})
    control_gamma = candidate_metrics.get("control_gamma") or control_metrics.get("gamma", {})
    def improves(candidate: Any, baseline: Any, fraction: float = .02) -> bool:
        return bool(
            isinstance(candidate, (int, float)) and isinstance(baseline, (int, float))
            and math.isfinite(candidate) and math.isfinite(baseline) and candidate <= baseline * (1 - fraction)
        )
    def nonworse(candidate: Any, baseline: Any) -> bool:
        return bool(
            isinstance(candidate, (int, float)) and isinstance(baseline, (int, float))
            and math.isfinite(candidate) and math.isfinite(baseline) and candidate <= baseline
        )
    return {
        "shadow_duration": (
            candidate_metrics.get("resolved_dates", 0) >= 28
            and candidate_metrics.get("shadow_days", 0) >= 28
        ),
        "shadow_sample": candidate_metrics.get("independent_city_days", 0) >= 60,
        "brier_vs_raw": improves(gamma.get("brier"), raw.get("brier")),
        "brier_vs_control": improves(gamma.get("brier"), control_gamma.get("brier")),
        "crps_vs_raw": candidate_metrics.get("crps_improvement_vs_raw", 0) >= .02,
        "holdout_not_worse_than_raw": nonworse(
            candidate_metrics.get("holdout_crps"), candidate_metrics.get("raw_holdout_crps")
        ),
        "log_loss_nonworse": nonworse(gamma.get("log_loss"), control_gamma.get("log_loss")),
        "reliability_nonworse": nonworse(gamma.get("reliability_error"), control_gamma.get("reliability_error")),
        "segments": candidate_metrics.get("segment_max_degradation", math.inf) <= .05,
        "provenance": candidate_metrics.get("provenance_complete") is True,
        "completeness": candidate_metrics.get("data_completeness", 0) >= .95,
        "reconciliation": candidate_metrics.get("reconciliation_checks_pass") is True,
        "seven_day_collection": candidate_metrics.get("collection_acceptance", {}).get("passed") is True,
        "archive": candidate_metrics.get("archive_verified") is True,
    }


def promote_model(model_version: str, db_path: Path | None = None) -> dict[str, bool]:
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        candidate = conn.execute(
            "SELECT * FROM model_versions WHERE id=?", (model_version,)
        ).fetchone()
        if not candidate or candidate["status"] not in {"candidate", "shadow"}:
            raise ValueError("model must be a candidate or shadow version")
        route = (str(candidate["lead_basis"]), str(candidate["lead_bucket"]))
        if route[0] not in {"run_time", "capture_cutoff"} or route[1] not in LEAD_BUCKETS:
            raise ValueError("candidate does not declare a promotable lead route")

        evaluated_at = utc_now()
        candidate_metrics = evaluate_model(
            model_version, db_path, conn=conn
        )
        candidate = conn.execute(
            "SELECT * FROM model_versions WHERE id=?", (model_version,)
        ).fetchone()
        control = active_model_for_route(conn, *route)
        control_metrics = json.loads(control["metrics_json"] or "{}")
        gates = promotion_gates(candidate_metrics, control_metrics)
        report = {
            "evaluated_at": iso_utc(evaluated_at),
            "route": {"lead_basis": route[0], "lead_bucket": route[1]},
            "candidate_id": model_version,
            "control_id": str(control["id"]),
            "candidate_metrics": candidate_metrics,
            "control_metrics": control_metrics,
            "gates": gates,
        }
        if not all(gates.values()):
            raise ValueError(
                "promotion gates failed: "
                + ", ".join(key for key, passed in gates.items() if not passed)
            )
        now = iso_utc()
        if control["id"] != LEGACY_MODEL_VERSION:
            conn.execute(
                "UPDATE model_versions SET status='retired',retired_at=? WHERE id=?",
                (now, control["id"]),
            )
        conn.execute(
            """UPDATE model_versions
               SET status='active',activated_at=?,predecessor_id=? WHERE id=?""",
            (now, control["id"], model_version),
        )
        conn.execute(
            """INSERT INTO model_transitions(
                   changed_at,from_model_id,to_model_id,action,gate_report_json
               ) VALUES(?,?,?,'promote',?)""",
            (now, control["id"], model_version, canonical_json(report)),
        )
    return gates


def rollback_model(model_version: str | None = None, db_path: Path | None = None) -> str:
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            """SELECT * FROM model_versions WHERE status='active'
               AND lead_basis IN ('run_time','capture_cutoff')
               ORDER BY activated_at DESC,id DESC LIMIT 1"""
        ).fetchone()
        if not active:
            raise ValueError("no active model")
        target_id = model_version or active["predecessor_id"]
        if not target_id:
            last = conn.execute(
                "SELECT from_model_id FROM model_transitions WHERE action='promote' AND to_model_id=? ORDER BY id DESC LIMIT 1",
                (active["id"],),
            ).fetchone()
            target_id = last["from_model_id"] if last else None
        target = conn.execute("SELECT * FROM model_versions WHERE id=?", (target_id,)).fetchone() if target_id else None
        if not target:
            raise ValueError("rollback target not found")
        if target["id"] != LEGACY_MODEL_VERSION and (
            target["lead_basis"] != active["lead_basis"]
            or target["lead_bucket"] != active["lead_bucket"]
        ):
            raise ValueError("rollback target belongs to a different lead route")
        now = iso_utc()
        conn.execute("UPDATE model_versions SET status='retired',retired_at=? WHERE id=?", (now, active["id"]))
        if target_id != LEGACY_MODEL_VERSION:
            conn.execute(
                "UPDATE model_versions SET status='active',activated_at=?,retired_at=NULL WHERE id=?",
                (now, target_id),
            )
        conn.execute(
            "INSERT INTO model_transitions(changed_at,from_model_id,to_model_id,action,gate_report_json) VALUES(?,?,?,'rollback','{}')",
            (now, active["id"], target_id),
        )
    return str(target_id)


def archive_month(month: str, db_path: Path | None = None, out_dir: Path | None = None) -> Path:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("archive requires pyarrow; install collector requirements") from exc
    datetime.strptime(month, "%Y-%m")
    output = out_dir or config.DATA_DIR / "archive" / month
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    previous: dict[str, Any] | None = None
    if manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = None
    revision = int((previous or {}).get("revision", 0)) + 1
    date_columns = {
        "forecast_snapshots": "target_date", "market_snapshots": "target_date",
        "prediction_snapshots": "generated_at", "station_observations": "observed_at",
        "daily_observations": "target_date", "market_resolutions": "target_date",
        "model_versions": "created_at", "model_transitions": "changed_at",
        "collection_runs": "started_at", "resolution_retry_state": "target_date",
    }
    revision_dir = output / f"r{revision}"
    tables: dict[str, Any] = {}
    with get_db(db_path) as conn:
        cohort = conn.execute(
            """WITH expected AS (
                   SELECT city,target_date FROM trades
                   UNION SELECT city,target_date FROM signals
                   UNION SELECT city,target_date FROM market_snapshots
               )
               SELECT e.city,e.target_date,COUNT(r.id) AS resolution_count,
                      SUM(CASE WHEN r.reconciliation_status='matched'
                                AND r.reconciled_daily_observation_id IS NOT NULL
                               THEN 1 ELSE 0 END) AS matched_count
               FROM expected e
               LEFT JOIN market_resolutions r
                 ON r.city=e.city AND r.target_date=e.target_date
               WHERE substr(e.target_date,1,7)=?
               GROUP BY e.city,e.target_date
               ORDER BY e.target_date,e.city""",
            (month,),
        ).fetchall()
        invalid = [
            dict(row) for row in cohort
            if int(row["resolution_count"] or 0) != 1
            or int(row["matched_count"] or 0) != 1
        ]
        nonmatched = int(conn.execute(
            """SELECT COUNT(*) AS n FROM market_resolutions
               WHERE substr(target_date,1,7)=?
                 AND (reconciliation_status!='matched'
                      OR reconciled_daily_observation_id IS NULL)""",
            (month,),
        ).fetchone()["n"])
        if invalid or nonmatched:
            raise RuntimeError(
                f"{month} is not finalizable under matched-only policy: "
                f"{len(invalid)} expected city/dates incomplete, "
                f"{nonmatched} non-matched resolutions"
            )
        provenance = [dict(row) for row in conn.execute(
            """SELECT city,target_date,id AS resolution_id,
                      reconciled_daily_observation_id,normalized_station_id,
                      resolution_provider,reconciliation_status
               FROM market_resolutions
               WHERE substr(target_date,1,7)=? AND reconciliation_status='matched'
               ORDER BY target_date,city,id""",
            (month,),
        ).fetchall()]
        finalization = {
            "policy_version": "gamma-station-matched-v1",
            "expected_city_dates": len(cohort),
            "accepted_reconciliation_statuses": ["matched"],
            "matched_expected_city_dates": len(cohort),
            "reconciliation_provenance": provenance,
        }
        revision_dir.mkdir(parents=True, exist_ok=False)
        for table, column in date_columns.items():
            rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM {table} WHERE substr({column},1,7)=? ORDER BY 1", (month,)
            ).fetchall()]
            expected = int(conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE substr({column},1,7)=?", (month,)
            ).fetchone()["n"])
            if expected != len(rows):
                raise RuntimeError(
                    f"{table} changed during archive: exported {len(rows)}, database has {expected}"
                )
            destination = revision_dir / f"{table}.parquet"
            table_data = pa.Table.from_pylist(rows) if rows else pa.table({"_empty": pa.array([], type=pa.null())})
            pq.write_table(table_data, destination, compression="zstd")
            verified = pq.read_table(destination).num_rows if rows else 0
            if verified != expected:
                raise RuntimeError(f"archive row-count mismatch for {table}: {verified} != {expected}")
            tables[table] = {
                "rows": expected,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "file": f"r{revision}/{table}.parquet",
            }

    def content(spec: dict[str, Any]) -> dict[str, tuple[int, str]]:
        return {name: (item["rows"], item["sha256"]) for name, item in spec.items()}

    if previous and content(previous.get("tables", {})) == content(tables):
        shutil.rmtree(revision_dir)
        return manifest_path
    manifest = {
        "month": month, "schema_version": SCHEMA_VERSION, "revision": revision,
        "tables": tables,
        "finalization": finalization,
        "supersedes": (previous or {}).get("revision"),
    }
    manifest_path.write_bytes((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    return manifest_path


def migrate_legacy_inventory(
    legacy_path: Path | None = None,
    db_path: Path | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    primary = db_path or config.DB_PATH
    legacy = legacy_path or config.COLLECTOR_ROOT / "data" / "wethr.db"
    if not legacy.exists():
        return {"legacy_path": str(legacy), "exists": False}
    result: dict[str, Any] = {"legacy_path": str(legacy), "exists": True, "apply": apply, "tables": {}}
    with sqlite3.connect(legacy) as source:
        source.row_factory = sqlite3.Row
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("trades", "signals", "historical_forecasts", "historical_observations"):
            result["tables"][table] = source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in tables else 0
    if not apply:
        return result
    backup_dir = config.DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(primary, backup_dir / f"wethr-primary-pre-legacy-{stamp}.db")
    shutil.copy2(legacy, backup_dir / f"wethr-secondary-pre-legacy-{stamp}.db")
    copied: dict[str, int] = {}
    skipped: dict[str, str] = {}
    with get_db(primary) as conn:
        conn.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
        for table, key_cols in {
            "trades": ("created_at", "city", "target_date", "bracket_label", "side", "entry_price", "size_usd"),
            "signals": ("city", "target_date", "bracket_label"),
            "historical_forecasts": ("city", "target_date", "lead_days", "model"),
            "historical_observations": ("city", "target_date"),
        }.items():
            target_cols = [row[1] for row in conn.execute(f"PRAGMA main.table_info({table})") if row[1] != "id"]
            source_cols = {row[1] for row in conn.execute(f"PRAGMA legacy.table_info({table})")}
            common = [col for col in target_cols if col in source_cols]
            if not common:
                # The table is absent from one side — the history tables are
                # created lazily — which would otherwise build a malformed
                # "INSERT INTO t () SELECT FROM ..." and fail the migration.
                skipped[table] = "table missing or no shared columns"
                copied[table] = 0
                continue
            before = conn.total_changes
            cols = ",".join(common)
            # ``IS`` is NULL-safe in SQLite. Plain ``=`` yields NULL for a NULL
            # key, making NOT EXISTS true and re-copying the same row on every
            # run, which would silently break idempotency.
            keys = " AND ".join(f"p.{key} IS l.{key}" for key in key_cols)
            conn.execute(
                f"INSERT INTO main.{table} ({cols}) SELECT {','.join('l.'+c for c in common)} FROM legacy.{table} l WHERE NOT EXISTS (SELECT 1 FROM main.{table} p WHERE {keys})"
            )
            copied[table] = conn.total_changes - before
        # Newly copied trades predate the ledger, so tag them as legacy control
        # rows. init_calibration_ledger already ran, long before this copy.
        conn.execute(
            "UPDATE trades SET model_version_id=? WHERE model_version_id IS NULL",
            (LEGACY_MODEL_VERSION,),
        )
        result["untagged_trades"] = int(conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE model_version_id IS NULL"
        ).fetchone()["n"])
    result["copied"] = copied
    result["skipped"] = skipped
    return result


def persist_pipeline_forecasts(
    forecasts: dict[str, dict[date, Any]],
    db_path: Path | None = None,
) -> dict[tuple[str, date], list[int]]:
    """Persist per-model members already fetched by the trading pipeline."""
    result: dict[tuple[str, date], list[int]] = defaultdict(list)
    with get_db(db_path) as conn:
        for city, by_date in forecasts.items():
            for target, forecast in by_date.items():
                grouped: dict[str, list[float]] = defaultdict(list)
                for member in forecast.members:
                    grouped[member.model].append(float(member.daily_max))
                for model, values in grouped.items():
                    try:
                        snapshot_id, _ = record_forecast_snapshot(
                            conn,
                            ForecastSnapshotInput(
                                city=city, target_date=target,
                                seen_at=forecast.fetch_time,
                                provider="open-meteo-ensemble", model=model,
                                members_c=values, source_endpoint=config.ENSEMBLE_API_BASE,
                                run_time=None,
                                quality_detail="prospective pipeline capture cutoff",
                            ),
                        )
                    except ValueError as exc:
                        log.warning(
                            f"Pipeline forecast for {city} {target} model {model} "
                            f"not persisted: {exc}"
                        )
                        continue
                    result[(city, target)].append(snapshot_id)
    return result


def _book_quote(book: Any) -> tuple[float | None, float | None]:
    if not isinstance(book, dict):
        return None, None
    def prices(side: str) -> list[float]:
        values = []
        for level in book.get(side, []) or []:
            raw = level.get("price") if isinstance(level, dict) else None
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                pass
        return values
    bids, asks = prices("bids"), prices("asks")
    return (max(bids) if bids else None, min(asks) if asks else None)


async def persist_market_quotes(
    client: httpx.AsyncClient,
    markets: Sequence[Any],
    forecasts: dict[str, dict[date, Any]] | None = None,
    forecast_ids: dict[tuple[str, date], list[int]] | None = None,
    db_path: Path | None = None,
) -> int:
    """Capture CLOB best bids/asks; Gamma prices are audit fields, not asks."""
    from .ledger import record_market_snapshot
    captured = utc_now()
    inserted = 0
    expected_markets = sum(len(market.brackets) for market in markets)
    captured_books = 0
    book_failures = 0
    quotes: list[tuple[Any, Any, float | None, float | None, float | None, float | None]] = []
    for market in markets:
        for bracket in market.brackets:
            yes_book = no_book = None
            try:
                if bracket.token_id:
                    response = await client.get(
                        f"{config.CLOB_API_BASE}/book",
                        params={"token_id": bracket.token_id}, timeout=config.HTTP_TIMEOUT,
                    )
                    response.raise_for_status()
                    yes_book = response.json()
                if bracket.no_token_id:
                    response = await client.get(
                        f"{config.CLOB_API_BASE}/book",
                        params={"token_id": bracket.no_token_id}, timeout=config.HTTP_TIMEOUT,
                    )
                    response.raise_for_status()
                    no_book = response.json()
            except httpx.HTTPError as exc:
                book_failures += 1
                log.warning(
                    f"CLOB book fetch failed for {market.city} {bracket.label}: {exc!r}"
                )
            yes_bid, yes_ask = _book_quote(yes_book)
            no_bid, no_ask = _book_quote(no_book)
            if yes_ask is not None and no_ask is not None:
                captured_books += 1
            quotes.append((market, bracket, yes_bid, yes_ask, no_bid, no_ask))

    # One connection and one transaction for the whole scan, so a scan is
    # persisted atomically rather than dribbling out per bracket while the
    # network calls above are still in flight.
    with get_db(db_path) as conn:
        for market, bracket, yes_bid, yes_ask, no_bid, no_ask in quotes:
            spread = yes_ask - yes_bid if yes_ask is not None and yes_bid is not None else None
            market_snapshot_id, created = record_market_snapshot(
                conn,
                captured_at=captured, condition_id=bracket.condition_id,
                event_id=market.event_id, city=market.city,
                target_date=market.target_date.isoformat(), bracket_label=bracket.label,
                bracket_lower=bracket.lower, bracket_upper=bracket.upper,
                bracket_unit=bracket.unit, yes_best_bid=yes_bid, yes_best_ask=yes_ask,
                no_best_bid=no_bid, no_best_ask=no_ask, midpoint=bracket.market_prob,
                last_price=bracket.market_prob, volume=market.total_volume,
                liquidity=None, spread=spread,
                resolution_url=market.resolution_url or f"https://polymarket.com/event/{market.event_slug}",
                declared_precision=market.declared_precision, source_endpoint="gamma+clob",
            )
            inserted += int(created)
            if forecasts and forecast_ids:
                _materialize_predictions(
                    conn, market, bracket, market_snapshot_id, captured,
                    forecasts, forecast_ids, yes_ask, no_ask,
                )
    coverage = captured_books / expected_markets if expected_markets else 1.0
    if coverage < .95:
        log.warning(
            f"Scan captured {captured_books}/{expected_markets} executable books "
            f"({coverage:.1%}); {book_failures} book fetches failed"
        )
    with get_db(db_path) as conn:
        conn.execute(
            """INSERT INTO collection_runs(
                   started_at,completed_at,expected_markets,captured_markets,coverage,
                   missing_executable_books,status,detail_json
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (iso_utc(captured), iso_utc(), expected_markets, captured_books, coverage,
             expected_markets - captured_books, "ok" if coverage >= .95 else "incomplete",
             canonical_json({"source": "gamma+clob", "book_fetch_failures": book_failures})),
        )
    return inserted


def _forecast_route(
    conn: sqlite3.Connection,
    forecast_ids: Sequence[int],
) -> tuple[str, str] | None:
    if not forecast_ids:
        return None
    placeholders = ",".join("?" for _ in forecast_ids)
    rows = conn.execute(
        f"""SELECT DISTINCT lead_basis,lead_bucket FROM forecast_snapshots
            WHERE id IN ({placeholders}) AND schema_version=?
              AND content_hash IS NOT NULL AND sighting_identity IS NOT NULL""",
        [*map(int, forecast_ids), SCHEMA_VERSION],
    ).fetchall()
    routes = {(str(row["lead_basis"]), str(row["lead_bucket"])) for row in rows}
    return next(iter(routes)) if len(routes) == 1 else None


def active_model_for_route(
    conn: sqlite3.Connection,
    lead_basis: str,
    lead_bucket_name: str,
) -> sqlite3.Row:
    model = conn.execute(
        """SELECT * FROM model_versions
           WHERE status='active' AND lead_basis=? AND lead_bucket=?
           ORDER BY activated_at DESC,id LIMIT 1""",
        (lead_basis, lead_bucket_name),
    ).fetchone()
    if model is None:
        model = conn.execute(
            "SELECT * FROM model_versions WHERE id=?", (LEGACY_MODEL_VERSION,)
        ).fetchone()
    if model is None:
        raise RuntimeError("legacy control model is unavailable")
    return model


def selected_model_versions(
    forecast_ids: dict[tuple[str, date], list[int]],
    db_path: Path | None = None,
) -> dict[tuple[str, date], str]:
    selected: dict[tuple[str, date], str] = {}
    with get_db(db_path) as conn:
        for key, ids in forecast_ids.items():
            route = _forecast_route(conn, ids)
            model = active_model_for_route(conn, *route) if route else conn.execute(
                "SELECT * FROM model_versions WHERE id=?", (LEGACY_MODEL_VERSION,)
            ).fetchone()
            if model:
                selected[key] = str(model["id"])
    return selected


def apply_routed_probabilities(
    probabilities: Any,
    model_version_id: str,
    db_path: Path | None = None,
) -> Any:
    """Overlay the selected route model probabilities on the served brackets."""
    if model_version_id == LEGACY_MODEL_VERSION:
        return probabilities
    from .probability import BracketProbability

    market = probabilities.market
    with get_db(db_path) as conn:
        rows = conn.execute(
            """SELECT m.bracket_label,p.bracket_probability
               FROM prediction_snapshots p
               JOIN market_snapshots m ON m.id=p.market_snapshot_id
               WHERE p.model_version_id=? AND m.city=? AND m.target_date=?
               ORDER BY p.generated_at DESC,p.id DESC""",
            (model_version_id, market.city, market.target_date.isoformat()),
        ).fetchall()
    routed: dict[str, float] = {}
    for row in rows:
        routed.setdefault(str(row["bracket_label"]), float(row["bracket_probability"]))
    if not routed:
        return probabilities
    probabilities.brackets = [
        BracketProbability(
            bracket=bp.bracket,
            model_prob=routed.get(bp.bracket.label, bp.model_prob),
            market_prob=bp.market_prob,
            edge=routed.get(bp.bracket.label, bp.model_prob) - bp.market_prob,
            member_count=bp.member_count,
            total_members=bp.total_members,
            confidence=max(
                routed.get(bp.bracket.label, bp.model_prob),
                1.0 - routed.get(bp.bracket.label, bp.model_prob),
            ),
        )
        for bp in probabilities.brackets
    ]
    return probabilities


def _materialize_predictions(
    conn: sqlite3.Connection,
    market: Any,
    bracket: Any,
    market_snapshot_id: int,
    generated_at: datetime,
    forecasts: dict[str, dict[date, Any]],
    forecast_ids: dict[tuple[str, date], list[int]],
    yes_ask: float | None,
    no_ask: float | None,
) -> None:
    """Generate raw, active-control, and candidate probabilities per quote."""
    from .calibration import calibrated_bracket_probability
    from .ledger import record_prediction_snapshot
    from .probability import count_members_in_bracket

    forecast = forecasts.get(market.city, {}).get(market.target_date)
    ids = forecast_ids.get((market.city, market.target_date), [])
    if forecast is None or not ids:
        return
    unit = market.city_config.temp_unit
    values = forecast.daily_maxes(unit)
    if len(values) == 0:
        return
    raw_count = count_members_in_bracket(values, bracket.lower, bracket.upper)
    raw_probability = raw_count / len(values)
    route = _forecast_route(conn, ids)
    versions = conn.execute(
        "SELECT * FROM model_versions WHERE status IN ('active','candidate','shadow')"
    ).fetchall()
    for version in versions:
        is_control = version["id"] in {LEGACY_MODEL_VERSION, "raw-ensemble-v1"}
        if not is_control and (
            route is None
            or (str(version["lead_basis"]), str(version["lead_bucket"])) != route
        ):
            continue
        probability = raw_probability
        if version["id"] == LEGACY_MODEL_VERSION:
            has_emos = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='emos_params'"
            ).fetchone()
            params_row = conn.execute(
                "SELECT params_json FROM emos_params WHERE city=? AND lead_days=-1",
                (market.city,),
            ).fetchone() if has_emos else None
            params = EMOSParams.from_dict(json.loads(params_row["params_json"])) if params_row else None
            if params is not None:
                mu, sigma = params.predict(
                    float(np.mean(values)),
                    float(np.std(values, ddof=1)) if len(values) > 1 else 0.1,
                )
                probability = calibrated_bracket_probability(
                    bracket.lower, bracket.upper, mu, sigma
                )
        elif version["algorithm"].startswith("emos"):
            parameters = json.loads(version["parameters_json"])
            raw_params = parameters.get("city", {}).get(market.city) or parameters.get("grouped")
            if raw_params:
                params = EMOSParams.from_dict(raw_params)
                c_values = forecast.daily_maxes("C")
                mu, sigma = params.predict(
                    float(np.mean(c_values)),
                    float(np.std(c_values, ddof=1)) if len(c_values) > 1 else 0.1,
                )
                lower = bracket.lower
                upper = bracket.upper
                if unit == "F":
                    lower = (lower - 32) * 5 / 9 if lower is not None else None
                    upper = (upper - 32) * 5 / 9 if upper is not None else None
                probability = calibrated_bracket_probability(lower, upper, mu, sigma)
        choices = []
        if yes_ask is not None:
            choices.append((probability - yes_ask, "YES", yes_ask))
        if no_ask is not None:
            choices.append(((1 - probability) - no_ask, "NO", no_ask))
        if not choices:
            continue
        _, side, ask = max(choices)
        record_prediction_snapshot(
            conn,
            model_version_id=version["id"], market_snapshot_id=market_snapshot_id,
            forecast_snapshot_ids=ids, bracket_probability=probability,
            raw_probability=raw_probability, executable_side=side,
            executable_ask=ask, generated_at=generated_at,
        )
