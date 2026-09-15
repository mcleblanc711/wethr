"""Best-first slot filling: strongest signals get limited open-position slots."""
from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import config, main
from src.markets import Bracket, WeatherMarket
from src.paper_trader import get_db, get_pending_trades, init_db
from src.probability import BracketProbability, MarketProbabilities
from src.sizing import size_position

TARGET = date(2026, 9, 20)


class FakeResponse:
    def raise_for_status(self) -> None:
        pass


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, dict]] = []

    async def post(self, url: str, *, content: bytes, headers: dict, timeout: int) -> FakeResponse:
        self.posts.append((url, content.decode("utf-8"), headers))
        return FakeResponse()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "wethr.db"
    init_db(path)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "NTFY_TOPIC_URL", "https://ntfy.test/t")
    monkeypatch.setattr(config, "MAX_PENDING_TRADES", 2)
    monkeypatch.setattr(config, "DAILY_LOSS_LIMIT", 300.0)
    return path


def _bp(label: str, model_prob: float, market_prob: float = 0.20) -> BracketProbability:
    bracket = Bracket(
        token_id=f"t-{label}", label=label, lower=70, upper=72, unit="F",
        market_prob=market_prob, condition_id=f"c-{label}",
    )
    return BracketProbability(
        bracket=bracket, model_prob=model_prob, market_prob=market_prob,
        edge=model_prob - market_prob, member_count=50, total_members=100, confidence=0.7,
    )


def _candidate(city: str, label: str, model_prob: float, market_prob: float = 0.20):
    ps = size_position(_bp(label, model_prob, market_prob), bankroll=10_000.0)
    assert ps.is_valid
    return main.TradeCandidate(
        city=city, target_date=TARGET, market_volume=1_000.0,
        model_version_id="legacy-emos-2026-04-08", ps=ps,
    )


def _place(candidates, pending=0, daily_pnl=0.0):
    client = FakeClient()
    ids = asyncio.run(main.place_ranked_trades(
        client, candidates, 10_000.0, daily_pnl, pending, strategy_version="legacy-v0",
    ))
    return ids, client


def _open_labels(db_path: Path) -> list[str]:
    with get_db(db_path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT bracket_label FROM trades WHERE settled = 0 ORDER BY id"
        )]


FIVE = [
    ("nyc", "weak", 0.30),       # kelly 0.125
    ("chicago", "best", 0.60),   # kelly 0.50
    ("miami", "mid", 0.40),      # kelly 0.25
    ("dallas", "second", 0.50),  # kelly 0.375
    ("denver", "low", 0.35),     # kelly 0.1875
]


@pytest.mark.parametrize("order", [FIVE, FIVE[::-1], FIVE[2:] + FIVE[:2]])
def test_cap_two_records_top_two_by_kelly_regardless_of_order(db_path, order):
    ids, client = _place([_candidate(*item) for item in order])

    assert len(ids) == 2
    assert _open_labels(db_path) == ["best", "second"]
    assert [body.count("Open positions:") for _, body, _ in client.posts] == [1, 1]
    assert "Open positions: 1" in client.posts[0][1]
    assert "Open positions: 2" in client.posts[1][1]


def test_long_shot_with_larger_edge_ranks_below_higher_kelly(db_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_PENDING_TRADES", 1)
    # Long shot: YES edge +0.25 at 0.10 (kelly 0.278); mid-price: YES edge +0.20 at 0.45 (kelly 0.364)
    long_shot = _candidate("nyc", "long-shot", 0.35, 0.10)
    mid = _candidate("chicago", "mid-price", 0.65, 0.45)
    assert abs(long_shot.ps.bracket_prob.edge) > abs(mid.ps.bracket_prob.edge)

    _place([long_shot, mid])

    assert _open_labels(db_path) == ["mid-price"]


def test_ties_break_deterministically(db_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_PENDING_TRADES", 3)
    tied = [
        _candidate("seattle", "z", 0.40),
        _candidate("atlanta", "b", 0.40),
        _candidate("atlanta", "a", 0.40),
    ]
    ranked = sorted(tied, key=main.trade_rank_key)
    assert [(c.city, c.ps.bracket_prob.bracket.label) for c in ranked] == [
        ("atlanta", "a"), ("atlanta", "b"), ("seattle", "z"),
    ]
    _place(tied[::-1])
    assert _open_labels(db_path) == ["a", "b", "z"]


def test_duplicate_open_position_does_not_consume_a_slot(db_path):
    _place([_candidate("chicago", "best", 0.60)], pending=0)
    assert _open_labels(db_path) == ["best"]

    ids, client = _place([_candidate(*item) for item in FIVE], pending=1)

    assert len(ids) == 1
    assert _open_labels(db_path) == ["best", "second"]
    assert "Open positions: 2" in client.posts[0][1]


def test_existing_pending_positions_reduce_available_slots(db_path):
    ids, _ = _place([_candidate(*item) for item in FIVE], pending=1)
    assert len(ids) == 1 and _open_labels(db_path) == ["best"]

    ids, client = _place([_candidate(*item) for item in FIVE], pending=2)
    assert ids == [] and client.posts == []


def test_daily_loss_limit_blocks_all_placements(db_path):
    ids, client = _place([_candidate(*item) for item in FIVE], daily_pnl=-301.0)
    assert ids == [] and client.posts == [] and _open_labels(db_path) == []


def test_live_branch_runs_once_per_placed_trade(db_path, monkeypatch):
    import src.trading as trading

    executed = []

    def fake_execute(client, city, target_date, ps):
        executed.append((city, ps.bracket_prob.bracket.label))
        return SimpleNamespace(success=True, error=None)

    monkeypatch.setattr(trading, "execute_trade", fake_execute)
    asyncio.run(main.place_ranked_trades(
        FakeClient(), [_candidate(*item) for item in FIVE], 10_000.0, 0.0, 0,
        strategy_version="legacy-v0", trading_client=SimpleNamespace(is_live=True),
    ))
    assert executed == [("chicago", "best"), ("dallas", "second")]


def test_scan_and_trade_ranks_across_markets(db_path, monkeypatch):
    """Slots go to the best brackets even when weaker markets are scanned first."""
    markets = [
        WeatherMarket(event_id=f"e-{city}", event_slug=city, city=city, target_date=TARGET,
                      question=city, total_volume=1_000.0)
        for city, _, _ in FIVE
    ]
    probs = {city: (label, p) for city, label, p in FIVE}

    async def discover(client):
        return markets

    async def no_async(*args, **kwargs):
        return {}

    def estimate(market, fc):
        label, p = probs[market.city]
        return MarketProbabilities(market=market, forecast=fc, brackets=[_bp(label, p)])

    fc = SimpleNamespace(mean=70.0, std=2.0)
    monkeypatch.setattr(main, "discover_markets", discover)
    monkeypatch.setattr(main, "fetch_all_ensembles", lambda client, cities: _forecasts(cities, fc))
    monkeypatch.setattr(main, "persist_pipeline_forecasts", lambda forecasts: {})
    monkeypatch.setattr(main, "persist_market_quotes", no_async)
    monkeypatch.setattr(
        main, "selected_model_versions",
        lambda ids: {(m.city, TARGET): "legacy-emos-2026-04-08" for m in markets},
    )
    monkeypatch.setattr(main, "_load_emos_params_safe", lambda: {})
    monkeypatch.setattr(main, "_load_bma_weights_safe", lambda: {})
    monkeypatch.setattr(main, "estimate_bracket_probabilities", estimate)
    monkeypatch.setattr(main, "apply_routed_probabilities", lambda mp, model_id: mp)

    ids = asyncio.run(main.scan_and_trade(FakeClient()))

    assert len(ids) == 2
    assert _open_labels(db_path) == ["best", "second"]
    assert len(get_pending_trades(db_path)) == 2
    with get_db(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == len(FIVE)


async def _forecasts(cities, fc):
    return {city: {TARGET: fc} for city in cities}
