"""Single source of truth for the M5 trend-direction trade filter.

Trading is only ever allowed in the direction of the *current* M5 structural
trend. That trend comes from exactly one place: ``PineSwingOBEngine.trend``
-- the same causal, already-tested BOS/CHoCH state machine the strategy
already uses (1 = bullish, -1 = bearish, 0 = neutral / no confirmed break
yet). It is never inferred from M15 context, candle color, short-term
slope, or Order Block direction alone, and M15 never overrides it (M15
stays context/metadata only).

Every execution path -- live tick fills, candle-replay fills, sweep/reclaim
fills, the historical OHLC backtest, and the tick-level backtest -- must
call the functions below rather than re-implementing this check, so Paper,
Demo (which only ever mirrors a Paper-approved position) and every backtest
runner agree by construction.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TrendDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


TRADE_REJECTED_M5_TREND_MISMATCH = "TRADE_REJECTED_M5_TREND_MISMATCH"
TRADE_REJECTED_M5_TREND_NEUTRAL = "TRADE_REJECTED_M5_TREND_NEUTRAL"
ORDER_CANCELLED_M5_TREND_CHANGED = "ORDER_CANCELLED_M5_TREND_CHANGED"
ORDER_CANCELLED_M5_TREND_NEUTRAL = "ORDER_CANCELLED_M5_TREND_NEUTRAL"

# This project's own OrderBlock/PendingOrder/PaperPosition "side" is the
# bull/bear Direction literal already used everywhere in models.py (bull ==
# buy, bear == sell -- see demo_executor.py's `is_buy = direction == "bull"`).
# Reuse that vocabulary rather than inventing a parallel buy/sell one; the
# buy/sell spellings are accepted too so callers that prefer that wording
# (e.g. tests) aren't forced into the internal naming.
_BUY_SIDE = frozenset({"bull", "buy"})
_SELL_SIDE = frozenset({"bear", "sell"})


def trend_from_engine_state(value: int) -> TrendDirection:
    """Map ``PineSwingOBEngine.trend`` (1 / -1 / 0) to ``TrendDirection``."""
    if value == 1:
        return TrendDirection.BULLISH
    if value == -1:
        return TrendDirection.BEARISH
    return TrendDirection.NEUTRAL


@dataclass(frozen=True)
class TrendValidationResult:
    is_allowed: bool
    current_trend: TrendDirection
    side: str
    rejection_reason: str | None


def is_trade_allowed_by_m5_trend(side: str, current_trend: TrendDirection) -> bool:
    if current_trend == TrendDirection.NEUTRAL:
        return False
    if side in _BUY_SIDE:
        return current_trend == TrendDirection.BULLISH
    if side in _SELL_SIDE:
        return current_trend == TrendDirection.BEARISH
    return False


def validate_trade_direction(side: str, current_trend: TrendDirection) -> TrendValidationResult:
    """The one function every order-creation and every fill path must call.

    Buy + Bullish = allowed. Sell + Bearish = allowed. Buy + Bearish,
    Sell + Bullish, and any side while Neutral = rejected. An unrecognized
    side is always rejected (mismatch), never silently allowed.
    """
    allowed = is_trade_allowed_by_m5_trend(side, current_trend)
    if allowed:
        return TrendValidationResult(True, current_trend, side, None)
    reason = (TRADE_REJECTED_M5_TREND_NEUTRAL if current_trend == TrendDirection.NEUTRAL
              else TRADE_REJECTED_M5_TREND_MISMATCH)
    return TrendValidationResult(False, current_trend, side, reason)
