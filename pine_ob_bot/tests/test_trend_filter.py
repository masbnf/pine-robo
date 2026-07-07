"""Unit tests for the single, shared M5 trend-direction trade filter.

Rules under test (project vocabulary: "bull" == Buy, "bear" == Sell, matching
PendingOrder/PaperPosition.direction and demo_executor.py's own
``is_buy = direction == "bull"`` convention):

    Buy  + Bullish -> allowed
    Sell + Bearish -> allowed
    Buy  + Bearish -> rejected (mismatch)
    Sell + Bullish -> rejected (mismatch)
    any side + Neutral -> rejected (neutral)
    unrecognized side -> rejected

Run with:  pytest pine_ob_bot/tests/test_trend_filter.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.trend_filter import (
        TRADE_REJECTED_M5_TREND_MISMATCH,
        TRADE_REJECTED_M5_TREND_NEUTRAL,
        TrendDirection,
        is_trade_allowed_by_m5_trend,
        trend_from_engine_state,
        validate_trade_direction,
    )
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from trend_filter import (
        TRADE_REJECTED_M5_TREND_MISMATCH,
        TRADE_REJECTED_M5_TREND_NEUTRAL,
        TrendDirection,
        is_trade_allowed_by_m5_trend,
        trend_from_engine_state,
        validate_trade_direction,
    )


def test_buy_in_bullish_is_allowed():
    assert is_trade_allowed_by_m5_trend("bull", TrendDirection.BULLISH) is True
    result = validate_trade_direction("bull", TrendDirection.BULLISH)
    assert result.is_allowed is True
    assert result.rejection_reason is None


def test_sell_in_bearish_is_allowed():
    assert is_trade_allowed_by_m5_trend("bear", TrendDirection.BEARISH) is True
    result = validate_trade_direction("bear", TrendDirection.BEARISH)
    assert result.is_allowed is True
    assert result.rejection_reason is None


def test_buy_in_bearish_is_rejected():
    assert is_trade_allowed_by_m5_trend("bull", TrendDirection.BEARISH) is False
    result = validate_trade_direction("bull", TrendDirection.BEARISH)
    assert result.is_allowed is False
    assert result.rejection_reason == TRADE_REJECTED_M5_TREND_MISMATCH


def test_sell_in_bullish_is_rejected():
    assert is_trade_allowed_by_m5_trend("bear", TrendDirection.BULLISH) is False
    result = validate_trade_direction("bear", TrendDirection.BULLISH)
    assert result.is_allowed is False
    assert result.rejection_reason == TRADE_REJECTED_M5_TREND_MISMATCH


def test_buy_in_neutral_is_rejected():
    result = validate_trade_direction("bull", TrendDirection.NEUTRAL)
    assert result.is_allowed is False
    assert result.rejection_reason == TRADE_REJECTED_M5_TREND_NEUTRAL


def test_sell_in_neutral_is_rejected():
    result = validate_trade_direction("bear", TrendDirection.NEUTRAL)
    assert result.is_allowed is False
    assert result.rejection_reason == TRADE_REJECTED_M5_TREND_NEUTRAL


def test_invalid_side_is_rejected():
    for side in ("hold", "", "BULL", 123, None):
        assert is_trade_allowed_by_m5_trend(side, TrendDirection.BULLISH) is False
        assert is_trade_allowed_by_m5_trend(side, TrendDirection.BEARISH) is False
        result = validate_trade_direction(side, TrendDirection.BULLISH)
        assert result.is_allowed is False


def test_buy_sell_spellings_are_also_accepted():
    """The project's own vocabulary is bull/bear, but buy/sell must work too
    since the requested prototype used that wording."""
    assert is_trade_allowed_by_m5_trend("buy", TrendDirection.BULLISH) is True
    assert is_trade_allowed_by_m5_trend("sell", TrendDirection.BEARISH) is True
    assert is_trade_allowed_by_m5_trend("buy", TrendDirection.BEARISH) is False
    assert is_trade_allowed_by_m5_trend("sell", TrendDirection.BULLISH) is False


def test_trend_from_engine_state_mapping():
    """Maps PineSwingOBEngine.trend (1 / -1 / 0) to the shared enum."""
    assert trend_from_engine_state(1) == TrendDirection.BULLISH
    assert trend_from_engine_state(-1) == TrendDirection.BEARISH
    assert trend_from_engine_state(0) == TrendDirection.NEUTRAL


def test_validation_result_carries_current_trend_and_side():
    result = validate_trade_direction("bull", TrendDirection.BEARISH)
    assert result.current_trend == TrendDirection.BEARISH
    assert result.side == "bull"
