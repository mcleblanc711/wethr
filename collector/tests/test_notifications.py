"""Offline tests for settlement pushes and ntfy mute flags."""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config, main, ntfy, settlement
from src.paper_trader import get_db, init_db


class FakeResponse:
    def raise_for_status(self) -> None:
        pass


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, dict]] = []

    async def post(self, url: str, *, content: bytes, headers: dict, timeout: int) -> FakeResponse:
        self.posts.append((url, content.decode("utf-8"), headers))
        return FakeResponse()


def make_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "wethr.db"
    init_db(db_path)
    return db_path


def insert_open_trade(db_path: Path, trade_id: int, side: str = "YES") -> None:
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trades (
                id, city, target_date, bracket_label, bracket_unit, side,
                entry_price, size_usd, model_prob, market_prob, edge,
                member_count, total_members, confidence, kelly_full, kelly_frac,
                condition_id
            ) VALUES (?, 'nyc', '2026-09-13', '72°F - 74°F', 'F', ?, 0.25, 20.0,
                0.40, 0.25, 0.15, 10, 20, 0.5, 0.1, 0.05, ?)
            """,
            (trade_id, side, f"cond{trade_id}"),
        )


def settled_trade(**overrides) -> dict:
    trade = {
        "id": 729, "city": "nyc", "target_date": "2026-09-13",
        "bracket_label": "72°F - 74°F", "side": "YES", "entry_price": 0.25,
        "size_usd": 20.0, "edge": 0.15, "outcome": True, "pnl": 60.0,
    }
    trade.update(overrides)
    return trade


def test_settled_message_reports_win_loss_and_epoch_total():
    win = ntfy.build_trade_settled_message(
        settled_trade(strategy_version="calib-v1"), 123.45, 7, 3
    )
    loss = ntfy.build_trade_settled_message(
        settled_trade(side="NO", outcome=True, pnl=-20.0)
    )

    assert win.startswith("WIN #729 New York City 2026-09-13")
    assert "72°F - 74°F YES @ 0.25" in win
    assert "Edge at entry: +15.0%" in win
    assert "P/L: $+60.00" in win
    assert "Epoch calib-v1: $+123.45 (7W / 3L)" in win
    assert loss.startswith("LOSS #729")
    assert "Epoch" not in loss


def test_muted_category_skips_the_post(tmp_path: Path):
    db_path = make_db(tmp_path)
    client = FakeClient()

    ntfy.set_muted("settlements", True, db_path)
    muted = asyncio.run(ntfy.send_message(
        client, "x", topic_url="https://ntfy.test/t", category="settlements", db_path=db_path
    ))
    other = asyncio.run(ntfy.send_message(
        client, "y", topic_url="https://ntfy.test/t", category="positions", db_path=db_path
    ))
    ntfy.set_muted("settlements", False, db_path)
    unmuted = asyncio.run(ntfy.send_message(
        client, "z", topic_url="https://ntfy.test/t", category="settlements", db_path=db_path
    ))

    assert (muted, other, unmuted) == (False, True, True)
    assert [body for _, body, _ in client.posts] == ["y", "z"]


def test_unreadable_settings_never_mute(tmp_path: Path):
    assert ntfy.is_muted("settlements", tmp_path / "missing" / "wethr.db") is False


def test_settle_date_lists_each_settled_trade(tmp_path: Path, monkeypatch):
    db_path = make_db(tmp_path)
    insert_open_trade(db_path, 1, side="YES")
    insert_open_trade(db_path, 2, side="NO")
    insert_open_trade(db_path, 3, side="YES")

    async def fake_events(client, target_date):
        return [{"title": "NYC high temperature"}]

    monkeypatch.setattr(settlement, "fetch_resolved_weather_events", fake_events)
    monkeypatch.setattr(settlement, "match_city", lambda title: "nyc")
    monkeypatch.setattr(
        settlement, "extract_outcomes", lambda event: {"cond1": True, "cond2": True}
    )
    monkeypatch.setattr(settlement, "extract_outcomes_by_label", lambda event: {})

    result = asyncio.run(settlement.settle_date(None, date(2026, 9, 13), db_path=db_path))

    by_id = {trade["id"]: trade for trade in result["settled"]}
    assert set(by_id) == {1, 2}
    assert by_id[1]["outcome"] is True and by_id[1]["pnl"] > 0
    assert by_id[2]["outcome"] is True and by_id[2]["pnl"] == -20.0
    assert result["trades_settled"] == 2
    assert abs(result["total_pnl"] - sum(t["pnl"] for t in result["settled"])) < 1e-9


def test_notify_settlements_pushes_one_message_per_trade(tmp_path: Path, monkeypatch):
    db_path = make_db(tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "NTFY_TOPIC_URL", "https://ntfy.test/t")
    client = FakeClient()
    result = {"settled": [settled_trade(id=1), settled_trade(id=2, outcome=False, pnl=-20.0)]}

    asyncio.run(main.notify_settlements(client, result))
    asyncio.run(main.notify_settlements(client, {"settled": []}))

    assert [headers["Title"] for _, _, headers in client.posts] == [
        "Wethr WIN #1", "Wethr LOSS #2",
    ]
