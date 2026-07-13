"""Controlled Re-entry (--allow-ob-reentry): one additional, fully
re-validated entry per Order Block after the previous trade on it closed.

Run with:  pytest pine_ob_bot/tests/test_ob_reentry.py -v
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


NEUTRAL = (101.0, 102.0, 100.5, 101.5)
SWEEP_RECLAIM = (101.0, 101.5, 99.0, 101.0)
RECLAIM_ONLY = (100.3, 101.5, 100.2, 101.0)


def make_broker(**overrides):
    cfg = BotConfig(entry_mode="sweep_reclaim", allow_ob_reentry=True, **overrides)
    return PaperBroker(cfg, 10_000.0)


def seed_confirmed_order(broker, ob_id="ob1"):
    """A first-entry order that is already sweep-confirmed (fills next tick)."""
    order = PendingOrder("first", ob_id, "bull", 100.0, 95.0, 107.5, 0, "t0",
                         True, {"break_kind": "BOS", "atr_at_formation": 1.0,
                                "ob_entry_price": 100.0, "ob_stop_price": 95.0,
                                "sweep_reclaim_confirmed": True}, "armed")
    broker.pending.append(order)
    return order


def bar(broker, index, shape=NEUTRAL):
    broker.process_signal_candle(candle(f"t{index}", *shape), index)


def open_and_close_first_trade(broker, result="loss"):
    """Fill the seeded order on a tick, then close it (loss by default)."""
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    bar(broker, 1)                                        # market_index = 1
    assert broker.process_tick(Tick("tt1", 100.4, 100.45)) == []
    assert len(broker.positions) == 1
    # The sweep_reclaim fill recomputes the target from the FILL price
    # (100.45 + 1.5 * 5.45 = 108.625), so the win tick must clear that.
    exit_tick = (Tick("tt2", 94.9, 94.95) if result == "loss"
                 else Tick("tt2", 108.7, 108.75))
    trades = broker.process_tick(exit_tick)
    assert len(trades) == 1 and trades[0].result == result
    return trades[0]


def reentry_order(broker):
    return next((o for o in broker.pending if o.meta.get("is_reentry")), None)


def test_flag_off_keeps_legacy_behaviour():
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim"), 10_000.0)
    seed_confirmed_order(broker)
    open_and_close_first_trade(broker)
    bar(broker, 2)
    assert broker.ob_entry_history == {}
    assert broker.stats["reentry_candidates"] == 0
    assert reentry_order(broker) is None


def test_second_entry_needs_completely_fresh_sweep():
    broker = make_broker()
    seed_confirmed_order(broker)
    open_and_close_first_trade(broker)                    # exit at bar index 1
    assert broker.stats["reentry_candidates"] == 1
    bar(broker, 2)                                        # wait 1 >= 1: order created
    new = reentry_order(broker)
    assert new is not None and new.id != "first"
    assert new.meta["ob_entry_attempt"] == 2
    assert new.meta["reentry_parent_trade_id"]
    assert new.meta["reentry_wait_bars"] == 1
    assert new.meta["previous_entry_result_r"] < 0
    assert not new.meta.get("sweep_reclaim_confirmed")    # first sweep NOT reused
    bar(broker, 3, RECLAIM_ONLY)                          # reclaim without new sweep
    assert not new.meta.get("sweep_reclaim_confirmed")
    bar(broker, 4, SWEEP_RECLAIM)                         # fresh sweep + reclaim
    assert new.meta.get("sweep_reclaim_confirmed")
    broker.process_tick(Tick("tt3", 100.9, 100.95))
    assert len(broker.positions) == 1
    assert broker.positions[0].meta["is_reentry"] is True
    assert broker.positions[0].meta["reentry_fresh_sweep_index"] == 4
    assert broker.stats["reentry_orders_filled"] == 1


def test_third_entry_is_blocked_by_attempt_cap():
    broker = make_broker()                                # max_entries_per_ob = 2
    seed_confirmed_order(broker)
    open_and_close_first_trade(broker)
    bar(broker, 2)
    new = reentry_order(broker)
    bar(broker, 3, SWEEP_RECLAIM)
    broker.process_tick(Tick("tt3", 100.9, 100.95))       # attempt 2 filled
    trades = broker.process_tick(Tick("tt4", 94.5, 94.55))  # closes as loss
    assert len(trades) == 1
    assert broker.stats["reentry_rejected_attempt_limit"] == 1
    bar(broker, 4)
    third = [o for o in broker.pending
             if o.meta.get("is_reentry") and o.id != new.id]
    assert third == []
    assert broker.stats["reentry_orders_created"] == 1


def test_min_wait_bars_is_respected():
    broker = make_broker(min_reentry_wait_bars=3)
    seed_confirmed_order(broker)
    open_and_close_first_trade(broker)                    # exit index 1
    bar(broker, 2)
    bar(broker, 3)
    assert reentry_order(broker) is None
    assert broker.stats["reentry_rejected_wait"] == 1     # counted once, not per bar
    bar(broker, 4)                                        # wait 3 >= 3
    assert reentry_order(broker) is not None


def test_invalid_ob_blocks_reentry():
    broker = make_broker()
    seed_confirmed_order(broker)
    open_and_close_first_trade(broker)
    broker.cancel_ob("ob1")
    bar(broker, 2)
    assert reentry_order(broker) is None
    assert broker.stats["reentry_rejected_ob_invalid"] == 1


def test_opposite_trend_blocks_reentry():
    broker = make_broker()
    seed_confirmed_order(broker)
    open_and_close_first_trade(broker)
    broker.update_m5_trend(TrendDirection.BEARISH, "t1b")
    bar(broker, 2)
    assert reentry_order(broker) is None
    assert broker.stats["reentry_rejected_trend"] == 1


def test_loss_only_mode_skips_reentry_after_win():
    broker = make_broker(reentry_after_loss_only=True)
    seed_confirmed_order(broker)
    open_and_close_first_trade(broker, result="win")
    bar(broker, 2)
    assert broker.stats["reentry_candidates"] == 0
    assert reentry_order(broker) is None


def test_weak_choch_never_reenters():
    broker = make_broker()
    order = seed_confirmed_order(broker)
    order.meta["break_kind"] = "CHoCH"                    # weak: no displacement flag
    open_and_close_first_trade(broker)
    bar(broker, 2)
    assert broker.stats["reentry_candidates"] == 0
    assert reentry_order(broker) is None


def test_dump_load_preserves_attempt_count():
    broker = make_broker()
    seed_confirmed_order(broker)
    open_and_close_first_trade(broker)
    bar(broker, 2)                                        # attempt 2 order created
    assert broker.ob_entry_history["ob1"]["attempts"] == 2
    state = broker.dump_state()

    restored = make_broker()
    restored.load_state(state)
    assert restored.ob_entry_history["ob1"]["attempts"] == 2
    # A restart must not mint a duplicate re-entry order for the same OB.
    bar(restored, 3)
    reentries = [o for o in restored.pending if o.meta.get("is_reentry")]
    assert len(reentries) == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
