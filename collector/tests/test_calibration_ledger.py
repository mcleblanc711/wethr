from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.calibration_ops import chronological_rolling_splits, promotion_gates
from src.history import fetch_historical_ensemble
from src.ledger import (
    ForecastSnapshotInput, aggregate_daily_observation, exact_value_for_interval,
    lead_bucket, record_forecast_snapshot, record_market_resolution,
    record_market_snapshot, record_station_observation, reconcile_resolution,
    round_to_precision, training_forecast_rows, value_in_interval,
)
from src.paper_trader import get_db, init_db

UTC = timezone.utc


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ledger.db"
    init_db(path)
    return path


def test_lead_bucket_boundaries():
    assert [lead_bucket(v) for v in (-.01, 0, 23.9, 24, 48, 72)] == [
        "in_day", "0_24h", "0_24h", "24_48h", "48_72h", "72h_plus"
    ]


def test_forecast_hash_dedup_and_content_versions(db_path):
    base = dict(
        city="london", target_date=date(2026, 7, 22),
        issued_at=datetime(2026, 7, 20, tzinfo=UTC),
        seen_at=datetime(2026, 7, 20, 1, tzinfo=UTC), provider="fixture",
        model="eps", members_c=[20, 21, 22], source_endpoint="fixture://ensemble",
        issue_time_verified=True,
    )
    with get_db(db_path) as conn:
        first, created = record_forecast_snapshot(conn, ForecastSnapshotInput(**base))
        second, created_again = record_forecast_snapshot(conn, ForecastSnapshotInput(
            **{**base, "issued_at": datetime(2026, 7, 20, 1, tzinfo=UTC), "seen_at": datetime(2026, 7, 20, 2, tzinfo=UTC)}
        ))
        third, changed = record_forecast_snapshot(conn, ForecastSnapshotInput(
            **{**base, "members_c": [20, 21, 23]}
        ))
        assert created and not created_again and first == second
        assert changed and third != first
        rows = conn.execute("SELECT content_version,last_seen_at FROM forecast_snapshots ORDER BY id").fetchall()
        assert [row["content_version"] for row in rows] == [1, 2]
        assert "T02:00:00" in rows[0]["last_seen_at"]


def test_deterministic_forecast_is_rejected(db_path):
    with get_db(db_path) as conn, pytest.raises(ValueError, match="deterministic"):
        record_forecast_snapshot(conn, ForecastSnapshotInput(
            city="london", target_date=date(2026, 7, 22),
            issued_at=datetime(2026, 7, 20, tzinfo=UTC),
            seen_at=datetime(2026, 7, 20, tzinfo=UTC), provider="fixture",
            model="det", members_c=[20], source_endpoint="fixture://det",
            issue_time_verified=True,
        ))


def test_precision_intervals_and_25c_regression():
    assert round_to_precision(24.5, 1) == 25
    assert exact_value_for_interval(25, 26, 1) == 25
    assert exact_value_for_interval(24, 26, 1) is None
    assert exact_value_for_interval(25, None, 1) is None
    assert value_in_interval(25, 25, 26)
    assert not value_in_interval(26, 25, 26)


def test_dst_local_day_aggregation(db_path):
    readings = [
        (datetime(2026, 3, 8, 4, 30, tzinfo=UTC), 99),
        (datetime(2026, 3, 8, 5, 30, tzinfo=UTC), 10),
        (datetime(2026, 3, 9, 3, 30, tzinfo=UTC), 20),
        (datetime(2026, 3, 9, 4, 30, tzinfo=UTC), 98),
    ]
    with get_db(db_path) as conn:
        for observed, value in readings:
            record_station_observation(
                conn, city="nyc", station_id="KLGA", provider="fixture",
                observed_at=observed, received_at=observed, temperature_c=value,
                unit_reported="C", source_url="fixture://station",
                raw={"at": observed.isoformat()},
            )
        daily_id = aggregate_daily_observation(
            conn, city="nyc", target_date=date(2026, 3, 8), provider="fixture",
            station_id="KLGA", declared_precision=1, rounded_unit="C",
        )
        row = conn.execute("SELECT * FROM daily_observations WHERE id=?", (daily_id,)).fetchone()
        assert row["max_temperature_c"] == 20
        assert len(json.loads(row["source_observation_ids_json"])) == 2


def test_snapshot_seen_after_resolution_cannot_train(db_path):
    target = date(2026, 7, 10)
    with get_db(db_path) as conn:
        record_station_observation(
            conn, city="london", station_id="EGLC", provider="fixture",
            observed_at=datetime(2026, 7, 10, 12, tzinfo=UTC),
            received_at=datetime(2026, 7, 10, 13, tzinfo=UTC), temperature_c=25.1,
            unit_reported="C", source_url="fixture://station", raw={"v": 25.1},
        )
        aggregate_daily_observation(
            conn, city="london", target_date=target, provider="fixture",
            station_id="EGLC", declared_precision=1, rounded_unit="C",
        )
        resolution_id, _ = record_market_resolution(
            conn, event_id="event", condition_id="condition", city="london",
            target_date=target, winning_label="25°C", winning_lower=25,
            winning_upper=26, winning_unit="C", resolution_url="fixture://gamma",
            declared_station="EGLC", declared_precision=1,
            resolved_at=datetime(2026, 7, 11, tzinfo=UTC),
            collected_at=datetime(2026, 7, 11, 1, tzinfo=UTC), source={"winner": "25°C"},
        )
        assert reconcile_resolution(conn, resolution_id) == "matched"
        for issued, seen, members in [
            (datetime(2026, 7, 9, tzinfo=UTC), datetime(2026, 7, 9, tzinfo=UTC), [24, 25, 26]),
            (datetime(2026, 7, 9, 6, tzinfo=UTC), datetime(2026, 7, 12, tzinfo=UTC), [25, 26, 27]),
        ]:
            record_forecast_snapshot(conn, ForecastSnapshotInput(
                city="london", target_date=target, issued_at=issued, seen_at=seen,
                provider="fixture", model="eps", members_c=members,
                source_endpoint="fixture://ensemble", issue_time_verified=True,
            ))
        rows = training_forecast_rows(conn)
        assert len(rows) == 1
        assert rows[0]["first_seen_at"].startswith("2026-07-09")


def test_market_snapshot_hash_dedup(db_path):
    fields = dict(
        captured_at=datetime(2026, 7, 20, tzinfo=UTC), condition_id="c", event_id="e",
        city="london", target_date="2026-07-21", bracket_label="25°C",
        bracket_lower=25, bracket_upper=26, bracket_unit="C", yes_best_bid=.4,
        yes_best_ask=.42, no_best_bid=.57, no_best_ask=.59, midpoint=.41,
        last_price=.41, volume=100, liquidity=50, spread=.02,
        resolution_url="fixture://rules", declared_precision=1,
        source_endpoint="fixture://market",
    )
    with get_db(db_path) as conn:
        first, created = record_market_snapshot(conn, **fields)
        second, duplicate = record_market_snapshot(conn, **{**fields, "captured_at": datetime(2026, 7, 20, 1, tzinfo=UTC)})
        third, changed = record_market_snapshot(conn, **{**fields, "yes_best_ask": .43})
        assert created and not duplicate and first == second
        assert changed and third != first


def test_chronological_splits_do_not_leak_target_dates():
    start = date(2026, 1, 1)
    dates = [start + timedelta(days=n) for n in range(100)]
    splits, holdout = chronological_rolling_splits(dates)
    assert len(holdout) == 28
    for train, validation in splits:
        assert train.isdisjoint(validation | holdout)
        assert validation.isdisjoint(holdout)
        assert max(train) < min(validation)


def test_migration_is_idempotent(db_path):
    init_db(db_path)
    init_db(db_path)
    with get_db(db_path) as conn:
        models = conn.execute("SELECT id,status FROM model_versions").fetchall()
        assert {row["id"] for row in models} == {"legacy-emos-2026-04-08", "raw-ensemble-v1"}
        assert sum(row["status"] == "active" for row in models) == 1
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        assert {"model_version_id", "prediction_snapshot_id", "strategy_version"} <= cols


def test_promotion_gate_contract():
    candidate = {
        "resolved_dates": 28, "independent_city_days": 60,
        "gamma": {"brier": .18, "log_loss": .5, "reliability_error": .04},
        "raw_gamma": {"brier": .20}, "crps_improvement_vs_raw": .03,
        "segment_max_degradation": .05, "provenance_complete": True,
        "data_completeness": .96, "reconciliation_checks_pass": True,
        "collection_acceptance": {"passed": True}, "archive_verified": True,
    }
    control = {"gamma": {"brier": .19, "log_loss": .51, "reliability_error": .04}}
    assert all(promotion_gates(candidate, control).values())


class FakeResponse:
    def raise_for_status(self):
        pass
    def json(self):
        return {"daily": {"time": ["2026-07-01"], "temperature_2m_max": [20]}}


class FakeClient:
    async def get(self, *args, **kwargs):
        return FakeResponse()


def test_history_never_invents_deterministic_spread(db_path):
    count = asyncio.run(fetch_historical_ensemble(
        FakeClient(), "london", date(2026, 7, 1), date(2026, 7, 1), db_path=db_path
    ))
    assert count == 0
    with get_db(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM historical_forecasts").fetchone()[0] == 0


def test_gamma_resolution_metadata_extraction():
    from src.markets import extract_resolution_metadata
    event = {
        "title": "Highest temperature in London?",
        "description": (
            "This market uses the weather station at EGLC. "
            "The result is rounded to the nearest whole degree. "
            "See https://example.test/rules."
        ),
        "markets": [],
    }
    url, station, precision = extract_resolution_metadata(event, "london")
    assert url == "https://example.test/rules"
    assert station == "EGLC"
    assert precision == 1.0


def test_missing_gamma_resolution_metadata_is_not_invented():
    from src.markets import extract_resolution_metadata
    assert extract_resolution_metadata({"title": "Highest temperature in London?"}, "london") == ("", None, None)
