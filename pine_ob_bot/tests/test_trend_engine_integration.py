"""End-to-end trend-filter tests driven by the real BOS/CHoCH engine.

Unlike test_paper_broker_trend.py (which drives PaperBroker.update_m5_trend
directly), these tests feed a hand-verified M5 candle sequence through the
actual, unmodified PineSwingOBEngine and check that PaperBroker's trend gate
reacts correctly to genuine BOS/CHoCH transitions -- never M15, never candle
color, never Order Block direction alone.

The candle sequence below (swing_length=2 for a short, fast test) was
verified interactively: bars 0-6 stay Neutral (no confirmed break yet), bar 7
is a bullish BOS (trend -> Bullish), and bar 8 is a bearish CHoCH
(trend -> Bearish). It forms a bull OB at bar 7 and a bear OB at bar 8.

Run with:  pytest pine_ob_bot/tests/test_trend_engine_integration.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.models import Candle, Tick
    from pine_ob_bot.paper import PaperBroker, SymbolSpec
    from pine_ob_bot.pine_engine import PineSwingOBEngine
    from pine_ob_bot.trend_filter import (
        ORDER_CANCELLED_M5_TREND_CHANGED, TrendDirection, trend_from_engine_state)
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from models import Candle, Tick
    from paper import PaperBroker, SymbolSpec
    from pine_engine import PineSwingOBEngine
    from trend_filter import (ORDER_CANCELLED_M5_TREND_CHANGED, TrendDirection,
                              trend_from_engine_state)


# (open, high, low, close) -- see module docstring for the derived trend path.
BARS = [
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 100.2, 95.0, 95.5),
    (95.5, 96.0, 95.2, 95.8),
    (95.8, 96.2, 95.3, 96.0),
    (96.0, 105.0, 95.9, 104.0),
    (104.0, 104.5, 103.5, 104.2),
    (104.2, 104.6, 103.8, 104.4),
    (104.4, 106.0, 104.0, 105.5),   # bar 7: bullish BOS
    (105.5, 105.8, 94.0, 94.5),     # bar 8: bearish CHoCH
]


def _candles() -> list[Candle]:
    return [Candle(f"2024-01-01T00:{i:02d}:00+00:00", o, h, l, c)
            for i, (o, h, l, c) in enumerate(BARS)]


def _cfg() -> BotConfig:
    # allowed_break_kinds defaults to ("BOS",) -- this scenario deliberately
    # exercises a CHoCH-formed OB too, so both kinds must be allowed through
    # add_ob's pre-existing break-kind filter (unrelated to the trend gate).
    return BotConfig(swing_length=2, atr_period=5, risk_fraction=0.01,
                     allowed_break_kinds=("BOS", "CHoCH"))


def _spec() -> SymbolSpec:
    return SymbolSpec(tick_size=0.01, tick_value=1.0, volume_min=0.01,
                      volume_max=100.0, volume_step=0.01)


def test_engine_drives_neutral_then_bullish_then_bearish():
    """The trend is Neutral until the real engine confirms a break -- never
    inferred early from OB direction or candle color."""
    engine = PineSwingOBEngine(_cfg())
    broker = PaperBroker(_cfg(), 10_000.0, _spec())
    for index, candle in enumerate(_candles()):
        engine.process(candle)
        broker.update_m5_trend(trend_from_engine_state(engine.trend), candle.time)
        if index < 7:
            assert broker.current_m5_trend == TrendDirection.NEUTRAL, f"bar {index}"
        elif index == 7:
            assert broker.current_m5_trend == TrendDirection.BULLISH
        else:
            assert broker.current_m5_trend == TrendDirection.BEARISH


def test_full_acceptance_sequence_bos_then_choch():
    """Requirement acceptance sequence, driven by the real engine:
    1. start Neutral -- no trade allowed
    2. bullish BOS confirmed -> trend Bullish
    3. Buy order allowed (the bull OB formed at the BOS is admitted)
    4. bearish CHoCH confirmed -> trend Bearish
    5. the still-pending Buy order is cancelled
    6. Sell order allowed (the bear OB formed at the CHoCH is admitted)
    7. a brand-new Buy order is rejected
    """
    engine = PineSwingOBEngine(_cfg())
    broker = PaperBroker(_cfg(), 10_000.0, _spec())
    bull_order = None
    bear_order = None

    for candle in _candles()[:7]:
        engine.process(candle)
        broker.update_m5_trend(trend_from_engine_state(engine.trend), candle.time)
    assert broker.current_m5_trend == TrendDirection.NEUTRAL
    assert broker.pending == []  # nothing could have been created while Neutral

    # Bar 7: bullish BOS.
    breaks, formed, invalidated = engine.process(_candles()[7])
    broker.update_m5_trend(trend_from_engine_state(engine.trend), _candles()[7].time)
    assert broker.current_m5_trend == TrendDirection.BULLISH
    bull_ob = next(ob for ob in formed if ob.direction == "bull")
    bull_order = broker.add_ob(bull_ob, {})
    assert bull_order is not None and bull_order.active is True

    # Bar 8: bearish CHoCH -- must cancel the still-pending Buy before the
    # new bear OB is even considered.
    breaks, formed, invalidated = engine.process(_candles()[8])
    broker.update_m5_trend(trend_from_engine_state(engine.trend), _candles()[8].time)
    assert broker.current_m5_trend == TrendDirection.BEARISH
    assert bull_order.active is False
    assert bull_order.meta["cancellation_reason"] == ORDER_CANCELLED_M5_TREND_CHANGED
    assert broker.stats["cancelled_trend_change"] == 1

    bear_ob = next(ob for ob in formed if ob.direction == "bear")
    bear_order = broker.add_ob(bear_ob, {})
    assert bear_order is not None and bear_order.active is True

    # A brand-new Buy attempted after the flip must be rejected.
    another_bull_ob = bull_ob
    another_bull_ob.id = "another-bull-ob"
    rejected = broker.add_ob(another_bull_ob, {})
    assert rejected is None
    assert broker.stats["rejected_trend_mismatch"] >= 1


def test_live_and_historical_orchestration_agree_on_trend_state():
    """Feed the identical candle sequence through two different orchestration
    styles -- "historical" (process_candle drives fills) and "live" (tick-only
    fills via process_signal_candle + process_tick) -- and require identical
    trend state, identical cancellations, and identical admission decisions.
    Both styles call the exact same engine.process -> update_m5_trend ->
    add_ob sequence; only the fill mechanism differs."""
    engine_hist = PineSwingOBEngine(_cfg())
    broker_hist = PaperBroker(_cfg(), 10_000.0, _spec())
    engine_live = PineSwingOBEngine(_cfg())
    broker_live = PaperBroker(_cfg(), 10_000.0, _spec())

    for index, candle in enumerate(_candles()):
        # Historical-style step.
        breaks, formed, _ = engine_hist.process(candle)
        broker_hist.update_m5_trend(trend_from_engine_state(engine_hist.trend), candle.time)
        for ob in formed:
            broker_hist.add_ob(ob, {})
        broker_hist.process_candle(candle, index, spread=0.0, execution_source="historical_ohlc")

        # Live-style step: process_signal_candle only advances lifecycle/
        # market_index, never fills; fills are tick-only, strictly after.
        broker_live.process_signal_candle(candle, index)
        breaks, formed, _ = engine_live.process(candle)
        broker_live.update_m5_trend(trend_from_engine_state(engine_live.trend), candle.time)
        for ob in formed:
            broker_live.add_ob(ob, {})
        tick = Tick(candle.time, candle.close - 0.01, candle.close + 0.01)
        broker_live.process_tick(tick)

    assert broker_hist.current_m5_trend == broker_live.current_m5_trend == TrendDirection.BEARISH
    for key in ("cancelled_trend_change", "cancelled_trend_neutral",
                "rejected_trend_mismatch", "rejected_trend_neutral"):
        assert broker_hist.stats[key] == broker_live.stats[key], key
