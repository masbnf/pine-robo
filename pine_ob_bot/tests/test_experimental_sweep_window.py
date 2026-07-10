"""Multi-bar sweep/reclaim: telemetry and metadata completions.

Complements test_sweep_reclaim_window.py (same-bar equivalence, lag1,
expiry) with the spec's remaining cases: lag-2 confirmation, the documented
deeper-sweep window reset, trend/OB cancellation of an open window, and the
sweep_index/reclaim_index/sweep_price/max_bars confirm metadata.

Run with:  pytest pine_ob_bot/tests/test_experimental_sweep_window.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.models import Candle, PendingOrder
    from pine_ob_bot.paper import PaperBroker
    from pine_ob_bot.trend_filter import TrendDirection
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from models import Candle, PendingOrder
    from paper import PaperBroker
    from trend_filter import TrendDirection


def candle(t, o, h, l, c):
    return Candle(time=t, open=o, high=h, low=l, close=c)


def bull_order():
    return PendingOrder(id="o", ob_id="ob", direction="bull", entry=100.0,
                        stop=95.0, target=110.0, created_index=0,
                        created_time="t0", active=True,
                        meta={"atr_at_formation": 1.0, "break_kind": "BOS"},
                        lifecycle_state="armed")


def broker_with(max_bars, order=None):
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim",
                                   sweep_reclaim_max_bars=max_bars), 10_000.0)
    order = order or bull_order()
    broker.pending.append(order)
    return broker, order


def test_lag2_confirm_counts_lag2plus():
    broker, order = broker_with(3)
    # bar1 sweeps (low 99, close 99.5); bar2 neither sweeps nor reclaims
    # (low == close == entry exactly); bar3 reclaims -> lag 2.
    broker.process_signal_candle(candle("t1", 101, 101.5, 99, 99.5), 1)
    broker.process_signal_candle(candle("t2", 100.4, 100.6, 100.0, 100.0), 2)
    broker.process_signal_candle(candle("t3", 100.2, 101.5, 100.1, 101), 3)
    assert order.meta.get("sweep_reclaim_confirmed")
    assert order.meta["sweep_reclaim_lag_bars"] == 2
    assert broker.stats["sweep_reclaim_lag2plus_confirms"] == 1


def test_deeper_sweep_restarts_window_and_counts_reset():
    broker, order = broker_with(2)
    # bar1 sweep to 99; bar2 deeper sweep to 98.5 (still closes below);
    # bar3 reclaims WITHOUT sweeping again (low stays above the edge).
    # Without the bar2 restart the 2-bar window measured from bar1 would
    # have expired at bar3.
    broker.process_signal_candle(candle("t1", 101, 101.5, 99, 99.5), 1)
    broker.process_signal_candle(candle("t2", 99.5, 99.8, 98.5, 99.0), 2)
    broker.process_signal_candle(candle("t3", 100.3, 101.4, 100.2, 101), 3)
    assert order.meta.get("sweep_reclaim_confirmed")
    assert broker.stats["sweep_reclaim_reset_by_deeper_sweep"] == 1
    assert broker.stats["sweep_reclaim_entry_swept"] == 1  # one EPISODE
    # lag is measured from the episode's most recent sweep bar (bar2).
    assert order.meta["sweep_reclaim_lag_bars"] == 1
    assert order.meta["sweep_price"] == 98.5  # deepest extreme of the episode


def test_confirm_metadata_is_complete():
    broker, order = broker_with(2)
    broker.process_signal_candle(candle("t1", 101, 101.5, 99, 99.5), 1)
    broker.process_signal_candle(candle("t2", 100.3, 101.5, 100.2, 101), 2)
    assert order.meta["sweep_index"] == 1
    assert order.meta["reclaim_index"] == 2
    assert order.meta["sweep_price"] == 99
    assert order.meta["sweep_reclaim_max_bars"] == 2
    assert order.meta["sweep_reclaim_lag_bars"] == 1
    assert order.meta["reclaim_close"] == 101


def test_trend_change_cancels_open_window():
    broker, order = broker_with(3)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    broker.process_signal_candle(candle("t1", 101, 101.5, 99, 99.5), 1)  # sweep, open window
    assert order.meta.get("sweep_reclaim_sweep_index") == 1
    broker.update_m5_trend(TrendDirection.BEARISH, "t2")
    assert order.active is False
    assert order.lifecycle_state == "cancelled_trend"
    assert broker.stats["sweep_reclaim_cancelled_trend"] == 1


def test_ob_invalidation_cancels_open_window():
    broker, order = broker_with(3)
    broker.process_signal_candle(candle("t1", 101, 101.5, 99, 99.5), 1)
    assert order.meta.get("sweep_reclaim_sweep_index") == 1
    broker.cancel_ob("ob")
    assert order.active is False
    assert broker.stats["sweep_reclaim_cancelled_ob_invalid"] == 1
    # An expired-window order later reclaiming must NOT confirm.
    broker.process_signal_candle(candle("t2", 100.3, 101.5, 100.2, 101), 2)
    assert not order.meta.get("sweep_reclaim_confirmed")


def test_default_window_never_counts_deeper_sweep_reset():
    """max_bars=1: the window can never span bars, so the reset counter must
    stay zero -- guarding the legacy telemetry surface."""
    broker, order = broker_with(1)
    for i in range(1, 6):
        broker.process_signal_candle(candle(f"t{i}", 99.6, 99.9, 98.5 - i * 0.1, 99.5), i)
    assert broker.stats["sweep_reclaim_reset_by_deeper_sweep"] == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
