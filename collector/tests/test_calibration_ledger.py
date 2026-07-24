from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.calibration_ops import chronological_rolling_splits, promotion_gates
from src import config
from src.history import fetch_historical_ensemble
from src.ledger import (
    ForecastSnapshotInput, aggregate_daily_observation, exact_value_for_interval,
    lead_bucket, record_forecast_snapshot, record_market_resolution,
    record_market_snapshot, record_station_observation, reconcile_resolution,
    parse_utc, round_to_precision, training_forecast_rows, value_in_interval,
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
        duplicate, created_again = record_forecast_snapshot(conn, ForecastSnapshotInput(
            **{**base, "seen_at": datetime(2026, 7, 20, 2, tzinfo=UTC)}
        ))
        other_run, other_run_created = record_forecast_snapshot(conn, ForecastSnapshotInput(
            **{**base, "run_time": datetime(2026, 7, 20, 1, tzinfo=UTC),
               "seen_at": datetime(2026, 7, 20, 2, tzinfo=UTC)}
        ))
        changed, changed_created = record_forecast_snapshot(conn, ForecastSnapshotInput(
            **{**base, "members_c": [20, 21, 23]}
        ))
        assert created and not created_again and first == duplicate
        assert other_run_created and other_run != first
        assert changed_created and changed not in {first, other_run}
        rows = conn.execute(
            """SELECT content_version,last_seen_at,content_hash,sighting_identity
               FROM forecast_snapshots ORDER BY id"""
        ).fetchall()
        assert [row["content_version"] for row in rows] == [1, 1, 2]
        assert rows[0]["content_hash"] == rows[1]["content_hash"]
        assert rows[0]["sighting_identity"] != rows[1]["sighting_identity"]
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
                conn, city="london", station_id="EGLC", provider="aviationweather-metar",
                observed_at=datetime(2026, 7, 10, hour, tzinfo=UTC),
                received_at=datetime(2026, 7, 10, hour, 30, tzinfo=UTC),
                temperature_c=value, unit_reported="C",
                source_url="fixture://station", raw={"v": value, "h": hour},
            )
        aggregate_daily_observation(
            conn, city="london", target_date=target, provider="aviationweather-metar",
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
        forecast_cols = {row[1] for row in conn.execute("PRAGMA table_info(forecast_snapshots)")}
        assert {"content_hash", "sighting_identity", "lead_basis"} <= forecast_cols
        resolution_cols = {row[1] for row in conn.execute("PRAGMA table_info(market_resolutions)")}
        assert {
            "normalized_station_id", "resolution_provider",
            "reconciled_daily_observation_id",
        } <= resolution_cols
        model_cols = {row[1] for row in conn.execute("PRAGMA table_info(model_versions)")}
        assert "lead_basis" in model_cols
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(model_versions)")}
        assert "idx_model_one_active_route" in indexes


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
            conn, city=city, station_id=station,
            provider=config.CITIES[city].resolution_adapter,
            observed_at=datetime(target.year, target.month, target.day, hour, tzinfo=UTC),
            received_at=datetime(target.year, target.month, target.day, hour, 30, tzinfo=UTC),
            temperature_c=value, unit_reported="C", source_url="fixture://station",
            raw={"v": value, "h": hour},
        )
    aggregate_daily_observation(
        conn, city=city, target_date=target,
        provider=config.CITIES[city].resolution_adapter,
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


def test_unresolved_retry_is_city_scoped_indefinite_and_capped(db_path):
    from src.calibration_ops import (
        collection_status, record_resolution_retry, unresolved_city_dates,
        unresolved_target_dates,
    )
    now = datetime(2026, 7, 20, tzinfo=UTC)
    target = date(2026, 3, 1)
    with get_db(db_path) as conn:
        for city in ("london", "paris"):
            conn.execute(
                """INSERT INTO trades (city,target_date,bracket_label,bracket_unit,
                                       side,entry_price,size_usd,model_prob,
                                       market_prob,edge,member_count,total_members,
                                       confidence,kelly_full,kelly_frac,settled)
                   VALUES (? ,? ,"25C","C","YES",.4,10,.5,.4,.1,20,50,.8,.1,.05,0)""",
                (city, target.isoformat()),
            )
        _seed_station_truth(conn, "london", "EGLC", target)

    items = unresolved_city_dates(db_path, now=now)
    assert [(item["city"], item["target_date"]) for item in items] == [
        ("paris", "2026-03-01")
    ]
    assert unresolved_target_dates(db_path, now=now) == [target]

    with get_db(db_path) as conn:
        for _ in range(5):
            record_resolution_retry(conn, "paris", target, "resolution_missing", now=now)
        retry = conn.execute(
            "SELECT * FROM resolution_retry_state WHERE city=?", ("paris",)
        ).fetchone()
        assert retry["attempt_count"] == 5
        assert parse_utc(retry["next_retry_at"]) == now + timedelta(days=7)

    status = collection_status(db_path, now=now)
    assert status["unresolved_items_older_than_3d"][0]["city"] == "paris"
    assert "abandoned_dates" not in status


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
    assert manifest["schema_version"] == 3
    assert manifest["finalization"]["policy_version"] == "gamma-station-matched-v1"
    assert manifest["finalization"]["accepted_reconciliation_statuses"] == ["matched"]
    assert manifest["finalization"]["reconciliation_provenance"][0][
        "reconciled_daily_observation_id"
    ]

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
    with pytest.raises(RuntimeError, match="matched-only"):
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


# --------------------------------------------------------------------------
# Schema-v3 review blocker regressions
# --------------------------------------------------------------------------

def _seed_daily_observation(conn, city, station, provider, target, peak=25.1):
    for hour in range(23):
        value = peak if hour == 14 else peak - 7
        record_station_observation(
            conn, city=city, station_id=station, provider=provider,
            observed_at=datetime(target.year, target.month, target.day, hour, tzinfo=UTC),
            received_at=datetime(target.year, target.month, target.day, hour, 30, tzinfo=UTC),
            temperature_c=value, unit_reported="C", source_url="fixture://station",
            raw={"provider": provider, "hour": hour},
        )
    return aggregate_daily_observation(
        conn, city=city, target_date=target, provider=provider,
        station_id=station, declared_precision=1, rounded_unit="C",
    )


def _insert_route_model(conn, model_id, basis, bucket, status="candidate"):
    conn.execute(
        """INSERT INTO model_versions(
               id,created_at,algorithm,scope_type,scope_value,lead_basis,lead_bucket,
               parameters_json,training_cutoff,dataset_manifest_json,metrics_json,
               status,shadow_since,schema_version
           ) VALUES(?,"2026-07-01T00:00:00Z","raw-ensemble-fallback","global","all",
                    ?,?,"{}",NULL,"{}","{}",?,"2026-07-01T00:00:00Z",3)""",
        (model_id, basis, bucket, status),
    )


def test_identical_content_distinguishes_cutoffs_buckets_and_bases(db_path):
    target = date(2026, 7, 22)
    common = dict(
        city="london", target_date=target, provider="fixture", model="eps",
        members_c=[20, 21, 22], source_endpoint="fixture://ensemble",
    )
    with get_db(db_path) as conn:
        ids = [record_forecast_snapshot(conn, ForecastSnapshotInput(**item))[0] for item in (
            {**common, "seen_at": datetime(2026, 7, 20, 12, tzinfo=UTC)},
            {**common, "seen_at": datetime(2026, 7, 21, 12, tzinfo=UTC)},
            {**common, "seen_at": datetime(2026, 7, 20, 12, tzinfo=UTC),
             "run_time": datetime(2026, 7, 20, 12, tzinfo=UTC)},
        )]
        rows = conn.execute(
            "SELECT lead_basis,lead_bucket,content_hash,sighting_identity FROM forecast_snapshots"
        ).fetchall()
    assert len(set(ids)) == 3
    assert len({row["content_hash"] for row in rows}) == 1
    assert len({row["sighting_identity"] for row in rows}) == 3
    assert {row["lead_basis"] for row in rows} == {"capture_cutoff", "run_time"}
    assert len({row["lead_bucket"] for row in rows}) >= 2


def test_wrong_station_and_wrong_provider_never_reconcile_or_train(db_path):
    target = date(2026, 7, 10)
    with get_db(db_path) as conn:
        _seed_daily_observation(
            conn, "london", "EGLL", "aviationweather-metar", target
        )
        wrong_station, _ = record_market_resolution(
            conn, event_id="wrong-station", condition_id="c1", city="london",
            target_date=target, winning_label="25C", winning_lower=25,
            winning_upper=26, winning_unit="C", resolution_url="fixture://gamma",
            declared_station="weather station EGLL", declared_precision=1,
            resolved_at=datetime(2026, 7, 11, tzinfo=UTC),
            collected_at=datetime(2026, 7, 11, 1, tzinfo=UTC), source={},
        )
        assert reconcile_resolution(conn, wrong_station) == "station_mismatch"
        assert conn.execute(
            "SELECT reconciled_daily_observation_id FROM market_resolutions WHERE id=?",
            (wrong_station,),
        ).fetchone()[0] is None

        _seed_daily_observation(conn, "london", "EGLC", "nws", target)
        wrong_provider, _ = record_market_resolution(
            conn, event_id="wrong-provider", condition_id="c2", city="london",
            target_date=target, winning_label="25C", winning_lower=25,
            winning_upper=26, winning_unit="C", resolution_url="fixture://gamma",
            declared_station="EGLC", declared_precision=1,
            resolved_at=datetime(2026, 7, 11, tzinfo=UTC),
            collected_at=datetime(2026, 7, 11, 1, tzinfo=UTC), source={},
        )
        assert reconcile_resolution(conn, wrong_provider) == "missing_station_truth"
        record_forecast_snapshot(conn, ForecastSnapshotInput(
            city="london", target_date=target,
            seen_at=datetime(2026, 7, 9, tzinfo=UTC), provider="fixture",
            model="eps", members_c=[24, 25, 26], source_endpoint="fixture://ensemble",
        ))
        assert training_forecast_rows(conn, lead_basis="capture_cutoff") == []


def test_pooled_sighting_combines_provider_model_members(db_path):
    from src.calibration_ops import _pooled_rows

    target = date(2026, 7, 10)
    seen = datetime(2026, 7, 9, tzinfo=UTC)
    with get_db(db_path) as conn:
        _seed_station_truth(conn, "london", "EGLC", target)
        for provider, model, members in (
            ("provider-a", "model-a", [23, 24, 25]),
            ("provider-b", "model-b", [25, 26, 27]),
        ):
            record_forecast_snapshot(conn, ForecastSnapshotInput(
                city="london", target_date=target, seen_at=seen,
                provider=provider, model=model, members_c=members,
                source_endpoint=f"fixture://{provider}",
            ))
        rows = training_forecast_rows(
            conn, lead_bucket_name="0_24h", lead_basis="capture_cutoff"
        )
        pooled = _pooled_rows(rows)
    assert len(rows) == 2 and len(pooled) == 1
    assert pooled[0]["member_count"] == 6
    assert len(pooled[0]["snapshot_ids"]) == 2
    assert pooled[0]["mean_c"] == pytest.approx(25.0)


@pytest.mark.parametrize(("first", "second", "expected"), [
    ("2026-07-11T00:00:00.500000Z", "2026-07-11T00:00:00Z", "2026-07-11T00:00:00Z"),
    ("2026-07-11T01:00:00+01:00", "2026-07-11T00:30:00Z", "2026-07-11T00:00:00Z"),
    ("2026-07-11T00:00:00Z", "2026-07-11T00:00:00.500000Z", "2026-07-11T00:00:00Z"),
])
def test_resolution_time_minimization_uses_datetimes(db_path, first, second, expected):
    identity = dict(
        event_id="event", condition_id="condition", city="london",
        target_date=date(2026, 7, 10), winning_label="25C", winning_lower=25,
        winning_upper=26, winning_unit="C", resolution_url="fixture://gamma",
        declared_station="EGLC", declared_precision=1,
        collected_at=datetime(2026, 7, 12, tzinfo=UTC), source={},
    )
    with get_db(db_path) as conn:
        resolution_id, _ = record_market_resolution(
            conn, resolved_at=parse_utc(first), **identity
        )
        record_market_resolution(conn, resolved_at=parse_utc(second), **identity)
        stored = conn.execute(
            "SELECT resolved_at FROM market_resolutions WHERE id=?", (resolution_id,)
        ).fetchone()[0]
    assert parse_utc(stored) == parse_utc(expected)


def test_archive_refuses_expected_city_date_without_resolution(db_path, tmp_path):
    pytest.importorskip("pyarrow")
    from src.calibration_ops import archive_month

    with get_db(db_path) as conn:
        conn.execute(
            """INSERT INTO trades (city,target_date,bracket_label,bracket_unit,
                                   side,entry_price,size_usd,model_prob,market_prob,
                                   edge,member_count,total_members,confidence,
                                   kelly_full,kelly_frac,settled)
               VALUES ("london","2026-07-10","25C","C","YES",.4,10,.5,.4,
                       .1,20,50,.8,.1,.05,0)"""
        )
    out = tmp_path / "archive"
    with pytest.raises(RuntimeError, match="expected city/dates incomplete"):
        archive_month("2026-07", db_path=db_path, out_dir=out)
    assert not (out / "r1").exists()


def test_train_candidate_has_no_all_route(db_path):
    from src.calibration_ops import train_candidate

    with pytest.raises(ValueError, match="invalid lead bucket"):
        train_candidate(None, db_path=db_path)  # type: ignore[arg-type]
    with get_db(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM model_versions WHERE status=? AND lead_bucket=?",
            ("candidate", "all")
        ).fetchone()[0] == 0


def test_materialization_and_active_selection_are_route_specific(db_path):
    from src.calibration_ops import (
        _materialize_predictions, active_model_for_route, selected_model_versions,
    )
    from src.ensemble import EnsembleForecast, EnsembleMember
    from src.markets import Bracket, WeatherMarket

    target = date(2026, 7, 22)
    seen = datetime(2026, 7, 21, 12, tzinfo=UTC)
    forecast = EnsembleForecast(
        city_slug="london", target_date=target, fetch_time=seen,
        members=[
            EnsembleMember("a", 0, 20, 68), EnsembleMember("a", 1, 21, 69.8),
            EnsembleMember("b", 0, 22, 71.6),
        ],
    )
    market = WeatherMarket(
        event_id="event", event_slug="event", city="london", target_date=target,
        question="temperature", declared_station="EGLC", declared_precision=1,
    )
    bracket = Bracket("yes", "20-22C", 20, 22, "C", .4, "condition", "no")
    market.brackets = [bracket]
    with get_db(db_path) as conn:
        ids = []
        for model, values in (("a", [20, 21]), ("b", [22, 23])):
            snapshot_id, _ = record_forecast_snapshot(conn, ForecastSnapshotInput(
                city="london", target_date=target, seen_at=seen,
                provider="fixture", model=model, members_c=values,
                source_endpoint="fixture://ensemble",
            ))
            ids.append(snapshot_id)
        route = conn.execute(
            "SELECT lead_basis,lead_bucket FROM forecast_snapshots WHERE id=?", (ids[0],)
        ).fetchone()
        _insert_route_model(conn, "candidate-exact", route["lead_basis"], route["lead_bucket"])
        _insert_route_model(conn, "candidate-other", route["lead_basis"], "48_72h")
        market_id, _ = record_market_snapshot(
            conn, captured_at=seen, condition_id="condition", event_id="event",
            city="london", target_date=target.isoformat(), bracket_label=bracket.label,
            bracket_lower=20, bracket_upper=22, bracket_unit="C", yes_best_bid=.39,
            yes_best_ask=.4, no_best_bid=.59, no_best_ask=.6, midpoint=.4,
            last_price=.4, volume=100, liquidity=50, spread=.01,
            resolution_url="fixture://rules", declared_precision=1,
            source_endpoint="fixture://market",
        )
        _materialize_predictions(
            conn, market, bracket, market_id, seen,
            {"london": {target: forecast}}, {("london", target): ids}, .4, .6,
        )
        candidate_ids = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT model_version_id FROM prediction_snapshots WHERE model_version_id LIKE ?",
                ("candidate-%",)
            )
        }
        assert candidate_ids == {"candidate-exact"}
        conn.execute("UPDATE model_versions SET status=? WHERE id=?", ("active", "candidate-exact"))
        conn.execute("UPDATE model_versions SET status=? WHERE id=?", ("active", "candidate-other"))
        assert active_model_for_route(conn, route["lead_basis"], route["lead_bucket"])["id"] == "candidate-exact"
        assert active_model_for_route(conn, "run_time", route["lead_bucket"])["id"] == "legacy-emos-2026-04-08"
    assert selected_model_versions({("london", target): ids}, db_path)[("london", target)] == "candidate-exact"


def test_promotion_recomputes_reconciliation_inside_transaction(db_path, monkeypatch):
    import src.calibration_ops as ops

    target = date(2026, 7, 10)
    with get_db(db_path) as conn:
        _seed_station_truth(conn, "london", "EGLC", target)
        snapshot_id, _ = record_forecast_snapshot(conn, ForecastSnapshotInput(
            city="london", target_date=target, seen_at=datetime(2026, 7, 9, tzinfo=UTC),
            provider="fixture", model="eps", members_c=[24, 25, 26],
            source_endpoint="fixture://ensemble",
        ))
        row = conn.execute(
            "SELECT lead_basis,lead_bucket FROM forecast_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        _insert_route_model(conn, "fresh-evidence", row["lead_basis"], row["lead_bucket"], "shadow")
        conn.execute(
            "UPDATE model_versions SET dataset_manifest_json=? WHERE id=?",
            (json.dumps({"forecast_snapshot_ids": [snapshot_id]}), "fresh-evidence"),
        )
        discrepancy, _ = record_market_resolution(
            conn, event_id="late-discrepancy", condition_id="late", city="london",
            target_date=target, winning_label="30C", winning_lower=30,
            winning_upper=31, winning_unit="C", resolution_url="fixture://gamma",
            declared_station="EGLC", declared_precision=1,
            resolved_at=datetime(2026, 7, 11, tzinfo=UTC),
            collected_at=datetime(2026, 7, 11, 2, tzinfo=UTC), source={},
        )
        assert reconcile_resolution(conn, discrepancy) == "discrepancy"

    monkeypatch.setattr(
        ops, "promotion_gates",
        lambda candidate, control: {
            "reconciliation": candidate["reconciliation_checks_pass"] is True
        },
    )
    with pytest.raises(ValueError, match="reconciliation"):
        ops.promote_model("fresh-evidence", db_path=db_path)
    with get_db(db_path) as conn:
        assert conn.execute(
            "SELECT status FROM model_versions WHERE id=?", ("fresh-evidence",)
        ).fetchone()[0] == "shadow"



def test_promotion_persists_complete_route_transition_report(db_path, monkeypatch):
    import src.calibration_ops as ops

    with get_db(db_path) as conn:
        _insert_route_model(conn, "report-candidate", "capture_cutoff", "24_48h", "shadow")
    fresh_metrics = {"fresh": True, "gamma": {"brier": .1}}
    monkeypatch.setattr(ops, "evaluate_model", lambda *args, **kwargs: fresh_metrics)
    monkeypatch.setattr(ops, "promotion_gates", lambda candidate, control: {"fresh": candidate["fresh"]})

    assert ops.promote_model("report-candidate", db_path=db_path) == {"fresh": True}
    with get_db(db_path) as conn:
        legacy = conn.execute(
            "SELECT status FROM model_versions WHERE id=?", ("legacy-emos-2026-04-08",)
        ).fetchone()[0]
        promoted = conn.execute(
            "SELECT status FROM model_versions WHERE id=?", ("report-candidate",)
        ).fetchone()[0]
        report = json.loads(conn.execute(
            "SELECT gate_report_json FROM model_transitions WHERE to_model_id=?",
            ("report-candidate",),
        ).fetchone()[0])
    assert legacy == "active" and promoted == "active"
    assert report["route"] == {"lead_basis": "capture_cutoff", "lead_bucket": "24_48h"}
    assert report["candidate_id"] == "report-candidate"
    assert report["control_id"] == "legacy-emos-2026-04-08"
    assert report["candidate_metrics"] == fresh_metrics
    assert report["gates"] == {"fresh": True}
    assert parse_utc(report["evaluated_at"])
