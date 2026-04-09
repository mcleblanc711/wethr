"""
Tests for Wethr core logic.

Run: python -m pytest tests/ -v
Or:  python tests/test_core.py

All tests are offline — no network calls. We mock API responses
where needed and test the math/parsing directly.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path
from types import ModuleType

import numpy as np

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- Mock httpx for offline testing ----
# The src modules import httpx at the top level for type annotations
# and async HTTP. We only test pure logic here, so a stub suffices.
if "httpx" not in sys.modules:
    _httpx = ModuleType("httpx")

    class _HTTPError(Exception):
        pass

    class _AsyncClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kw):
            raise _HTTPError(f"Stub — no network")

    _httpx.HTTPError = _HTTPError
    _httpx.AsyncClient = _AsyncClient
    sys.modules["httpx"] = _httpx
# ---- End mock ----

from src.markets import parse_bracket_label, parse_target_date, match_city
from src.probability import count_members_in_bracket, BracketProbability
from src.sizing import calculate_kelly, size_position
from src.ensemble import c_to_f, EnsembleMember, EnsembleForecast
from src.paper_trader import init_db, record_paper_trade, settle_trade, get_stats, get_calibration_data


# ===================================================================
# Bracket label parsing
# ===================================================================

def test_parse_range_bracket():
    """Standard US range bracket: '72°F - 74°F'"""
    lo, hi, unit = parse_bracket_label("72°F - 74°F")
    assert lo == 72.0
    assert hi == 74.0
    assert unit == "F"


def test_parse_range_celsius():
    """International range bracket: '22°C - 23°C'"""
    lo, hi, unit = parse_bracket_label("22°C - 23°C")
    assert lo == 22.0
    assert hi == 23.0
    assert unit == "C"


def test_parse_range_with_to():
    """Range with 'to' instead of dash: '72°F to 74°F'"""
    lo, hi, unit = parse_bracket_label("72°F to 74°F")
    assert lo == 72.0
    assert hi == 74.0
    assert unit == "F"


def test_parse_below_bracket():
    """Lower tail: 'Below 68°F'"""
    lo, hi, unit = parse_bracket_label("Below 68°F")
    assert lo is None
    assert hi == 68.0
    assert unit == "F"


def test_parse_above_bracket():
    """Upper tail: '76°F or above'"""
    lo, hi, unit = parse_bracket_label("76°F or above")
    assert lo == 76.0
    assert hi is None
    assert unit == "F"


def test_parse_above_plus():
    """Upper tail with plus sign: '76°F+'"""
    lo, hi, unit = parse_bracket_label("76°F+")
    assert lo == 76.0
    assert hi is None
    assert unit == "F"


def test_parse_below_celsius():
    """Lower tail Celsius: 'Below 20°C'"""
    lo, hi, unit = parse_bracket_label("Below 20°C")
    assert lo is None
    assert hi == 20.0
    assert unit == "C"


def test_parse_above_or_more():
    """Upper tail: '24°C or more'"""
    lo, hi, unit = parse_bracket_label("24°C or more")
    assert lo == 24.0
    assert hi is None
    assert unit == "C"


def test_parse_unparseable():
    """Graceful failure on weird labels"""
    lo, hi, unit = parse_bracket_label("Partly cloudy")
    assert lo is None and hi is None and unit == ""


# ===================================================================
# Date parsing
# ===================================================================

def test_parse_date_standard():
    d = parse_target_date("Highest temperature in NYC on March 22, 2026?")
    assert d == date(2026, 3, 22)


def test_parse_date_no_year():
    d = parse_target_date("Highest temperature in NYC on March 22?")
    assert d is not None
    assert d.month == 3 and d.day == 22


def test_parse_date_iso():
    d = parse_target_date("Weather market 2026-03-22")
    assert d == date(2026, 3, 22)


# ===================================================================
# City matching
# ===================================================================

def test_match_nyc():
    assert match_city("Highest temperature in NYC on March 18?") == "nyc"


def test_match_nyc_full_name():
    assert match_city("Highest temperature in New York City on March 22?") == "nyc"


def test_match_london():
    assert match_city("Highest temperature in London on March 22?") == "london"


def test_match_sao_paulo():
    assert match_city("Highest temperature in Sao Paulo on March 19?") == "sao_paulo"


def test_match_buenos_aires():
    assert match_city("Highest temperature in Buenos Aires on March 19?") == "buenos_aires"


def test_match_tel_aviv():
    assert match_city("Highest temperature in Tel Aviv on March 16?") == "tel_aviv"


def test_match_no_city():
    assert match_city("What's the weather today?") is None


# --- Polymarket-actual bracket formats ---

def test_parse_or_below():
    """Polymarket format: '31°F or below'"""
    lo, hi, unit = parse_bracket_label("31°F or below")
    assert lo is None
    assert hi == 31.0
    assert unit == "F"


def test_parse_or_below_negative():
    """Polymarket format: '-6°C or below'"""
    lo, hi, unit = parse_bracket_label("-6°C or below")
    assert lo is None
    assert hi == -6.0
    assert unit == "C"


def test_parse_or_higher():
    """Polymarket format: '23°C or higher'"""
    lo, hi, unit = parse_bracket_label("23°C or higher")
    assert lo == 23.0
    assert hi is None
    assert unit == "C"


def test_parse_compact_range():
    """Polymarket format: '44-45°F' (2°F bin)"""
    lo, hi, unit = parse_bracket_label("44-45°F")
    assert lo == 44.0
    assert hi == 46.0  # 45 + 1 = 46
    assert unit == "F"


def test_parse_compact_range_high():
    """Polymarket format: '96-97°F'"""
    lo, hi, unit = parse_bracket_label("96-97°F")
    assert lo == 96.0
    assert hi == 98.0
    assert unit == "F"


def test_parse_single_celsius():
    """Polymarket format: '13°C' (1°C bin)"""
    lo, hi, unit = parse_bracket_label("13°C")
    assert lo == 13.0
    assert hi == 14.0  # 13 + 1 = 14
    assert unit == "C"


def test_parse_single_negative():
    """Polymarket format: '-1°C'"""
    lo, hi, unit = parse_bracket_label("-1°C")
    assert lo == -1.0
    assert hi == 0.0
    assert unit == "C"


# ===================================================================
# Member counting
# ===================================================================

def test_count_interior_bracket():
    temps = np.array([70, 71, 72, 73, 74, 75, 76, 77, 78])
    count = count_members_in_bracket(temps, 72.0, 76.0)
    # [72, 73, 74, 75] → 4 members
    assert count == 4


def test_count_below_bracket():
    temps = np.array([65, 66, 67, 68, 69, 70, 71, 72])
    count = count_members_in_bracket(temps, None, 68.0)
    # [65, 66, 67] → 3 members (< 68)
    assert count == 3


def test_count_above_bracket():
    temps = np.array([70, 72, 74, 76, 78, 80])
    count = count_members_in_bracket(temps, 76.0, None)
    # [76, 78, 80] → 3 members (>= 76)
    assert count == 3


def test_count_exhaustive():
    """All members must fall into exactly one bracket."""
    temps = np.array([65, 68, 70, 72, 74, 76, 80])
    below_68 = count_members_in_bracket(temps, None, 68.0)
    b68_72 = count_members_in_bracket(temps, 68.0, 72.0)
    b72_76 = count_members_in_bracket(temps, 72.0, 76.0)
    above_76 = count_members_in_bracket(temps, 76.0, None)
    assert below_68 + b68_72 + b72_76 + above_76 == len(temps)


def test_count_boundary_values():
    """Boundary: lower-inclusive, upper-exclusive."""
    temps = np.array([72.0, 74.0])
    # 72 is IN [72, 74), 74 is NOT in [72, 74)
    assert count_members_in_bracket(temps, 72.0, 74.0) == 1
    # 74 IS in [74, +inf)
    assert count_members_in_bracket(temps, 74.0, None) == 1


# ===================================================================
# Kelly criterion
# ===================================================================

def test_kelly_positive_edge():
    """Model says 60%, market says 45% → positive Kelly for YES."""
    kelly, side = calculate_kelly(0.60, 0.45)
    assert side == "YES"
    assert kelly > 0
    # Kelly = (0.60 - 0.45) / (1 - 0.45) = 0.15 / 0.55 ≈ 0.2727
    assert abs(kelly - 0.2727) < 0.01


def test_kelly_negative_edge():
    """Model says 30%, market says 45% → buy NO."""
    kelly, side = calculate_kelly(0.30, 0.45)
    assert side == "NO"
    assert kelly > 0
    # Buying NO at 0.55, model prob of NO = 0.70
    # Kelly = (0.70 - 0.55) / (1 - 0.55) = 0.15 / 0.45 ≈ 0.3333
    assert abs(kelly - 0.3333) < 0.01


def test_kelly_no_edge():
    """Model agrees with market → zero Kelly."""
    kelly, _ = calculate_kelly(0.50, 0.50)
    assert abs(kelly) < 0.001


def test_kelly_extreme_edge():
    """Model says 95%, market says 30% → large Kelly."""
    kelly, side = calculate_kelly(0.95, 0.30)
    assert side == "YES"
    # (0.95 - 0.30) / (1 - 0.30) = 0.65 / 0.70 ≈ 0.929
    assert kelly > 0.9


# ===================================================================
# Position sizing with caps
# ===================================================================

def test_size_capped_by_bankroll_pct():
    """Position can't exceed 5% of bankroll."""
    from src.markets import Bracket
    bracket = Bracket(
        token_id="t1", label="72°F - 74°F",
        lower=72.0, upper=74.0, unit="F",
        market_prob=0.10, condition_id="c1",
    )
    bp = BracketProbability(
        bracket=bracket,
        model_prob=0.50,  # Huge edge
        market_prob=0.10,
        edge=0.40,
        member_count=70,
        total_members=143,
        confidence=0.70,
    )
    ps = size_position(bp, bankroll=10000.0)
    # 5% of $10K = $500, but hard cap is $100
    assert ps.capped_size_usd <= 100.0
    assert ps.is_valid


def test_size_skipped_low_edge():
    """Below threshold → skipped."""
    from src.markets import Bracket
    bracket = Bracket(
        token_id="t1", label="72°F - 74°F",
        lower=72.0, upper=74.0, unit="F",
        market_prob=0.30, condition_id="c1",
    )
    bp = BracketProbability(
        bracket=bracket,
        model_prob=0.33,  # Edge = 3%, below 8% threshold
        market_prob=0.30,
        edge=0.03,
        member_count=47,
        total_members=143,
        confidence=0.67,
    )
    ps = size_position(bp, bankroll=10000.0)
    assert not ps.is_valid
    assert "Edge" in ps.reason_skipped or "edge" in ps.reason_skipped.lower()


# ===================================================================
# Temperature conversion
# ===================================================================

def test_c_to_f():
    assert c_to_f(0) == 32.0
    assert c_to_f(100) == 212.0
    assert abs(c_to_f(22.2) - 72.0) < 0.1


# ===================================================================
# EnsembleForecast properties
# ===================================================================

def test_ensemble_forecast_stats():
    members = [
        EnsembleMember("gfs", i, 20.0 + i * 0.5, c_to_f(20.0 + i * 0.5))
        for i in range(31)
    ]
    ef = EnsembleForecast(city_slug="nyc", target_date=date(2026, 3, 22), members=members)

    assert ef.n_members == 31
    assert ef.mean > 0
    assert ef.std > 0
    assert ef.spread > 0
    # Mean should be around 20 + 15*0.5 = 27.5°C
    assert abs(ef.mean - 27.5) < 0.1


# ===================================================================
# Settlement logic
# ===================================================================

# ===================================================================
# Paper trading database round-trip
# ===================================================================

def test_paper_trade_roundtrip():
    """Record a trade, settle it, verify stats."""
    from src.markets import Bracket

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_db(db_path)

        bracket = Bracket(
            token_id="t1", label="72°F - 74°F",
            lower=72.0, upper=74.0, unit="F",
            market_prob=0.20, condition_id="c1",
        )
        bp = BracketProbability(
            bracket=bracket, model_prob=0.35, market_prob=0.20,
            edge=0.15, member_count=50, total_members=143, confidence=0.65,
        )
        ps = size_position(bp, bankroll=10000.0)
        assert ps.is_valid

        tid = record_paper_trade("nyc", date(2026, 3, 22), ps, db_path)
        assert tid == 1

        # Settle as a win (YES resolves true)
        pnl = settle_trade(tid, outcome=True, db_path=db_path)
        assert pnl > 0  # We bought YES at 0.20, it resolved to 1.0

        stats = get_stats(db_path)
        assert stats.total_trades == 1
        assert stats.settled_trades == 1
        assert stats.wins == 1
        assert stats.gross_pnl > 0


def test_paper_trade_loss():
    """Verify loss calculation."""
    from src.markets import Bracket

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        init_db(db_path)

        bracket = Bracket(
            token_id="t2", label="76°F or above",
            lower=76.0, upper=None, unit="F",
            market_prob=0.15, condition_id="c2",
        )
        bp = BracketProbability(
            bracket=bracket, model_prob=0.30, market_prob=0.15,
            edge=0.15, member_count=43, total_members=143, confidence=0.70,
        )
        ps = size_position(bp, bankroll=10000.0)
        tid = record_paper_trade("nyc", date(2026, 3, 22), ps, db_path)

        # Settle as a loss
        pnl = settle_trade(tid, outcome=False, db_path=db_path)
        assert pnl < 0
        assert pnl == -ps.capped_size_usd  # Lost full position


# ===================================================================
# Probability sanity — ensemble counting sums to 1.0
# ===================================================================

def test_probability_exhaustive():
    """Bracket probs from ensemble counting should sum to 1.0."""
    # Simulate 143 members with temps between 65-85°F
    np.random.seed(42)
    temps = np.random.normal(75, 3, size=143)

    brackets = [
        (None, 68.0),    # Below 68
        (68.0, 70.0),
        (70.0, 72.0),
        (72.0, 74.0),
        (74.0, 76.0),
        (76.0, 78.0),
        (78.0, 80.0),
        (80.0, None),    # 80 or above
    ]

    total = sum(
        count_members_in_bracket(temps, lo, hi) for lo, hi in brackets
    )
    assert total == 143, f"Expected 143, got {total}"

    prob_sum = total / 143
    assert abs(prob_sum - 1.0) < 0.001


# ===================================================================
# EMOS / NGR Calibration
# ===================================================================

def test_crps_gaussian_perfect():
    """CRPS = 0 when prediction is a point mass at the observation."""
    from src.calibration import crps_gaussian
    # Very small sigma centered on obs → CRPS ≈ 0
    crps = crps_gaussian(mu=75.0, sigma=0.001, obs=75.0)
    assert crps < 0.01


def test_crps_gaussian_increases_with_error():
    """CRPS should increase as prediction moves away from observation."""
    from src.calibration import crps_gaussian
    crps_close = crps_gaussian(mu=75.0, sigma=2.0, obs=75.0)
    crps_far = crps_gaussian(mu=75.0, sigma=2.0, obs=80.0)
    assert crps_far > crps_close


def test_crps_gaussian_increases_with_spread():
    """Wider distribution = higher CRPS (all else equal), for centered case."""
    from src.calibration import crps_gaussian
    crps_narrow = crps_gaussian(mu=75.0, sigma=1.0, obs=75.0)
    crps_wide = crps_gaussian(mu=75.0, sigma=5.0, obs=75.0)
    assert crps_wide > crps_narrow


def test_emos_train_identity():
    """With perfect ensemble (mean = obs), EMOS should learn near-identity."""
    from src.calibration import TrainingData, train_emos
    np.random.seed(42)
    n = 100
    obs = np.random.normal(75, 5, size=n)
    # Perfect mean, realistic std
    means = obs + np.random.normal(0, 0.5, size=n)  # Small noise
    stds = np.full(n, 2.5)

    data = TrainingData(ens_means=means, ens_stds=stds, observations=obs)
    params = train_emos(data, city="test")

    # b should be close to 1.0 (ensemble mean tracks obs well)
    assert 0.8 < params.b < 1.2, f"b={params.b}, expected ~1.0"
    # a should be close to 0 (no systematic bias)
    assert abs(params.a) < 3.0, f"a={params.a}, expected ~0"


def test_emos_corrects_bias():
    """EMOS should learn to correct a systematic +3°F bias."""
    from src.calibration import TrainingData, train_emos
    np.random.seed(42)
    n = 100
    obs = np.random.normal(75, 5, size=n)
    # Biased ensemble: consistently 3°F too high
    means = obs + 3.0 + np.random.normal(0, 0.3, size=n)
    stds = np.full(n, 2.5)

    data = TrainingData(ens_means=means, ens_stds=stds, observations=obs)
    params = train_emos(data, city="test_bias")

    # a should be negative (correcting the +3 bias)
    # With b≈1, we'd expect a ≈ -3
    corrected_mean = params.a + params.b * 78.0  # Example input
    expected = 78.0 - 3.0  # Should target ~75
    assert abs(corrected_mean - expected) < 2.0, (
        f"Corrected {78.0} → {corrected_mean:.1f}, expected ~{expected}"
    )


def test_calibrated_bracket_probability_sums_to_one():
    """CDF-based bracket probs should sum to 1.0."""
    from src.calibration import calibrated_bracket_probability
    mu, sigma = 75.0, 3.0
    brackets = [
        (None, 68.0),
        (68.0, 70.0),
        (70.0, 72.0),
        (72.0, 74.0),
        (74.0, 76.0),
        (76.0, 78.0),
        (78.0, 80.0),
        (80.0, None),
    ]
    total = sum(
        calibrated_bracket_probability(lo, hi, mu, sigma)
        for lo, hi in brackets
    )
    assert abs(total - 1.0) < 0.001, f"Sum = {total}, expected 1.0"


def test_calibrated_vs_raw_tails():
    """EMOS with wider sigma should give more probability to tails than raw counting."""
    from src.calibration import calibrated_bracket_probability
    # Raw ensemble: tight distribution centered at 75°F
    # EMOS spreads it out (larger sigma)
    # Tail bracket [80, +inf) should get more probability from EMOS

    raw_sigma = 2.0
    emos_sigma = 4.0  # EMOS learned the ensemble is under-dispersed
    mu = 75.0

    raw_tail = calibrated_bracket_probability(80.0, None, mu, raw_sigma)
    emos_tail = calibrated_bracket_probability(80.0, None, mu, emos_sigma)

    assert emos_tail > raw_tail, (
        f"EMOS tail ({emos_tail:.3f}) should exceed raw tail ({raw_tail:.3f})"
    )


# ===================================================================
# Trading client (dry run)
# ===================================================================

def test_trading_client_dry_run():
    """Dry-run trading should succeed without API credentials."""
    from src.trading import TradingClient, OrderResult
    client = TradingClient(live=False)
    client.initialize()

    assert not client.is_live

    result = client.place_order(
        token_id="fake-token-id-12345",
        side="YES",
        price=0.30,
        size_usd=50.0,
    )
    assert result.success
    assert result.dry_run
    assert result.price == 0.30
    assert result.size == 50.0


def test_trading_client_rejects_bad_price():
    """Orders with price <= 0 or >= 1 should be rejected."""
    from src.trading import TradingClient
    client = TradingClient(live=False)
    client.initialize()

    result = client.place_order("token", "YES", price=0.0, size_usd=50.0)
    assert not result.success

    result = client.place_order("token", "YES", price=1.0, size_usd=50.0)
    assert not result.success


# ===================================================================
# Latency detection
# ===================================================================

def test_latency_detector_no_previous():
    """First observation should return None (no comparison possible)."""
    from src.latency import LatencyDetector
    detector = LatencyDetector()

    members = [
        EnsembleMember("gfs", i, 22.0 + i * 0.1, c_to_f(22.0 + i * 0.1))
        for i in range(31)
    ]
    fc = EnsembleForecast(city_slug="nyc", target_date=date(2026, 3, 25), members=members)

    shift = detector.check_shift(fc)
    assert shift is None
    assert detector.tracked_count == 1


def test_latency_detector_detects_shift():
    """Significant mean shift should be flagged."""
    from src.latency import LatencyDetector
    detector = LatencyDetector()

    # First observation: mean ~23°C
    members1 = [
        EnsembleMember("gfs", i, 23.0 + i * 0.05, c_to_f(23.0 + i * 0.05))
        for i in range(31)
    ]
    fc1 = EnsembleForecast(city_slug="nyc", target_date=date(2026, 3, 25), members=members1)
    detector.check_shift(fc1)

    # Second observation: mean ~25°C (+2°C shift — significant)
    members2 = [
        EnsembleMember("gfs", i, 25.0 + i * 0.05, c_to_f(25.0 + i * 0.05))
        for i in range(31)
    ]
    fc2 = EnsembleForecast(city_slug="nyc", target_date=date(2026, 3, 25), members=members2)
    shift = detector.check_shift(fc2)

    assert shift is not None
    assert shift.is_significant
    assert abs(shift.mean_shift - 2.0) < 0.1
    assert shift.severity in ("MODERATE", "MAJOR")


def test_latency_detector_ignores_small_shift():
    """Sub-threshold shift should not be significant."""
    from src.latency import LatencyDetector
    detector = LatencyDetector()

    members1 = [
        EnsembleMember("gfs", i, 23.0 + i * 0.05, c_to_f(23.0 + i * 0.05))
        for i in range(31)
    ]
    fc1 = EnsembleForecast(city_slug="nyc", target_date=date(2026, 3, 25), members=members1)
    detector.check_shift(fc1)

    # +0.2°C shift — below threshold
    members2 = [
        EnsembleMember("gfs", i, 23.2 + i * 0.05, c_to_f(23.2 + i * 0.05))
        for i in range(31)
    ]
    fc2 = EnsembleForecast(city_slug="nyc", target_date=date(2026, 3, 25), members=members2)
    shift = detector.check_shift(fc2)

    assert shift is not None
    assert not shift.is_significant


# ===================================================================
# NO-side bracket token extraction
# ===================================================================

def test_bracket_has_no_token_field():
    """Bracket dataclass should have no_token_id field."""
    from src.markets import Bracket
    b = Bracket(
        token_id="yes123", label="72°F - 74°F",
        lower=72, upper=74, unit="F", market_prob=0.3,
        condition_id="c1", no_token_id="no456",
    )
    assert b.no_token_id == "no456"
    assert b.token_id == "yes123"


# ===================================================================
# BMA weighting
# ===================================================================

def test_bma_equal_weights():
    """With equal CRPS, all models should get equal weight."""
    from src.bma import BMAWeights
    weights = BMAWeights(
        weights={
            "ecmwf_ifs025_ensemble": 0.25,
            "gfs025_eps": 0.25,
            "icon_seamless_eps": 0.25,
            "gem_global_ensemble": 0.25,
        },
        city="nyc",
    )
    assert abs(sum(weights.weights.values()) - 1.0) < 0.01
    assert weights.get("ecmwf_ifs025_ensemble") == 0.25


def test_bma_weighted_bracket():
    """BMA weighting should produce valid probabilities."""
    from src.bma import weighted_bracket_probability, BMAWeights

    # Create a forecast with members from two models
    members = (
        [EnsembleMember("ecmwf_ifs025_ensemble", i, 22.0 + i * 0.1, c_to_f(22.0 + i * 0.1))
         for i in range(20)] +
        [EnsembleMember("gfs025_eps", i, 24.0 + i * 0.1, c_to_f(24.0 + i * 0.1))
         for i in range(15)]
    )
    fc = EnsembleForecast(city_slug="nyc", target_date=date(2026, 3, 25), members=members)

    # ECMWF gets 70% weight (better model), GFS gets 30%
    bma = BMAWeights(
        weights={
            "ecmwf_ifs025_ensemble": 0.70,
            "gfs025_eps": 0.30,
        },
        city="nyc",
    )

    # Bracket spanning ~72-76°F
    prob = weighted_bracket_probability(fc, 72.0, 76.0, "F", bma)
    assert 0.0 <= prob <= 1.0


def test_bma_brackets_sum_approximately_one():
    """BMA bracket probabilities should sum to ~1.0."""
    from src.bma import weighted_bracket_probability, BMAWeights

    members = (
        [EnsembleMember("ecmwf_ifs025_ensemble", i, 22.0 + i * 0.2, c_to_f(22.0 + i * 0.2))
         for i in range(30)] +
        [EnsembleMember("gfs025_eps", i, 23.0 + i * 0.15, c_to_f(23.0 + i * 0.15))
         for i in range(20)]
    )
    fc = EnsembleForecast(city_slug="nyc", target_date=date(2026, 3, 25), members=members)

    bma = BMAWeights(
        weights={"ecmwf_ifs025_ensemble": 0.60, "gfs025_eps": 0.40},
        city="nyc",
    )

    brackets = [
        (None, 68.0),
        (68.0, 72.0),
        (72.0, 76.0),
        (76.0, 80.0),
        (80.0, None),
    ]
    total = sum(
        weighted_bracket_probability(fc, lo, hi, "F", bma)
        for lo, hi in brackets
    )
    assert abs(total - 1.0) < 0.05, f"BMA probs sum to {total}, expected ~1.0"


def test_bma_favors_better_model():
    """BMA with higher weight on ECMWF should shift distribution toward ECMWF mean."""
    from src.bma import weighted_bracket_probability, BMAWeights

    # ECMWF predicts cooler (mean ~20°C = 68°F)
    # GFS predicts warmer (mean ~26°C = 79°F)
    members = (
        [EnsembleMember("ecmwf_ifs025_ensemble", i, 20.0, c_to_f(20.0))
         for i in range(20)] +
        [EnsembleMember("gfs025_eps", i, 26.0, c_to_f(26.0))
         for i in range(20)]
    )
    fc = EnsembleForecast(city_slug="nyc", target_date=date(2026, 3, 25), members=members)

    # Heavy weight on ECMWF (cooler)
    bma_ecmwf = BMAWeights(
        weights={"ecmwf_ifs025_ensemble": 0.90, "gfs025_eps": 0.10},
        city="nyc",
    )

    # Heavy weight on GFS (warmer)
    bma_gfs = BMAWeights(
        weights={"ecmwf_ifs025_ensemble": 0.10, "gfs025_eps": 0.90},
        city="nyc",
    )

    # Check "Below 72°F" bracket — should be higher when ECMWF (cooler) is favored
    cool_bracket_ecmwf = weighted_bracket_probability(fc, None, 72.0, "F", bma_ecmwf)
    cool_bracket_gfs = weighted_bracket_probability(fc, None, 72.0, "F", bma_gfs)

    assert cool_bracket_ecmwf > cool_bracket_gfs, (
        f"ECMWF-favored ({cool_bracket_ecmwf:.3f}) should give more weight to cool bracket "
        f"than GFS-favored ({cool_bracket_gfs:.3f})"
    )


# ===================================================================
# Signal dedup
# ===================================================================

def test_signal_dedup():
    """Repeated signal recording should update, not duplicate."""
    from src.markets import Bracket
    from src.probability import BracketProbability
    from src.paper_trader import init_db, record_signal, get_db

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dedup.db"
        init_db(db_path)

        bracket = Bracket(
            token_id="t1", label="72°F - 74°F",
            lower=72, upper=74, unit="F", market_prob=0.20, condition_id="c1",
        )
        bp = BracketProbability(
            bracket=bracket, model_prob=0.30, market_prob=0.20,
            edge=0.10, member_count=43, total_members=143, confidence=0.70,
        )

        # Record same signal twice with different model_prob
        record_signal("nyc", date(2026, 3, 22), bp, db_path)
        bp2 = BracketProbability(
            bracket=bracket, model_prob=0.35, market_prob=0.20,
            edge=0.15, member_count=50, total_members=143, confidence=0.65,
        )
        record_signal("nyc", date(2026, 3, 22), bp2, db_path)

        # Should have exactly one row, updated to latest model_prob
        with get_db(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE city='nyc' AND bracket_label='72°F - 74°F'"
            ).fetchall()
        assert len(rows) == 1, f"Expected 1 signal row, got {len(rows)}"
        assert abs(rows[0]["model_prob"] - 0.35) < 0.001


def test_trade_dedup():
    """Duplicate trade for same bracket should return None."""
    from src.markets import Bracket
    from src.probability import BracketProbability
    from src.paper_trader import init_db, record_paper_trade

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "dedup.db"
        init_db(db_path)

        bracket = Bracket(
            token_id="t1", label="72°F - 74°F",
            lower=72, upper=74, unit="F", market_prob=0.20, condition_id="c1",
        )
        bp = BracketProbability(
            bracket=bracket, model_prob=0.35, market_prob=0.20,
            edge=0.15, member_count=50, total_members=143, confidence=0.70,
        )
        ps = size_position(bp, bankroll=10000.0)
        assert ps.is_valid

        tid1 = record_paper_trade("nyc", date(2026, 3, 22), ps, db_path)
        assert tid1 is not None

        tid2 = record_paper_trade("nyc", date(2026, 3, 22), ps, db_path)
        assert tid2 is None, "Duplicate trade should return None"


# ===================================================================
# Run all tests
# ===================================================================

def run_all():
    """Simple test runner — no pytest dependency needed."""
    import traceback

    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"  {passed} passed, {failed} failed, {passed + failed} total")
    print(f"{'='*50}")

    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
