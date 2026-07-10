"""M1 Entry Assist, shadow mode: strictly observe-only.

Run with:  pytest pine_ob_bot/tests/test_m1_shadow.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.m1 import M1Candle
    from pine_ob_bot.models import PendingOrder, Tick
    from pine_ob_bot.paper import PaperBroker
    from pine_ob_bot.trend_filter import TrendDirection
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from m1 import M1Candle
    from models import PendingOrder, Tick
    from paper import PaperBroker
    from trend_filter import TrendDirection


def m1(t, o, h, l, c):
    spread = 0.05
    return M1Candle(time=t, open=o, high=h, low=l, close=c,
                    bid_open=o, bid_high=h, bid_low=l, bid_close=c,
                    ask_open=o + spread, ask_high=h + spread, ask_low=l + spread,
                    ask_close=c + spread, spread_open=spread, spread_high=spread,
                    spread_low=spread, spread_close=spread, spread_average=spread,
                    tick_count=10)


SWEEP_RECLAIM = (100.2, 100.6, 99.4, 100.4)


def make_broker():
    cfg = BotConfig(entry_mode="sweep_reclaim", m1_entry_assist=True,
                    m1_assist_mode="shadow")
    broker = PaperBroker(cfg, 10_000.0)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    return broker


def seed(broker, order_id="o1", ob_id="ob1"):
    order = PendingOrder(order_id, ob_id, "bull", 100.0, 95.0, 107.5, 0, "t0",
                         True, {"break_kind": "BOS", "atr_at_formation": 1.0,
                                "ob_entry_price": 100.0, "ob_stop_price": 95.0},
                         "armed")
    broker.pending.append(order)
    broker.m1.register_setup(order)
    return order


def test_shadow_never_touches_real_state():
    broker = make_broker()
    order = seed(broker)
    equity_before = broker.equity
    broker.process_m1_candle(m1("t1", *SWEEP_RECLAIM))
    assert broker.stats["m1_confirmations_shadow"] == 1
    assert len(broker.m1.shadow_positions) == 1
    # Real order untouched: no confirmation meta, original stop, still armed.
    assert not order.meta.get("sweep_reclaim_confirmed")
    assert order.stop == 95.0 and order.active and order.lifecycle_state == "armed"
    # A tick fills nothing: no real or paper position, no equity change.
    broker.process_tick(Tick("tt1", 100.5, 100.55))
    assert broker.positions == [] and broker.trades == []
    assert broker.equity == equity_before
    assert broker.stats["m1_confirmations_filled"] == 0


def test_shadow_result_recorded_without_equity_impact():
    broker = make_broker()
    seed(broker)
    broker.process_m1_candle(m1("t1", *SWEEP_RECLAIM))
    shadow = broker.m1.shadow_positions[0]
    assert shadow["entry"] == 100.45                      # ask close at confirm
    # Walk M1 bars to the shadow target (fill 100.45, stop 94.8 -> RR 1.5
    # target = 100.45 + 1.5 * 5.65 = 108.925).
    broker.process_m1_candle(m1("t2", 100.5, 109.2, 100.4, 109.0))
    assert broker.stats["m1_shadow_wins"] == 1
    assert abs(broker.stats["m1_shadow_total_r"] - 1.5) < 1e-9
    assert broker.equity == 10_000.0
    event = broker.m1.shadow_events[0]
    for key in ("shadow_entry", "shadow_stop", "shadow_target", "shadow_fill_time",
                "shadow_exit_time", "shadow_result_r",
                "shadow_max_favorable_excursion_r",
                "shadow_max_adverse_excursion_r", "shadow_break_kind",
                "shadow_m5_setup_id", "shadow_m1_sweep_index",
                "shadow_m1_reclaim_index"):
        assert key in event


def test_shadow_never_occupies_position_limit():
    broker = make_broker()
    seed(broker, order_id="o1", ob_id="ob1")
    broker.process_m1_candle(m1("t1", *SWEEP_RECLAIM))    # shadow "open"
    # A real M5-confirmed order still fills: shadow holds no slot.
    real = PendingOrder("real", "ob2", "bull", 100.0, 95.0, 107.5, 0, "t0", True,
                        {"break_kind": "BOS", "atr_at_formation": 1.0,
                         "sweep_reclaim_confirmed": True}, "armed")
    broker.pending.append(real)
    broker.process_tick(Tick("tt1", 100.5, 100.55))
    assert len(broker.positions) == 1
    assert broker.positions[0].order_id == "real"


def test_shadow_consumption_books_are_separate():
    broker = make_broker()
    seed(broker)
    broker.process_m1_candle(m1("t1", *SWEEP_RECLAIM))
    assert broker.m1.shadow_used_event_ids                # shadow book written
    assert broker.m1.used_event_ids == set()              # real book untouched
    assert broker.m1.ob_confirmations == {}


def test_shadow_state_survives_dump_load_and_never_becomes_real():
    broker = make_broker()
    seed(broker)
    broker.process_m1_candle(m1("t1", *SWEEP_RECLAIM))
    state = broker.dump_state()

    restored = PaperBroker(BotConfig(entry_mode="sweep_reclaim",
                                     m1_entry_assist=True,
                                     m1_assist_mode="shadow"), 10_000.0)
    restored.load_state(state)
    assert len(restored.m1.shadow_positions) == 1
    restored.update_m5_trend(TrendDirection.BULLISH, "t0r")
    restored.process_tick(Tick("tt1", 100.5, 100.55))
    assert restored.positions == []                       # still shadow-only


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
