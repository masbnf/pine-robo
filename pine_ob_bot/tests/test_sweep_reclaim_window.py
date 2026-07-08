"""Unit tests for the flag-gated multi-bar sweep/reclaim window.

Covers PaperBroker._sweep_reclaim_window via process_signal_candle:
default N=1 must stay bit-identical to the original same-bar rule, N>1 may
only add gap-return confirmations, and the lag/expiry telemetry must count
correctly.

Run with:  pytest pine_ob_bot/tests/test_sweep_reclaim_window.py -v
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.models import Candle, PendingOrder
    from pine_ob_bot.paper import PaperBroker
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from models import Candle, PendingOrder
    from paper import PaperBroker


def candle(t, o, h, l, c):
    return Candle(time=t, open=o, high=h, low=l, close=c)


def bull_order():
    return PendingOrder(id="o", ob_id="ob", direction="bull", entry=100.0,
                        stop=95.0, target=110.0, created_index=0,
                        created_time="t0", active=True,
                        meta={"atr_at_formation": 1.0}, lifecycle_state="armed")


def bear_order():
    return PendingOrder(id="o", ob_id="ob", direction="bear", entry=100.0,
                        stop=105.0, target=90.0, created_index=0,
                        created_time="t0", active=True,
                        meta={"atr_at_formation": 1.0}, lifecycle_state="armed")


def run(max_bars, candles, order_factory=bull_order):
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim",
                                   sweep_reclaim_max_bars=max_bars), 10_000.0)
    order = order_factory()
    broker.pending.append(order)
    for i, c in enumerate(candles, start=1):
        broker.process_signal_candle(c, i)
    return order, broker.stats


def test_same_bar_confirms_at_any_window():
    for n in (1, 2, 3):
        order, stats = run(n, [candle("t1", 101, 102, 99, 101)])
        assert order.meta.get("sweep_reclaim_confirmed")
        assert order.meta["sweep_reclaim_lag_bars"] == 0
        assert stats["sweep_reclaim_lag0_confirms"] == 1


def test_gap_return_blocked_at_default_window():
    # Bar 1 sweeps and closes below; bar 2 trades entirely above the edge.
    delayed = [candle("t1", 101, 101.5, 99, 99.5),
               candle("t2", 100.3, 101.5, 100.2, 101)]
    order, stats = run(1, delayed)
    assert not order.meta.get("sweep_reclaim_confirmed")
    assert stats["sweep_reclaim_window_expired"] == 1


def test_gap_return_confirms_lag1_at_window_two():
    delayed = [candle("t1", 101, 101.5, 99, 99.5),
               candle("t2", 100.3, 101.5, 100.2, 101)]
    order, stats = run(2, delayed)
    assert order.meta["sweep_reclaim_lag_bars"] == 1
    assert stats["sweep_reclaim_lag1_confirms"] == 1
    assert stats["sweep_reclaim_entry_swept"] == 1


def test_continuous_linger_pop_confirms_under_both_windows():
    # In continuous prices the pop-above bar opens below the edge, so its own
    # low sweeps too: the ORIGINAL same-bar rule already catches it (lag 0).
    linger = [candle("t1", 101, 101.5, 99, 99.5),
              candle("t2", 99.5, 99.9, 98.8, 99.2),
              candle("t3", 99.2, 101.2, 99.0, 101)]
    for n in (1, 2):
        order, _ = run(n, linger)
        assert order.meta.get("sweep_reclaim_confirmed")
        assert order.meta["sweep_reclaim_lag_bars"] == 0


def test_bear_mirror_and_atr_buffer_stop():
    bars = [candle("t1", 99, 101, 98.5, 100.5),
            candle("t2", 99.8, 99.9, 99.0, 99.2)]
    order, _ = run(2, bars, bear_order)
    assert order.meta["sweep_reclaim_lag_bars"] == 1
    assert abs(order.stop - 105.2) < 1e-9  # 105 + 0.20 ATR buffer


def test_no_sweep_no_state():
    order, stats = run(2, [candle("t1", 101, 102, 100.5, 101.5)])
    assert not order.meta.get("sweep_reclaim_confirmed")
    assert stats["sweep_reclaim_entry_swept"] == 0


def _random_bars(n=400, seed=7):
    random.seed(seed)
    px, out = 101.0, []
    for i in range(1, n):
        op = px
        cl = max(96.5, op + random.uniform(-0.8, 0.8))
        hi = max(op, cl) + random.uniform(0, 0.4)
        lo = max(min(op, cl) - random.uniform(0, 0.4), 95.5)
        out.append((i, op, hi, lo, cl))
        px = cl
    return out


def test_default_window_bit_identical_to_original_rule():
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim",
                                   sweep_reclaim_max_bars=1), 10_000.0)
    order = bull_order()
    broker.pending.append(order)
    confirms = []
    for i, op, hi, lo, cl in _random_bars():
        broker.process_signal_candle(candle(f"t{i}", op, hi, lo, cl), i)
        if order.meta.get("sweep_reclaim_confirmed"):
            confirms.append(i)
            order.meta.pop("sweep_reclaim_confirmed")
            order.meta.pop("sweep_reclaim_sweep_index", None)
    original_rule = [i for i, op, hi, lo, cl in _random_bars()
                     if lo < 100.0 and cl > 100.0]
    assert confirms == original_rule


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
