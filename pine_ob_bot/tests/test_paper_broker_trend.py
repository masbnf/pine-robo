"""Integration tests: the M5 trend filter wired into the real PaperBroker.

Covers the pending-order scenarios and per-path fill checks from the trend
filter requirements, using PaperBroker.update_m5_trend directly to drive
trend transitions (test_trend_engine_integration.py separately drives the
same broker from the real BOS/CHoCH engine).

Run with:  pytest pine_ob_bot/tests/test_paper_broker_trend.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.models import Candle, OrderBlock, Tick
    from pine_ob_bot.paper import PaperBroker, SymbolSpec
    from pine_ob_bot.trend_filter import (
        ORDER_CANCELLED_M5_TREND_CHANGED, ORDER_CANCELLED_M5_TREND_NEUTRAL,
        TRADE_REJECTED_M5_TREND_MISMATCH, TRADE_REJECTED_M5_TREND_NEUTRAL, TrendDirection)
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from models import Candle, OrderBlock, Tick
    from paper import PaperBroker, SymbolSpec
    from trend_filter import (ORDER_CANCELLED_M5_TREND_CHANGED, ORDER_CANCELLED_M5_TREND_NEUTRAL,
                              TRADE_REJECTED_M5_TREND_MISMATCH, TRADE_REJECTED_M5_TREND_NEUTRAL,
                              TrendDirection)


def _cfg(**overrides) -> BotConfig:
    return BotConfig(risk_fraction=0.01, **overrides)


def _spec() -> SymbolSpec:
    return SymbolSpec(tick_size=0.01, tick_value=1.0, volume_min=0.01,
                      volume_max=100.0, volume_step=0.01)


def _broker(cfg: BotConfig | None = None, equity: float = 10_000.0) -> PaperBroker:
    return PaperBroker(cfg or _cfg(), equity, _spec())


def _bull_ob(entry: float = 101.0, stop: float = 100.0, ob_id: str = "ob-bull",
            formed_index: int = 0) -> OrderBlock:
    # OrderBlock.entry == high, .stop == low for a bull OB.
    return OrderBlock(ob_id, "bull", entry, stop, formed_index, "t0",
                      formed_index, "t0", True, "BOS", None)


def _bear_ob(entry: float = 99.0, stop: float = 100.0, ob_id: str = "ob-bear",
            formed_index: int = 0) -> OrderBlock:
    # OrderBlock.entry == low, .stop == high for a bear OB.
    return OrderBlock(ob_id, "bear", stop, entry, formed_index, "t0",
                      formed_index, "t0", True, "BOS", None)


# --- Scenario 1: Bullish trend, Buy pending, still Bullish at fill -> filled ---

def test_scenario1_buy_filled_when_trend_stays_bullish():
    broker = _broker()
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    order = broker.add_ob(_bull_ob(entry=101.0, stop=100.0), {})
    assert order is not None and order.active is True

    trades = broker.process_tick(Tick("t1", bid=100.95, ask=101.0))
    assert trades == []
    assert len(broker.positions) == 1
    assert broker.positions[0].direction == "bull"


# --- Scenario 2: Bullish trend, Sell signal -> rejected, nothing created ---

def test_scenario2_sell_signal_rejected_in_bullish_trend():
    broker = _broker()
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    order = broker.add_ob(_bear_ob(entry=99.0, stop=100.0), {})
    assert order is None
    assert broker.pending == []
    assert broker.positions == []
    assert broker.trend_events[-1]["reason"] == TRADE_REJECTED_M5_TREND_MISMATCH


# --- Scenario 3: Neutral trend, Buy signal -> rejected ---

def test_scenario3_buy_signal_rejected_in_neutral_trend():
    broker = _broker()  # current_m5_trend defaults to NEUTRAL
    order = broker.add_ob(_bull_ob(), {})
    assert order is None
    assert broker.trend_events[-1]["reason"] == TRADE_REJECTED_M5_TREND_NEUTRAL


# --- Scenario 4: Bullish -> Buy pending -> trend flips Bearish -> cancelled ---

def test_scenario4_buy_pending_cancelled_when_trend_flips_bearish():
    broker = _broker()
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    order = broker.add_ob(_bull_ob(entry=101.0, stop=100.0), {})
    assert order is not None and order.active is True

    cancelled = broker.update_m5_trend(TrendDirection.BEARISH, "t1")
    assert cancelled == 1
    assert order.active is False
    assert order.lifecycle_state == "cancelled_trend"
    assert order.meta["cancellation_reason"] == ORDER_CANCELLED_M5_TREND_CHANGED
    assert broker.stats["cancelled_trend_change"] == 1

    # Price later reaches the old entry -- nothing should fill.
    trades = broker.process_tick(Tick("t2", bid=100.95, ask=101.0))
    assert trades == []
    assert broker.positions == []


# --- Scenario 5: Bearish -> Sell pending -> trend flips Bullish -> cancelled ---

def test_scenario5_sell_pending_cancelled_when_trend_flips_bullish():
    broker = _broker()
    broker.update_m5_trend(TrendDirection.BEARISH, "t0")
    order = broker.add_ob(_bear_ob(entry=99.0, stop=100.0), {})
    assert order is not None and order.active is True

    cancelled = broker.update_m5_trend(TrendDirection.BULLISH, "t1")
    assert cancelled == 1
    assert order.active is False
    assert order.meta["cancellation_reason"] == ORDER_CANCELLED_M5_TREND_CHANGED

    trades = broker.process_tick(Tick("t2", bid=99.0, ask=99.05))
    assert trades == []
    assert broker.positions == []


# --- Scenario 6: order created while Bullish; trend is Neutral at fill time ---

def test_scenario6_neutral_at_fill_time_blocks_fill_via_update_m5_trend():
    """The ordinary path: Neutral arrives through update_m5_trend, which
    cancels the order outright before any fill is attempted."""
    broker = _broker()
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    order = broker.add_ob(_bull_ob(entry=101.0, stop=100.0), {})
    assert order is not None

    broker.update_m5_trend(TrendDirection.NEUTRAL, "t1")
    assert order.active is False
    assert order.meta["cancellation_reason"] == ORDER_CANCELLED_M5_TREND_NEUTRAL

    trades = broker.process_tick(Tick("t2", bid=100.95, ask=101.0))
    assert trades == []
    assert broker.positions == []


def test_final_trend_recheck_inside_open_is_a_standalone_safety_net():
    """Defense in depth: even if current_m5_trend were Neutral without the
    order having been cancelled first (e.g. some future code path skips
    update_m5_trend), _open() itself must still refuse to fill. This proves
    the "recheck trend right before Fill" rule does not rely solely on the
    cancellation step having already run."""
    broker = _broker()
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    order = broker.add_ob(_bull_ob(entry=101.0, stop=100.0), {})
    assert order is not None

    # Bypass update_m5_trend's cancellation on purpose.
    broker.current_m5_trend = TrendDirection.NEUTRAL

    position = broker._open(order, fill=order.entry, when="t1", bar_index=0,
                            execution_source="test", execution_spread=0.0,
                            stop=order.stop, target=order.target)
    assert position is None
    assert order.active is False
    assert order.meta["rejection_reason"] == TRADE_REJECTED_M5_TREND_NEUTRAL
    assert broker.stats["rejected_trend_neutral"] == 1


# --- Per fill-path coverage: process_tick, process_candle, sweep_reclaim (both) ---

def test_process_tick_limit_fill_blocks_trend_mismatch():
    broker = _broker()
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    order = broker.add_ob(_bull_ob(entry=101.0, stop=100.0), {})
    assert order is not None
    broker.current_m5_trend = TrendDirection.BEARISH  # force mismatch without cancelling

    trades = broker.process_tick(Tick("t1", bid=100.95, ask=101.0))
    assert trades == []
    assert broker.positions == []
    assert order.active is False
    assert order.meta["rejection_reason"] == TRADE_REJECTED_M5_TREND_MISMATCH


def test_process_candle_limit_fill_blocks_trend_mismatch():
    broker = _broker()
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    ob = _bull_ob(entry=101.0, stop=100.0, formed_index=0)
    order = broker.add_ob(ob, {})
    assert order is not None
    broker.current_m5_trend = TrendDirection.BEARISH  # force mismatch without cancelling

    # bar_index=1 so age (1) >= max(1, min_entry_wait_bars) -- not the
    # formation candle. Candle touches down through the entry (101.0).
    candle = Candle("t1", 101.5, 101.5, 100.5, 101.2)
    closed = broker.process_candle(candle, bar_index=1)
    assert closed == []
    assert broker.positions == []
    assert order.active is False
    assert order.meta["rejection_reason"] == TRADE_REJECTED_M5_TREND_MISMATCH


def test_sweep_reclaim_via_process_candle_blocks_trend_mismatch():
    cfg = _cfg(entry_mode="sweep_reclaim")
    broker = _broker(cfg)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    ob = _bull_ob(entry=101.0, stop=100.0, formed_index=0)
    order = broker.add_ob(ob, {})
    assert order is not None
    broker.current_m5_trend = TrendDirection.BEARISH  # force mismatch without cancelling

    # Sweeps below stop (100.0) and reclaims back above entry (101.0) on close.
    candle = Candle("t1", 100.5, 101.5, 99.5, 101.2)
    closed = broker.process_candle(candle, bar_index=1)
    assert closed == []
    assert broker.positions == []
    assert order.active is False
    assert order.meta["rejection_reason"] == TRADE_REJECTED_M5_TREND_MISMATCH


def test_sweep_reclaim_via_process_tick_blocks_trend_mismatch():
    cfg = _cfg(entry_mode="sweep_reclaim")
    broker = _broker(cfg)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    ob = _bull_ob(entry=101.0, stop=100.0, formed_index=0)
    order = broker.add_ob(ob, {})
    assert order is not None
    # Normally set by process_signal_candle once the closed candle confirms
    # the sweep/reclaim; set directly here to isolate the tick-fill path.
    order.meta["sweep_reclaim_confirmed"] = True
    broker.current_m5_trend = TrendDirection.BEARISH  # force mismatch without cancelling

    trades = broker.process_tick(Tick("t1", bid=101.4, ask=101.5))
    assert trades == []
    assert broker.positions == []
    assert order.active is False
    assert order.meta["rejection_reason"] == TRADE_REJECTED_M5_TREND_MISMATCH


def test_valid_trend_still_fills_normally_process_candle():
    """Sanity check: the trend filter does not block a legitimately aligned
    fill -- only mismatches/Neutral are rejected."""
    broker = _broker()
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    ob = _bull_ob(entry=101.0, stop=100.0, formed_index=0)
    order = broker.add_ob(ob, {})
    assert order is not None

    candle = Candle("t1", 101.5, 101.5, 100.5, 101.2)
    broker.process_candle(candle, bar_index=1)
    assert len(broker.positions) == 1
    assert broker.positions[0].meta["trend_at_creation"] == TrendDirection.BULLISH.value
    assert broker.positions[0].meta["trend_at_fill_check"] == TrendDirection.BULLISH.value
