"""Controlled Revival (--controlled-revival): REAL, flag-gated revival of
setups cancelled purely by an M5 trend change.

Trend-timing convention used everywhere below: the experimental candle hook
runs BEFORE update_m5_trend for the same bar (identical in all three
execution paths), so alignment is judged with the previous bar's trend --
a trend that returns "at bar K" is first visible to the hook at bar K+1.

Run with:  pytest pine_ob_bot/tests/test_controlled_revival.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.models import Candle, PendingOrder, Tick
    from pine_ob_bot.paper import PaperBroker
    from pine_ob_bot.trend_filter import TrendDirection
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from models import Candle, PendingOrder, Tick
    from paper import PaperBroker
    from trend_filter import TrendDirection


def candle(t, o, h, l, c):
    return Candle(time=t, open=o, high=h, low=l, close=c)


NEUTRAL = (101.0, 102.0, 100.5, 101.5)      # touches nothing on a 100/95 bull OB
SWEEP_RECLAIM = (101.0, 101.5, 99.0, 101.0)  # wick through 100, close back above
RECLAIM_ONLY = (100.3, 101.5, 100.2, 101.0)  # closes above without sweeping


def make_broker(**overrides):
    cfg = BotConfig(entry_mode="sweep_reclaim", controlled_revival=True, **overrides)
    return PaperBroker(cfg, 10_000.0)


def seed_order(broker, break_kind="BOS", strong=False, ob_id="ob1"):
    meta = {"break_kind": break_kind, "atr_at_formation": 1.0,
            "ob_entry_price": 100.0, "ob_stop_price": 95.0}
    if strong:
        meta["bos_displacement_top_quartile"] = True
    order = PendingOrder("orig", ob_id, "bull", 100.0, 95.0, 107.5, 0, "t0",
                         True, meta, "armed")
    broker.pending.append(order)
    return order


def bar(broker, index, shape=NEUTRAL):
    broker.process_signal_candle(candle(f"t{index}", *shape), index)


def suspend(broker, order):
    """Bull order cancelled by a bearish flip at bar 1."""
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    bar(broker, 1)
    broker.update_m5_trend(TrendDirection.BEARISH, "t1")
    assert order.active is False and order.lifecycle_state == "cancelled_trend"


def revival_order(broker):
    return next((o for o in broker.pending if o.meta.get("is_revival")), None)


def test_flag_off_creates_no_state():
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim"), 10_000.0)
    order = seed_order(broker)
    suspend(broker, order)
    assert broker.revival_records == []
    assert broker.stats["revival_suspended"] == 0


def test_trend_return_within_3_creates_new_order_and_links_metadata():
    broker = make_broker()
    original = seed_order(broker)
    suspend(broker, original)
    assert broker.stats["revival_suspended"] == 1
    bar(broker, 2)                                        # still bearish
    broker.update_m5_trend(TrendDirection.BULLISH, "t2")  # trend returns
    bar(broker, 3)                                        # hook sees the return
    new = revival_order(broker)
    assert new is not None and new.id != original.id
    assert original.active is False                       # never re-activated
    assert new.meta["revival_parent_order_id"] == "orig"
    assert new.meta["revival_parent_ob_id"] == "ob1"
    assert new.meta["revival_attempt"] == 1
    assert new.meta["revival_suspended_index"] == 1
    assert new.meta["revival_trend_return_index"] == 3
    assert new.meta["revival_return_lag_bars"] == 2
    assert broker.stats["revival_trend_returned"] == 1
    assert broker.stats["revival_returned_within_3"] == 1
    assert broker.stats["revival_returned_within_6"] == 1   # cumulative buckets
    assert broker.stats["revival_orders_created"] == 1
    assert broker.revival_attempts["ob1"] == 1


def test_fresh_sweep_required_and_pre_return_sweep_never_counts():
    broker = make_broker()
    original = seed_order(broker)
    suspend(broker, original)
    bar(broker, 2, SWEEP_RECLAIM)                         # sweep BEFORE return: unusable
    broker.update_m5_trend(TrendDirection.BULLISH, "t2")
    bar(broker, 3)                                        # revival order created here
    new = revival_order(broker)
    assert new is not None
    bar(broker, 4, RECLAIM_ONLY)                          # reclaim without a NEW sweep
    assert not new.meta.get("sweep_reclaim_confirmed")
    bar(broker, 5, SWEEP_RECLAIM)                         # fresh sweep + reclaim
    assert new.meta.get("sweep_reclaim_confirmed")
    assert new.meta["sweep_index"] == 5
    # Fill happens through the normal tick path; stop was ATR-buffered at
    # confirmation (95 - 0.20 * 1.0 ATR).
    trades = broker.process_tick(Tick("t5x", 100.9, 100.95))
    assert trades == [] and len(broker.positions) == 1
    assert abs(broker.positions[0].stop - 94.8) < 1e-9
    assert broker.positions[0].meta["revival_fresh_sweep_index"] == 5
    assert broker.stats["revival_orders_filled"] == 1


def test_return_later_than_limit_is_rejected():
    broker = make_broker(revival_max_return_bars=3)
    original = seed_order(broker)
    suspend(broker, original)
    for i in (2, 3, 4, 5):
        bar(broker, i)                                    # bearish throughout
    assert broker.stats["revival_rejected_too_late"] == 1
    broker.update_m5_trend(TrendDirection.BULLISH, "t5")
    bar(broker, 6)                                        # too late: no order
    assert revival_order(broker) is None
    assert broker.stats["revival_orders_created"] == 0


def test_dead_ob_is_never_revived():
    broker = make_broker()
    original = seed_order(broker)
    suspend(broker, original)
    broker.cancel_ob("ob1")
    assert broker.stats["revival_rejected_ob_invalid"] == 1
    broker.update_m5_trend(TrendDirection.BULLISH, "t2")
    bar(broker, 3)
    assert revival_order(broker) is None


def test_weak_choch_never_suspends_but_strong_choch_does():
    broker = make_broker()
    weak = seed_order(broker, break_kind="CHoCH", strong=False, ob_id="weak")
    suspend(broker, weak)
    assert broker.stats["revival_suspended"] == 0

    broker2 = make_broker()
    strong = seed_order(broker2, break_kind="CHoCH", strong=True, ob_id="strong")
    suspend(broker2, strong)
    assert broker2.stats["revival_suspended"] == 1


def test_max_one_revival_per_ob():
    broker = make_broker()  # revival_max_per_ob defaults to 1
    original = seed_order(broker)
    suspend(broker, original)
    broker.update_m5_trend(TrendDirection.BULLISH, "t1b")
    bar(broker, 2)
    first = revival_order(broker)
    assert first is not None and broker.revival_attempts["ob1"] == 1
    # The revival order itself dies to another flip; the attempt budget for
    # this OB is spent, so no second suspension record may be created.
    broker.update_m5_trend(TrendDirection.BEARISH, "t2b")
    assert first.active is False
    assert broker.stats["revival_orders_cancelled"] == 1
    assert broker.stats["revival_rejected_no_fresh_sweep"] == 1
    assert broker.stats["revival_rejected_attempt_limit"] == 1
    broker.update_m5_trend(TrendDirection.BULLISH, "t3b")
    bar(broker, 4)
    assert broker.stats["revival_orders_created"] == 1    # still just one


def test_no_fresh_sweep_optout_creates_preconfirmed_order():
    broker = make_broker(revival_require_fresh_sweep=False)
    original = seed_order(broker)
    suspend(broker, original)
    broker.update_m5_trend(TrendDirection.BULLISH, "t1b")
    bar(broker, 2)
    new = revival_order(broker)
    assert new is not None and new.meta.get("sweep_reclaim_confirmed")
    assert abs(new.stop - 94.8) < 1e-9                    # buffered at creation
    assert new.meta["revival_reason"] == "trend_returned_no_fresh_sweep_required"


def test_dump_load_preserves_attempts_and_records():
    broker = make_broker()
    original = seed_order(broker)
    suspend(broker, original)
    broker.update_m5_trend(TrendDirection.BULLISH, "t1b")
    bar(broker, 2)
    assert broker.revival_attempts == {"ob1": 1}
    state = broker.dump_state()

    restored = make_broker()
    restored.load_state(state)
    assert restored.revival_attempts == {"ob1": 1}
    assert any(rec["state"] == "order_created" for rec in restored.revival_records)
    assert revival_order(restored) is not None            # still pending, once


def test_config_mismatch_on_restore_cancels_experimental_state_keeps_attempts():
    broker = make_broker()
    original = seed_order(broker)
    suspend(broker, original)
    broker.update_m5_trend(TrendDirection.BULLISH, "t1b")
    bar(broker, 2)
    state = broker.dump_state()

    plain = PaperBroker(BotConfig(entry_mode="sweep_reclaim"), 10_000.0)
    plain.load_state(state)
    revived = [o for o in plain.pending if o.meta.get("is_revival")]
    assert revived and all(not o.active for o in revived)
    assert all(o.lifecycle_state == "cancelled_config_change" for o in revived)
    assert plain.revival_attempts == {"ob1": 1}           # never reset
    assert any(e["event_type"] == "experimental_state_cancelled"
               for e in plain.trend_events)


def test_shadow_audit_coexists_without_double_counting():
    broker = make_broker(revival_shadow_audit=True)
    original = seed_order(broker)
    suspend(broker, original)
    # One real record and one shadow record, counted in separate namespaces.
    assert broker.stats["revival_suspended"] == 1
    assert broker.stats["trend_suspended"] == 1
    assert len(broker.revival_records) == 1
    assert len(broker.revival_shadow) == 1
    broker.update_m5_trend(TrendDirection.BULLISH, "t1b")
    bar(broker, 2)
    assert broker.stats["revival_orders_created"] == 1
    assert broker.stats["reactivation_would_fill"] == 0   # shadow untouched by real


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
