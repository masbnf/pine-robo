"""Spread Wait After Confirmation (--wait-for-spread-after-confirmation).

Legacy (flag off): a spread-blocked confirmed order silently retries on
every subsequent tick, forever. Flag on: an explicit waiting_spread state
bounded by seconds/ticks/bars, cancelled deterministically on timeout,
trend change or OB invalidation, with unique-order counting and fill-time
repricing/re-sizing.

Run with:  pytest pine_ob_bot/tests/test_spread_wait.py -v
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


WIDE = ("2024-01-01T00:00:00+00:00", 100.4, 100.9)   # spread 0.5 > 0.1 ATR cap
OK = ("2024-01-01T00:00:20+00:00", 100.45, 100.5)    # spread 0.05


def make_broker(equity=10_000.0, **overrides):
    overrides.setdefault("wait_for_spread_after_confirmation", True)
    overrides.setdefault("spread_wait_max_ticks", 3)
    cfg = BotConfig(entry_mode="sweep_reclaim", **overrides)
    return PaperBroker(cfg, equity)


def seed_confirmed(broker):
    order = PendingOrder("o1", "ob1", "bull", 100.0, 95.0, 107.5, 0, "t0", True,
                         {"break_kind": "BOS", "atr_at_formation": 1.0,
                          "sweep_reclaim_confirmed": True}, "armed")
    broker.pending.append(order)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    broker.process_signal_candle(
        Candle("t1", 101.0, 102.0, 100.5, 101.5), 1)     # market_index = 1
    return order


def tick(stamp_bid_ask):
    return Tick(*stamp_bid_ask)


def test_flag_off_keeps_legacy_unbounded_retry():
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim"), 10_000.0)
    order = seed_confirmed(broker)
    for _ in range(10):
        broker.process_tick(tick(WIDE))
    assert order.active is True                           # still retrying, forever
    assert broker.stats["spread_wait_started"] == 0
    assert "spread_wait_active" not in order.meta


def test_bad_then_good_spread_fills_at_current_price_and_counts_once():
    broker = make_broker()
    order = seed_confirmed(broker)
    broker.process_tick(tick(WIDE))
    broker.process_tick(tick(("2024-01-01T00:00:10+00:00", 100.4, 100.9)))
    assert broker.stats["spread_wait_started"] == 1       # unique order, not per tick
    assert broker.stats["unique_orders_blocked_by_spread"] == 1
    assert broker.stats["spread_entry_blocked"] == 2      # legacy per-tick counter
    broker.process_tick(tick(OK))
    assert len(broker.positions) == 1
    position = broker.positions[0]
    assert position.entry == 100.5                        # price at fill time, not confirmation
    assert position.meta["spread_waited"] is True
    assert position.meta["spread_at_confirmation"] == 0.5
    assert abs(position.meta["spread_at_fill"] - 0.05) < 1e-9
    assert position.meta["spread_wait_seconds"] == 20.0
    assert broker.stats["spread_wait_eventually_filled"] == 1
    assert broker.stats["spread_wait_duration_ticks_sum"] == 2


def test_tick_timeout_expires_the_order():
    broker = make_broker(spread_wait_max_ticks=3)
    order = seed_confirmed(broker)
    for i in range(4):
        broker.process_tick(tick((f"2024-01-01T00:00:{i:02d}+00:00", 100.4, 100.9)))
    assert order.active is False
    assert order.lifecycle_state == "spread_wait_expired"
    assert broker.stats["spread_wait_expired"] == 1


def test_seconds_timeout_expires_the_order():
    broker = make_broker(spread_wait_max_ticks=None, spread_wait_max_seconds=30.0)
    order = seed_confirmed(broker)
    broker.process_tick(tick(("2024-01-01T00:00:00+00:00", 100.4, 100.9)))
    broker.process_tick(tick(("2024-01-01T00:00:31+00:00", 100.4, 100.9)))
    assert order.active is False
    assert order.lifecycle_state == "spread_wait_expired"
    assert broker.stats["spread_wait_max_duration_seconds"] == 31.0


def test_trend_change_cancels_the_wait():
    broker = make_broker()
    order = seed_confirmed(broker)
    broker.process_tick(tick(WIDE))
    broker.update_m5_trend(TrendDirection.BEARISH, "t2")
    assert order.active is False
    assert broker.stats["spread_wait_cancelled_trend"] == 1


def test_ob_invalidation_cancels_the_wait():
    broker = make_broker()
    order = seed_confirmed(broker)
    broker.process_tick(tick(WIDE))
    broker.cancel_ob("ob1")
    assert order.active is False
    assert broker.stats["spread_wait_cancelled_ob_invalid"] == 1


def test_resizing_at_fill_rejects_when_sizing_became_invalid():
    # Equity so small the rounded volume falls below the broker minimum at
    # the (re-priced) fill -- the wait must end in a sizing rejection.
    broker = make_broker(equity=10.0)
    order = seed_confirmed(broker)
    broker.process_tick(tick(WIDE))
    broker.process_tick(tick(OK))
    assert order.active is False
    assert order.lifecycle_state == "rejected_sizing"
    assert broker.stats["spread_wait_rejected_sizing"] == 1
    assert broker.positions == []


def test_wait_state_survives_dump_load_and_timeout_continues():
    broker = make_broker(spread_wait_max_ticks=3)
    seed_confirmed(broker)
    broker.process_tick(tick(WIDE))
    broker.process_tick(tick(("2024-01-01T00:00:05+00:00", 100.4, 100.9)))
    state = broker.dump_state()

    restored = make_broker(spread_wait_max_ticks=3)
    restored.load_state(state)
    restored.update_m5_trend(TrendDirection.BULLISH, "t0r")
    pending = next(o for o in restored.pending if o.active)
    assert pending.meta["spread_wait_ticks"] == 2         # attempts NOT reset
    restored.process_tick(tick(("2024-01-01T00:00:12+00:00", 100.4, 100.9)))
    restored.process_tick(tick(("2024-01-01T00:00:14+00:00", 100.4, 100.9)))
    assert pending.active is False                        # 4th blocked tick overall
    assert pending.lifecycle_state == "spread_wait_expired"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
