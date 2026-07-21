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
        run_time=datetime(2026, 7, 20, tzinfo=UTC),
        seen_at=datetime(2026, 7, 20, 1, tzinfo=UTC), provider="fixture",
        model="eps", members_c=[20, 21, 22], source_endpoint="fixture://ensemble",
    )
    with get_db(db_path) as conn:
        first, created = record_forecast_snapshot(conn, ForecastSnapshotInput(**base))
        second, created_again = record_forecast_snapshot(conn, ForecastSnapshotInput(
            **{**base, "run_time": datetime(2026, 7, 20, 1, tzinfo=UTC), "seen_at": datetime(2026, 7, 20, 2, tzinfo=UTC)}
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
            run_time=datetime(2026, 7, 20, tzinfo=UTC),
            seen_at=datetime(2026, 7, 20, tzinfo=UTC), provider="fixture",
            model="det", members_c=[20], source_endpoint="fixture://det",
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
        # A full local day of hourly readings: a sparse day is deliberately not
        # station truth, which is covered by test_partial_day_is_not_station_truth.
        for hour in range(23):
            value = 25.1 if hour == 14 else 18.0
            record_station_observation(
                conn, city="london", station_id="EGLC", provider="fixture",
                observed_at=datetime(2026, 7, 10, hour, tzinfo=UTC),
                received_at=datetime(2026, 7, 10, hour, 30, tzinfo=UTC),
                temperature_c=value, unit_reported="C",
                source_url="fixture://station", raw={"v": value, "h": hour},
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
        for run_time, seen, members in [
            (datetime(2026, 7, 9, tzinfo=UTC), datetime(2026, 7, 9, tzinfo=UTC), [24, 25, 26]),
            (datetime(2026, 7, 9, 6, tzinfo=UTC), datetime(2026, 7, 12, tzinfo=UTC), [25, 26, 27]),
        ]:
            record_forecast_snapshot(conn, ForecastSnapshotInput(
                city="london", target_date=target, run_time=run_time, seen_at=seen,
                provider="fixture", model="eps", members_c=members,
                source_endpoint="fixture://ensemble",
            ))
        rows = training_forecast_rows(conn, lead_basis="run_time")
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


def _passing_candidate():
    return {
        "resolved_dates": 28, "independent_city_days": 60, "shadow_days": 30,
        "gamma": {"brier": .18, "log_loss": .5, "reliability_error": .04},
        "raw_gamma": {"brier": .20}, "crps_improvement_vs_raw": .03,
        "holdout_crps": .9, "raw_holdout_crps": 1.0,
        "segment_max_degradation": .05, "provenance_complete": True,
        "data_completeness": .96, "reconciliation_checks_pass": True,
        "collection_acceptance": {"passed": True}, "archive_verified": True,
    }


CONTROL = {"gamma": {"brier": .19, "log_loss": .51, "reliability_error": .04}}


def test_promotion_gate_contract():
    assert all(promotion_gates(_passing_candidate(), CONTROL).values())


@pytest.mark.parametrize("field,value,gate", [
    ("provenance_complete", False, "provenance"),
    ("reconciliation_checks_pass", False, "reconciliation"),
    ("data_completeness", .5, "completeness"),
    ("shadow_days", 3, "shadow_duration"),
    ("resolved_dates", 5, "shadow_duration"),
    ("independent_city_days", 10, "shadow_sample"),
    ("crps_improvement_vs_raw", .0, "crps_vs_raw"),
    ("holdout_crps", 1.4, "holdout_not_worse_than_raw"),
    ("segment_max_degradation", .5, "segments"),
    ("archive_verified", False, "archive"),
])
def test_promotion_gate_fails_on_degraded_input(field, value, gate):
    """Each gate must actually be able to fail — a gate seeded with a literal
    True in train_candidate is not a gate."""
    candidate = {**_passing_candidate(), field: value}
    gates = promotion_gates(candidate, CONTROL)
    assert gates[gate] is False
    assert not all(gates.values())


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


# --------------------------------------------------------------------------
# Provenance and truth-quality regressions
# --------------------------------------------------------------------------

def _seed_station_truth(conn, city, station, target, peak=25.1, hours=23):
    """A fully covered local day plus its resolution, reconciled."""
    for hour in range(hours):
        value = peak if hour == 14 else peak - 7
        record_station_observation(
            conn, city=city, station_id=station, provider="fixture",
            observed_at=datetime(target.year, target.month, target.day, hour, tzinfo=UTC),
            received_at=datetime(target.year, target.month, target.day, hour, 30, tzinfo=UTC),
            temperature_c=value, unit_reported="C", source_url="fixture://station",
            raw={"v": value, "h": hour},
        )
    aggregate_daily_observation(
        conn, city=city, target_date=target, provider="fixture",
        station_id=station, declared_precision=1, rounded_unit="C",
    )
    resolution_id, _ = record_market_resolution(
        conn, event_id="event", condition_id="condition", city=city,
        target_date=target, winning_label="25°C", winning_lower=25,
        winning_upper=26, winning_unit="C", resolution_url="fixture://gamma",
        declared_station=station, declared_precision=1,
        resolved_at=datetime(2026, 7, 11, tzinfo=UTC),
        collected_at=datetime(2026, 7, 11, 1, tzinfo=UTC), source={"winner": "25°C"},
    )
    reconcile_resolution(conn, resolution_id)
    return resolution_id


def test_naive_provider_timestamp_is_never_replaced_with_now():
    """AviationWeather reportTime is naive; stamping it with now() corrupts
    every local-day and lead calculation downstream."""
    from src.calibration_ops import _parse_timestamp
    assert _parse_timestamp("2026-07-19 12:53:00") is None
    assert _parse_timestamp("2026-07-19 12:53:00", assume_utc=True) == datetime(
        2026, 7, 19, 12, 53, tzinfo=UTC
    )
    assert _parse_timestamp(1752925980) == datetime.fromtimestamp(1752925980, tz=UTC)
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not-a-time", assume_utc=True) is None


def test_capture_cutoff_is_not_a_verified_issue_time(db_path):
    with get_db(db_path) as conn:
        snapshot_id, _ = record_forecast_snapshot(conn, ForecastSnapshotInput(
            city="london", target_date=date(2026, 7, 22),
            seen_at=datetime(2026, 7, 20, tzinfo=UTC), provider="fixture",
            model="eps", members_c=[20, 21, 22], source_endpoint="fixture://ensemble",
        ))
        row = conn.execute(
            "SELECT * FROM forecast_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        assert row["issue_time_verified"] == 0
        assert row["lead_basis"] == "capture_cutoff"
        assert row["capture_cutoff_at"] == row["issued_at"]


def test_training_never_mixes_lead_bases(db_path):
    target = date(2026, 7, 10)
    with get_db(db_path) as conn:
        _seed_station_truth(conn, "london", "EGLC", target)
        record_forecast_snapshot(conn, ForecastSnapshotInput(
            city="london", target_date=target, seen_at=datetime(2026, 7, 9, tzinfo=UTC),
            provider="fixture", model="capture", members_c=[24, 25, 26],
            source_endpoint="fixture://ensemble",
        ))
        record_forecast_snapshot(conn, ForecastSnapshotInput(
            city="london", target_date=target, seen_at=datetime(2026, 7, 9, tzinfo=UTC),
            run_time=datetime(2026, 7, 9, tzinfo=UTC), provider="fixture",
            model="run", members_c=[24, 25, 26], source_endpoint="fixture://ensemble",
        ))
        capture = training_forecast_rows(conn, lead_basis="capture_cutoff")
        runtime = training_forecast_rows(conn, lead_basis="run_time")
        assert [r["model"] for r in capture] == ["capture"]
        assert [r["model"] for r in runtime] == ["run"]
        with pytest.raises(ValueError, match="unsupported lead basis"):
            training_forecast_rows(conn, lead_basis="anything")


def test_partial_day_is_not_station_truth(db_path):
    with get_db(db_path) as conn:
        for hour in (11, 12, 13):
            record_station_observation(
                conn, city="london", station_id="EGLC", provider="fixture",
                observed_at=datetime(2026, 7, 10, hour, tzinfo=UTC),
                received_at=datetime(2026, 7, 10, hour, tzinfo=UTC),
                temperature_c=18.0, unit_reported="C",
                source_url="fixture://station", raw={"h": hour},
            )
        daily_id = aggregate_daily_observation(
            conn, city="london", target_date=date(2026, 7, 10), provider="fixture",
            station_id="EGLC", declared_precision=1, rounded_unit="C",
        )
        row = conn.execute(
            "SELECT quality_status FROM daily_observations WHERE id=?", (daily_id,)
        ).fetchone()
        assert row["quality_status"] == "partial_day"


def test_unresolved_sweep_is_bounded(db_path):
    """Markets that never resolve must leave the daily retry queue, or the
    sweep grows without bound and costs a network round trip per date forever."""
    from src.calibration_ops import abandoned_target_dates, unresolved_target_dates
    now = datetime(2026, 7, 20, tzinfo=UTC)
    with get_db(db_path) as conn:
        for target in ("2026-07-19", "2026-03-01"):
            conn.execute(
                """INSERT INTO trades (city,target_date,bracket_label,bracket_unit,
                                       side,entry_price,size_usd,model_prob,
                                       market_prob,edge,member_count,total_members,
                                       confidence,kelly_full,kelly_frac,settled)
                   VALUES ('london',?,'25°C','C','YES',.4,10,.5,.4,.1,20,50,.8,.1,.05,0)""",
                (target,),
            )
    recent = unresolved_target_dates(db_path, now=now)
    old = abandoned_target_dates(db_path, now=now)
    assert recent == [date(2026, 7, 19)]
    assert old == [date(2026, 3, 1)]


def test_archive_revises_instead_of_failing(db_path, tmp_path):
    """A late reconciliation must not make the monthly job fail permanently."""
    pytest.importorskip("pyarrow")
    from src.calibration_ops import archive_month
    out = tmp_path / "archive" / "2026-07"
    with get_db(db_path) as conn:
        _seed_station_truth(conn, "london", "EGLC", date(2026, 7, 10))
    first = archive_month("2026-07", db_path=db_path, out_dir=out)
    manifest = json.loads(first.read_text())
    assert manifest["revision"] == 1
    assert manifest["tables"]["market_resolutions"]["rows"] == 1

    # Re-running unchanged is a no-op, so the job stays idempotent.
    archive_month("2026-07", db_path=db_path, out_dir=out)
    assert json.loads(first.read_text())["revision"] == 1

    # A corrected reconciliation produces revision 2 rather than RuntimeError.
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE market_resolutions SET reconciliation_detail='corrected'"
        )
    archive_month("2026-07", db_path=db_path, out_dir=out)
    revised = json.loads(first.read_text())
    assert revised["revision"] == 2 and revised["supersedes"] == 1
    assert (out / "r1").exists() and (out / "r2").exists()


def test_archive_refuses_unreconciled_month(db_path, tmp_path):
    pytest.importorskip("pyarrow")
    from src.calibration_ops import archive_month
    with get_db(db_path) as conn:
        record_market_resolution(
            conn, event_id="e", condition_id="c", city="london",
            target_date=date(2026, 7, 10), winning_label="25°C", winning_lower=25,
            winning_upper=26, winning_unit="C", resolution_url="fixture://gamma",
            declared_station=None, declared_precision=None,
            resolved_at=datetime(2026, 7, 11, tzinfo=UTC),
            collected_at=datetime(2026, 7, 11, tzinfo=UTC), source={},
        )
    with pytest.raises(RuntimeError, match="unreconciled"):
        archive_month("2026-07", db_path=db_path, out_dir=tmp_path / "a")


def test_legacy_migration_is_idempotent_and_tags_trades(tmp_path, monkeypatch):
    from src import config as cfg
    from src.calibration_ops import migrate_legacy_inventory
    from src.ledger import LEGACY_MODEL_VERSION
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)

    legacy, primary = tmp_path / "legacy.db", tmp_path / "primary.db"
    init_db(legacy)
    init_db(primary)
    with get_db(legacy) as conn:
        conn.execute(
            """INSERT INTO trades (city,target_date,bracket_label,bracket_unit,side,
                                   entry_price,size_usd,model_prob,market_prob,edge,
                                   member_count,total_members,confidence,kelly_full,
                                   kelly_frac,created_at)
               VALUES ('london','2026-04-01','25°C','C','YES',.4,10,.5,.4,.1,
                       20,50,.8,.1,.05,'2026-04-01T00:00:00Z')"""
        )
        # A legacy row predating the ledger carries no model attribution.
        conn.execute("UPDATE trades SET model_version_id=NULL")

    first = migrate_legacy_inventory(legacy, primary, apply=True)
    second = migrate_legacy_inventory(legacy, primary, apply=True)
    assert first["copied"]["trades"] == 1
    assert second["copied"]["trades"] == 0
    # Migrated trades must not be left unattributed: init_calibration_ledger's
    # backfill ran long before this copy.
    assert second["untagged_trades"] == 0
    with get_db(primary) as conn:
        rows = conn.execute("SELECT model_version_id FROM trades").fetchall()
        assert [r["model_version_id"] for r in rows] == [LEGACY_MODEL_VERSION]


def test_resolution_recollection_does_not_duplicate(db_path):
    """Gamma's updatedAt mutates; re-collection must not mint a second outcome."""
    target = date(2026, 7, 10)
    identity = dict(
        event_id="event", condition_id="condition", city="london", target_date=target,
        winning_label="25°C", winning_lower=25, winning_upper=26, winning_unit="C",
        resolution_url="fixture://gamma", collected_at=datetime(2026, 7, 11, 1, tzinfo=UTC),
        source={"winner": "25°C"},
    )
    with get_db(db_path) as conn:
        first, created = record_market_resolution(
            conn, declared_station=None, declared_precision=None,
            resolved_at=datetime(2026, 7, 11, tzinfo=UTC), **identity,
        )
        # Re-collected later with a bumped updatedAt and corrected metadata.
        second, created_again = record_market_resolution(
            conn, declared_station="EGLC", declared_precision=1,
            resolved_at=datetime(2026, 7, 12, tzinfo=UTC), **identity,
        )
        assert first == second and created and not created_again
        assert conn.execute("SELECT COUNT(*) FROM market_resolutions").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM market_resolutions WHERE id=?", (first,)).fetchone()
        # Metadata corrections land; the resolution time only moves earlier so
        # the look-ahead filter can never loosen.
        assert row["declared_station"] == "EGLC"
        assert row["declared_precision"] == 1
        assert row["exact_rounded_value"] == 25
        assert row["resolved_at"].startswith("2026-07-11")
