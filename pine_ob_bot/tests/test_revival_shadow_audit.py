"""Unit tests for the observe-only revival shadow audit.

The audit (cfg.revival_shadow_audit) must never create, fill or cancel a
real order; it only tracks trend-cancelled setups and reports whether a
controlled revival (trend back within 3/6/12 bars while the OB is valid,
then a FRESH same-bar sweep+reclaim) would have produced fills.

Run with:  pytest pine_ob_bot/tests/test_revival_shadow_audit.py -v
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

BULL = TrendDirection("bullish")
BEAR = TrendDirection("bearish")


def candle(t, o, h, l, c):
    return Candle(time=t, open=o, high=h, low=l, close=c)


def make_broker(audit=True):
    cfg = BotConfig(entry_mode="sweep_reclaim", revival_shadow_audit=audit,
                    fallback_spread=0.2)
    broker = PaperBroker(cfg, 10_000.0)
    order = PendingOrder(id="o1", ob_id="ob1", direction="bull", entry=100.0,
                         stop=95.0, target=110.0, created_index=0,
                         created_time="t0", active=True,
                         meta={"atr_at_formation": 1.0},
                         lifecycle_state="armed")
    broker.pending.append(order)
    broker.market_index = 5
    return broker, order


def suspend(broker):
    broker.update_m5_trend(BULL, "t")
    broker.update_m5_trend(BEAR, "t1")  # cancels the bull order at index 5


def test_flag_off_zero_shadow_state():
    broker, order = make_broker(audit=False)
    suspend(broker)
    assert not order.active                      # real cancel unchanged
    assert broker.revival_shadow == []
    assert broker.stats["trend_suspended"] == 0


def test_full_cycle_suspend_return_fresh_sweep_shadow_win():
    broker, order = make_broker()
    suspend(broker)
    assert broker.stats["trend_suspended"] == 1
    assert broker.revival_shadow[0]["state"] == "suspended"
    broker.process_signal_candle(candle("t6", 101, 102, 100.5, 101.5), 6)
    broker.update_m5_trend(BULL, "t7")           # trend returns
    broker.process_signal_candle(candle("t7", 101, 102, 100.5, 101.5), 7)
    rec = broker.revival_shadow[0]
    assert broker.stats["trend_returned_3"] == 1 and rec["state"] == "awaiting"
    broker.process_signal_candle(candle("t8", 101, 102, 99, 101), 8)
    assert broker.stats["reactivation_fresh_sweep"] == 1
    assert broker.stats["reactivation_confirmed"] == 1
    assert broker.stats["reactivation_would_fill"] == 1
    pos = broker.shadow_positions[0]
    assert abs(pos["stop"] - 94.8) < 1e-9 and abs(pos["fill"] - 101.2) < 1e-9
    broker.process_signal_candle(
        candle("t9", 101, pos["target"] + 1, 100.9, pos["target"]), 9)
    assert broker.stats["reactivation_wins"] == 1
    assert abs(broker.stats["reactivation_hypothetical_r"] - 1.5) < 1e-9
    assert broker.shadow_positions == []
    # the REAL order stayed cancelled throughout
    assert not order.active and order.lifecycle_state == "cancelled_trend"


def test_reclaim_before_trend_return_never_counts():
    broker, _ = make_broker()
    suspend(broker)
    broker.process_signal_candle(candle("t6", 101, 102, 99, 101), 6)
    assert broker.stats["reactivation_fresh_sweep"] == 0
    assert broker.stats["reactivation_confirmed"] == 0


def test_one_revival_per_ob():
    broker, _ = make_broker()
    suspend(broker)
    broker.update_m5_trend(BULL, "t7")
    broker.process_signal_candle(candle("t7", 101, 102, 100.5, 101.5), 7)
    broker.update_m5_trend(BEAR, "t8")           # trend leaves again
    broker.process_signal_candle(candle("t8", 101, 102, 100.5, 101.5), 8)
    assert broker.revival_shadow[0]["state"] == "trend_left_again"
    broker.update_m5_trend(BULL, "t9")
    broker.process_signal_candle(candle("t9", 101, 102, 99, 101), 9)
    assert broker.stats["reactivation_confirmed"] == 0


def test_ob_death_ends_tracking():
    broker, _ = make_broker()
    suspend(broker)
    broker.cancel_ob("ob1")
    assert broker.revival_shadow[0]["state"] == "dead_ob"
    broker.update_m5_trend(BULL, "t7")
    broker.process_signal_candle(candle("t7", 101, 102, 99, 101), 7)
    assert broker.stats["reactivation_ob_still_valid"] == 0


def test_trend_return_after_twelve_bars_rejected():
    broker, _ = make_broker()
    suspend(broker)
    broker.update_m5_trend(BULL, "t20")
    broker.process_signal_candle(candle("t20", 101, 102, 100.5, 101.5), 20)
    assert broker.revival_shadow[0]["state"] == "return_too_late"
    assert broker.stats["reactivation_ob_still_valid"] == 0


def test_return_lag_buckets():
    for lag, key in ((5, "trend_returned_6"), (10, "trend_returned_12")):
        broker, _ = make_broker()
        suspend(broker)
        broker.update_m5_trend(BULL, "tx")
        broker.process_signal_candle(
            candle("tx", 101, 102, 100.5, 101.5), 5 + lag)
        assert broker.stats[key] == 1, (lag, key)


def test_dump_load_roundtrip_keeps_shadow_state():
    broker, _ = make_broker()
    suspend(broker)
    data = broker.dump_state()
    restored, _ = make_broker()
    restored.load_state(data)
    assert restored.revival_shadow
    assert restored.stats["trend_suspended"] == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
