"""Strategy epochs: additive P/L and bankroll splits over one trade ledger."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src import config, main
from src.markets import Bracket
from src.ops import export_settled_trades
from src.paper_trader import (
    LEGACY_STRATEGY_VERSION,
    get_current_epoch,
    get_daily_pnl,
    get_db,
    get_stats,
    init_db,
    list_epochs,
    print_report,
    record_paper_trade,
    reset_bankroll,
    settle_trade,
    start_epoch,
)
from src.probability import BracketProbability
from src.sizing import size_position
from src.telegram_bot import command_response


class FakeResponse:
    def raise_for_status(self) -> None:
        pass


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, dict]] = []

    async def post(self, url: str, *, content: bytes, headers: dict, timeout: int) -> FakeResponse:
        self.posts.append((url, content.decode("utf-8"), headers))
        return FakeResponse()


def _position(label: str = "72°F - 74°F", bankroll: float = 10_000.0):
    bracket = Bracket(
        token_id=f"t-{label}", label=label, lower=72, upper=74, unit="F",
        market_prob=0.20, condition_id=f"c-{label}",
    )
    bp = BracketProbability(
        bracket=bracket, model_prob=0.35, market_prob=0.20,
        edge=0.15, member_count=50, total_members=143, confidence=0.70,
    )
    return size_position(bp, bankroll=bankroll)


def _trade(db_path: Path, label: str, outcome: bool | None = None, **kwargs) -> int:
    trade_id = record_paper_trade("nyc", date(2026, 9, 20), _position(label), 0.0, db_path, **kwargs)
    assert trade_id is not None
    if outcome is not None:
        settle_trade(trade_id, outcome=outcome, db_path=db_path)
    return trade_id


def _set_adjustment(db_path: Path, value: float) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('bankroll_adjustment', ?)", (str(value),)
        )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "wethr.db"
    init_db(path)
    return path


def test_legacy_epoch_is_seeded_once_and_keeps_displayed_bankroll(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INITIAL_BANKROLL", 10_000.0)
    path = tmp_path / "legacy.db"
    init_db(path)
    with get_db(path) as conn:
        conn.execute("DELETE FROM strategy_epochs")  # simulate a pre-epoch database
    _set_adjustment(path, 2695.72)
    loss = _trade(path, "a", outcome=False)
    before = 10_000.0 + 2695.72 + get_stats(path).gross_pnl

    init_db(path)
    init_db(path)  # idempotent

    epochs = list_epochs(path)
    assert [e.label for e in epochs] == [LEGACY_STRATEGY_VERSION]
    assert epochs[0].starting_bankroll == pytest.approx(12_695.72)
    with get_db(path) as conn:
        first_trade = conn.execute("SELECT created_at FROM trades WHERE id=?", (loss,)).fetchone()[0]
    assert epochs[0].started_at == first_trade
    assert get_current_epoch(path).label == LEGACY_STRATEGY_VERSION
    assert get_stats(path).bankroll == pytest.approx(before)


def test_epoch_switch_tags_new_trades_and_leaves_history_untouched(db_path):
    old_win = _trade(db_path, "a", outcome=True)
    old_loss = _trade(db_path, "b", outcome=False)
    with get_db(db_path) as conn:
        before = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id")]

    epoch = start_epoch("calib-v1", 10_000.0, "first calibrated epoch", db_path)
    new_trade = _trade(db_path, "c", outcome=False)
    open_trade = _trade(db_path, "d")

    with get_db(db_path) as conn:
        rows = {r["id"]: r["strategy_version"] for r in conn.execute("SELECT id, strategy_version FROM trades")}
        after = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE id IN (?, ?) ORDER BY id", (old_win, old_loss))]
    assert after == before
    assert rows == {
        old_win: LEGACY_STRATEGY_VERSION, old_loss: LEGACY_STRATEGY_VERSION,
        new_trade: "calib-v1", open_trade: "calib-v1",
    }
    assert get_current_epoch(db_path).label == epoch.label == "calib-v1"
    assert [e.label for e in list_epochs(db_path)] == [LEGACY_STRATEGY_VERSION, "calib-v1"]


def test_per_epoch_stats_and_bankroll(db_path, monkeypatch):
    monkeypatch.setattr(config, "INITIAL_BANKROLL", 10_000.0)
    _trade(db_path, "a", outcome=True)
    _trade(db_path, "b", outcome=False)
    legacy_before = get_stats(db_path, strategy_version=LEGACY_STRATEGY_VERSION)

    start_epoch("calib-v1", 5_000.0, db_path=db_path)
    _trade(db_path, "c", outcome=False)
    _trade(db_path, "d")

    legacy = get_stats(db_path, strategy_version=LEGACY_STRATEGY_VERSION)
    current = get_stats(db_path, strategy_version="calib-v1")
    overall = get_stats(db_path)

    assert (legacy.settled_trades, legacy.wins, legacy.losses) == (2, 1, 1)
    assert legacy.bankroll == pytest.approx(legacy_before.bankroll)
    assert (current.total_trades, current.settled_trades, current.pending_trades) == (2, 1, 1)
    assert current.wins == 0 and current.losses == 1
    assert current.bankroll == pytest.approx(5_000.0 + current.gross_pnl)
    assert overall.total_trades == 4
    assert overall.gross_pnl == pytest.approx(legacy.gross_pnl + current.gross_pnl)
    assert overall.bankroll == pytest.approx(current.bankroll)

    today = datetime.now(timezone.utc).date()
    assert get_daily_pnl(today, db_path, strategy_version="calib-v1") == pytest.approx(current.gross_pnl)
    assert get_daily_pnl(today, db_path) == pytest.approx(overall.gross_pnl)


def test_sizing_uses_epoch_bankroll(db_path, monkeypatch):
    monkeypatch.setattr(config, "INITIAL_BANKROLL", 10_000.0)
    _set_adjustment(db_path, -8_000.0)
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM strategy_epochs")
    init_db(db_path)
    legacy_bankroll = get_stats(db_path).bankroll
    assert legacy_bankroll == pytest.approx(2_000.0)

    start_epoch("calib-v1", 10_000.0, db_path=db_path)
    epoch_bankroll = get_stats(db_path).bankroll
    assert epoch_bankroll == pytest.approx(10_000.0)
    assert (
        _position(bankroll=epoch_bankroll).capped_size_usd
        > _position(bankroll=legacy_bankroll).capped_size_usd
    )


@pytest.mark.parametrize("label", ["", "has space", "../x", "-leading"])
def test_invalid_epoch_labels_are_refused(db_path, label):
    with pytest.raises(ValueError, match="label"):
        start_epoch(label, db_path=db_path)


def test_duplicate_epoch_and_bad_bankroll_are_refused(db_path):
    start_epoch("calib-v1", db_path=db_path)
    with pytest.raises(ValueError, match="already exists"):
        start_epoch("calib-v1", db_path=db_path)
    with pytest.raises(ValueError, match="already exists"):
        start_epoch(LEGACY_STRATEGY_VERSION, db_path=db_path)
    with pytest.raises(ValueError, match="positive"):
        start_epoch("calib-v2", 0, db_path=db_path)
    assert get_current_epoch(db_path).label == "calib-v1"


def test_unknown_current_epoch_setting_falls_back_to_legacy(db_path):
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('current_strategy_epoch', 'missing')"
        )
    assert get_current_epoch(db_path).label == LEGACY_STRATEGY_VERSION
    trade_id = _trade(db_path, "a")
    with get_db(db_path) as conn:
        assert conn.execute(
            "SELECT strategy_version FROM trades WHERE id=?", (trade_id,)
        ).fetchone()[0] == LEGACY_STRATEGY_VERSION


def test_reset_bankroll_only_moves_current_epoch(db_path):
    _trade(db_path, "a", outcome=False)
    legacy = get_stats(db_path, strategy_version=LEGACY_STRATEGY_VERSION).bankroll
    start_epoch("calib-v1", 10_000.0, db_path=db_path)
    _trade(db_path, "b", outcome=False)

    reset_bankroll(7_500.0, db_path)

    assert get_stats(db_path).bankroll == pytest.approx(7_500.0)
    assert get_stats(db_path, strategy_version=LEGACY_STRATEGY_VERSION).bankroll == pytest.approx(legacy)


def test_pnl_command_orders_current_legacy_all_time(db_path):
    _trade(db_path, "a", outcome=False)
    only_legacy = command_response("/pnl", db_path)
    assert only_legacy.startswith(f"Current epoch ({LEGACY_STRATEGY_VERSION})")
    assert "Pre-calibration" not in only_legacy

    start_epoch("calib-v1", 10_000.0, db_path=db_path)
    _trade(db_path, "b", outcome=True)
    text = command_response("/pnl", db_path)

    current = text.index("Current epoch (calib-v1)")
    legacy = text.index(f"Pre-calibration ({LEGACY_STRATEGY_VERSION})")
    all_time = text.index("All-time")
    assert current < legacy < all_time
    assert "Settled: 2 (1W / 1L" in text[all_time:]

    table = command_response("/pnl all", db_path)
    assert table.splitlines()[0] == "Realized P/L by strategy epoch"
    assert f"{LEGACY_STRATEGY_VERSION}: $" in table
    assert "calib-v1 (current): $" in table
    assert command_response("/pnl nope", db_path) == "Usage: /pnl [all]"


def test_settlement_push_reports_each_trades_own_epoch(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "NTFY_TOPIC_URL", "https://ntfy.test/t")
    legacy_trade = _trade(db_path, "a", outcome=False)
    start_epoch("calib-v1", 10_000.0, db_path=db_path)
    new_trade = _trade(db_path, "b", outcome=True)
    with get_db(db_path) as conn:
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM trades")}
    result = {"settled": [
        {**rows[legacy_trade], "outcome": False},
        {**rows[new_trade], "outcome": True},
    ]}
    client = FakeClient()

    asyncio.run(main.notify_settlements(client, result))

    legacy_stats = get_stats(db_path, strategy_version=LEGACY_STRATEGY_VERSION)
    epoch_stats = get_stats(db_path, strategy_version="calib-v1")
    bodies = [body for _, body, _ in client.posts]
    assert f"Epoch {LEGACY_STRATEGY_VERSION}: ${legacy_stats.gross_pnl:+,.2f} (0W / 1L)" in bodies[0]
    assert f"Epoch calib-v1: ${epoch_stats.gross_pnl:+,.2f} (1W / 0L)" in bodies[1]


def test_settled_export_includes_strategy_version(db_path, tmp_path):
    _trade(db_path, "a", outcome=False)
    start_epoch("calib-v1", db_path=db_path)
    _trade(db_path, "b", outcome=True)

    path, count = export_settled_trades(out_path=tmp_path / "out.json", db_path=db_path)

    rows = json.loads(path.read_text())
    assert count == 2
    assert sorted(r["strategy_version"] for r in rows) == ["calib-v1", LEGACY_STRATEGY_VERSION]


def test_report_lists_epochs(db_path):
    _trade(db_path, "a", outcome=False)
    start_epoch("calib-v1", db_path=db_path)
    report = print_report(db_path)
    assert f"  {LEGACY_STRATEGY_VERSION}: $" in report
    assert " *calib-v1: $+0.00 (0W / 0L, 0 open)" in report
