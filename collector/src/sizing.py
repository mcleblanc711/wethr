"""
Position sizing via fractional Kelly criterion.

Kelly formula for binary outcomes at price p, model probability q:
    f* = (q - p) / (1 - p)

We use 5% fractional Kelly because:
    1. Full Kelly is optimal only with perfect probability estimates
    2. At 5%, raw Kelly sizes span ~$30-$150 for a $10K bankroll,
       so the $100 hard cap only binds for the strongest signals
    3. This lets Kelly actually differentiate: a 20% edge bet gets
       2-3x the capital of an 8% edge bet, instead of both being
       flattened to $100

Entry price cap at 50c because:
    At 50c entry, you need 50% accuracy to break even (win +$100, lose -$100).
    At 70c entry, you need 70% accuracy (win +$43, lose -$100).
    Raw ensemble counting is under-dispersed, so apparent edges at high entry
    prices often evaporate. Until EMOS calibration is trained, the 50c cap
    keeps the risk/reward honest.

Hard caps:
    - 5% of bankroll per trade
    - $100 absolute max per trade
    - Daily loss circuit breaker at $300
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config
from .probability import BracketProbability

log = logging.getLogger(__name__)


@dataclass
class PositionSize:
    """Calculated position size for a trade."""
    bracket_prob: BracketProbability
    full_kelly: float          # Full Kelly fraction
    fractional_kelly: float    # After applying KELLY_FRACTION
    raw_size_usd: float        # Before caps
    capped_size_usd: float     # After all caps
    side: str                  # "YES" or "NO"
    entry_price: float         # Price we'd pay
    win_pnl: float = 0.0      # Payout if we win
    loss_pnl: float = 0.0     # Loss if we lose (always negative)
    breakeven_wr: float = 0.0 # Win rate needed to break even
    reason_skipped: str = ""   # If empty, trade is valid

    @property
    def is_valid(self) -> bool:
        return not self.reason_skipped and self.capped_size_usd > 0


def calculate_kelly(
    model_prob: float,
    market_price: float,
) -> tuple[float, str]:
    """
    Calculate Kelly fraction for a binary bet.

    If model_prob > market_price -> buy YES at market_price
    If model_prob < market_price -> buy NO at (1 - market_price)

    Returns:
        (kelly_fraction, side)
        kelly_fraction can be negative (meaning don't bet)
    """
    if model_prob > market_price:
        # Buy YES
        side = "YES"
        p = market_price
        q = model_prob
    else:
        # Buy NO
        side = "NO"
        p = 1.0 - market_price
        q = 1.0 - model_prob

    # Avoid division by zero
    if p <= 0 or p >= 1:
        return 0.0, side

    # Kelly: f* = (q - p) / (1 - p)
    kelly = (q - p) / (1.0 - p)
    return kelly, side


def size_position(
    bp: BracketProbability,
    bankroll: float,
    daily_pnl: float = 0.0,
    pending_count: int = 0,
) -> PositionSize:
    """
    Calculate position size for a bracket with edge.

    Applies fractional Kelly, bankroll %, absolute caps, and circuit breakers.
    """
    full_kelly, side = calculate_kelly(bp.model_prob, bp.market_prob)
    entry_price = bp.market_prob if side == "YES" else (1.0 - bp.market_prob)

    # Breakeven win rate = entry price for any binary bet
    breakeven_wr = entry_price

    # --- Validation gates ---
    reason = ""

    if full_kelly <= 0:
        reason = "Negative Kelly (no edge after pricing)"
    elif entry_price < config.MIN_ENTRY_PRICE:
        reason = f"Entry price {entry_price:.2f} below min {config.MIN_ENTRY_PRICE}"
    elif entry_price > config.MAX_ENTRY_PRICE:
        reason = (
            f"Entry price {entry_price:.2f} > max {config.MAX_ENTRY_PRICE} "
            f"(need {breakeven_wr:.0%} accuracy to break even)"
        )
    elif abs(bp.edge) < config.MIN_EDGE_THRESHOLD:
        reason = f"|Edge| {abs(bp.edge):.1%} < threshold {config.MIN_EDGE_THRESHOLD:.0%}"
    elif bp.confidence < config.MIN_ENSEMBLE_CONFIDENCE:
        reason = f"Low confidence {bp.confidence:.1%} < {config.MIN_ENSEMBLE_CONFIDENCE:.0%}"
    elif daily_pnl < -config.DAILY_LOSS_LIMIT:
        reason = f"Daily loss limit hit (${daily_pnl:,.0f})"
    elif pending_count >= config.MAX_PENDING_TRADES:
        reason = f"Max pending trades ({config.MAX_PENDING_TRADES})"

    fractional = full_kelly * config.KELLY_FRACTION

    # Raw size in USD
    raw_usd = fractional * bankroll

    # Apply caps
    max_from_bankroll = config.MAX_BANKROLL_PCT * bankroll
    capped_usd = min(raw_usd, max_from_bankroll, config.MAX_TRADE_SIZE_USD)
    capped_usd = max(capped_usd, 0.0)  # Floor at zero

    # Minimum trade size (not worth the gas/effort below $1)
    if capped_usd < 1.0 and not reason:
        reason = f"Position too small (${capped_usd:.2f})"
        capped_usd = 0.0

    # Compute actual win/loss P&L at this size
    if entry_price > 0 and capped_usd > 0:
        contracts = capped_usd / entry_price
        win_pnl = contracts * (1.0 - entry_price)
        loss_pnl = -capped_usd
    else:
        win_pnl = 0.0
        loss_pnl = 0.0

    ps = PositionSize(
        bracket_prob=bp,
        full_kelly=full_kelly,
        fractional_kelly=fractional,
        raw_size_usd=raw_usd,
        capped_size_usd=round(capped_usd, 2),
        side=side,
        entry_price=entry_price,
        win_pnl=round(win_pnl, 2),
        loss_pnl=round(loss_pnl, 2),
        breakeven_wr=round(breakeven_wr, 4),
        reason_skipped=reason,
    )

    if ps.is_valid:
        log.info(
            f"  SIZE: {bp.bracket.label} -> {side} @ {entry_price:.2f}, "
            f"${capped_usd:.2f} "
            f"(Kelly={full_kelly:.3f}, frac={fractional:.4f}, "
            f"raw=${raw_usd:.0f}, "
            f"win=+${win_pnl:.0f}/loss=-${capped_usd:.0f}, "
            f"BE={breakeven_wr:.0%})"
        )
    elif reason:
        log.debug(f"  SKIP: {bp.bracket.label} -- {reason}")

    return ps
