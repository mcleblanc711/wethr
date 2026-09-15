from __future__ import annotations

import json
from datetime import date

import pytest

import scripts.monthly_calibration as monthly
import src.calibration_ops as ops
from src.calibration_ops import LEAD_BUCKETS
from src.paper_trader import get_db, init_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ledger.db"
    init_db(path)
    return path


def _insert_candidate(conn, model_id, bucket, algorithm="emos-grouped", shadow_since=None):
    conn.execute(
        """INSERT INTO model_versions(
               id,created_at,algorithm,scope_type,scope_value,lead_basis,lead_bucket,
               parameters_json,training_cutoff,dataset_manifest_json,metrics_json,
               status,shadow_since,schema_version
           ) VALUES(?,"2026-09-01T00:00:00Z",?,"multi-city","eligible","capture_cutoff",?,
                    "{}",NULL,"{}","{}","candidate",?,3)""",
        (model_id, algorithm, bucket, shadow_since),
    )


def _fake_train(db_path, fail=()):
    def train(bucket, db_path=None, lead_basis="capture_cutoff"):
        if bucket in fail:
            raise RuntimeError(f"no data for {bucket}")
        algorithm = "raw-ensemble-fallback" if bucket == "72h_plus" else "emos-grouped"
        with get_db(db_path) as conn:
            _insert_candidate(conn, f"cand-{bucket}", bucket, algorithm)
        return f"cand-{bucket}"
    return train


def test_prior_month_rolls_back_across_year():
    assert monthly.prior_month(date(2026, 1, 1)) == "2025-12"
    assert monthly.prior_month(date(2026, 10, 1)) == "2026-09"


def test_failed_archive_still_trains_and_shadows_every_bucket(db_path, monkeypatch):
    def archive(month, db_path=None, out_dir=None):
        raise RuntimeError(f"{month} is not finalizable under matched-only policy")

    monkeypatch.setattr(monthly, "archive_month", archive)
    monkeypatch.setattr(monthly, "train_candidate", _fake_train(db_path))

    summary = monthly.run(date(2026, 9, 1), db_path=db_path)

    assert summary["archive"] == {"error": "2026-08 is not finalizable under matched-only policy"}
    assert set(summary["candidates"]) == set(LEAD_BUCKETS)
    assert all(entry["shadow"] for entry in summary["candidates"].values())
    assert summary["candidates"]["72h_plus"]["fallback"] is True
    assert summary["candidates"]["24_48h"]["fallback"] is False
    assert summary["errors"] == ["archive 2026-08: 2026-08 is not finalizable under matched-only policy"]
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT status, shadow_since FROM model_versions WHERE id LIKE 'cand-%'"
        ).fetchall()
    assert len(rows) == len(LEAD_BUCKETS)
    assert all(row["status"] == "shadow" and row["shadow_since"] for row in rows)


def test_one_failed_bucket_does_not_skip_the_rest(db_path, monkeypatch):
    monkeypatch.setattr(monthly, "archive_month", lambda month, db_path=None: db_path)
    monkeypatch.setattr(monthly, "train_candidate", _fake_train(db_path, fail={"0_24h"}))

    summary = monthly.run(date(2026, 9, 1), db_path=db_path)

    assert summary["candidates"]["0_24h"] == {"error": "no data for 0_24h"}
    trained = [b for b, entry in summary["candidates"].items() if entry.get("id")]
    assert trained == [b for b in LEAD_BUCKETS if b != "0_24h"]
    assert summary["errors"] == ["train 0_24h: no data for 0_24h"]
    message = monthly.failure_message(summary)
    assert "- train 0_24h: no data for 0_24h" in message
    assert "72h_plus: cand-72h_plus (raw fallback)" in message


def test_main_alerts_once_and_exits_nonzero_on_any_failure(monkeypatch, capsys):
    alerts = []

    async def alert(message):
        alerts.append(message)

    summary = {
        "month": "2026-08", "archive": {"error": "boom"},
        "candidates": {"in_day": {"id": "cand-in_day", "fallback": False}},
        "errors": ["archive 2026-08: boom"],
    }
    monkeypatch.setattr(monthly, "run", lambda: summary)
    monkeypatch.setattr(monthly, "_alert", alert)

    assert monthly.main() == 1
    assert len(alerts) == 1 and "archive 2026-08: boom" in alerts[0]
    assert json.loads(capsys.readouterr().out)["errors"] == ["archive 2026-08: boom"]

    alerts.clear()
    monkeypatch.setattr(monthly, "run", lambda: {**summary, "errors": []})
    assert monthly.main() == 0
    assert alerts == []


def test_evaluate_on_fresh_candidate_starts_shadow_clock(db_path):
    with get_db(db_path) as conn:
        _insert_candidate(conn, "fresh", "24_48h")

    metrics = ops.evaluate_model("fresh", db_path)

    assert metrics["resolved_dates"] == 0
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT status, shadow_since FROM model_versions WHERE id='fresh'"
        ).fetchone()
    assert row["status"] == "shadow" and row["shadow_since"]


def _failing_gates(candidate, control):
    return {"shadow_duration": False, "brier_vs_raw": True, "archive": False}


def test_paper_override_promotes_and_records_failed_gates(db_path, monkeypatch):
    with get_db(db_path) as conn:
        _insert_candidate(conn, "override", "24_48h", shadow_since="2026-09-01T00:00:00Z")
    monkeypatch.delenv("WETHR_LIVE", raising=False)
    monkeypatch.setattr(ops, "evaluate_model", lambda *args, **kwargs: {"gamma": {}})
    monkeypatch.setattr(ops, "promotion_gates", _failing_gates)

    gates = ops.promote_model(
        "override", db_path=db_path, paper_override_reason="paper epoch calib-v1"
    )

    assert gates == _failing_gates(None, None)
    with get_db(db_path) as conn:
        status = conn.execute("SELECT status FROM model_versions WHERE id='override'").fetchone()[0]
        report = json.loads(conn.execute(
            "SELECT gate_report_json FROM model_transitions WHERE to_model_id='override'"
        ).fetchone()[0])
    assert status == "active"
    assert report["gates"] == gates
    assert report["paper_override"] == {
        "applied": True,
        "reason": "paper epoch calib-v1",
        "failed_gates": ["shadow_duration", "archive"],
    }


def test_failing_promotion_without_override_persists_nothing(db_path, monkeypatch):
    with get_db(db_path) as conn:
        _insert_candidate(conn, "blocked", "24_48h", shadow_since="2026-09-01T00:00:00Z")
    monkeypatch.setattr(ops, "evaluate_model", lambda *args, **kwargs: {"gamma": {}})
    monkeypatch.setattr(ops, "promotion_gates", _failing_gates)

    with pytest.raises(ValueError, match="shadow_duration, archive"):
        ops.promote_model("blocked", db_path=db_path)
    with get_db(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_transitions").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM model_versions WHERE id='blocked'"
        ).fetchone()[0] == "candidate"


@pytest.mark.parametrize("reason", ["", "   "])
def test_paper_override_requires_reason(db_path, reason):
    with pytest.raises(ValueError, match="non-empty reason"):
        ops.promote_model("anything", db_path=db_path, paper_override_reason=reason)


def test_paper_override_refused_when_live(db_path, monkeypatch):
    with get_db(db_path) as conn:
        _insert_candidate(conn, "live", "24_48h")
    monkeypatch.setenv("WETHR_LIVE", "1")

    with pytest.raises(ValueError, match="WETHR_LIVE=1"):
        ops.promote_model("live", db_path=db_path, paper_override_reason="paper")
    with get_db(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_transitions").fetchone()[0] == 0
