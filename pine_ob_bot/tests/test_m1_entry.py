"""M1 Entry Assist, entry mode: gates, fills, duplicates, M5 fallback.

Run with:  pytest pine_ob_bot/tests/test_m1_entry.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.m1 import M1Candle
    from pine_ob_bot.models import Candle, PendingOrder, Tick
    from pine_ob_bot.paper import PaperBroker
    from pine_ob_bot.trend_filter import TrendDirection
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from m1 import M1Candle
    from models import Candle, PendingOrder, Tick
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


BULL_SWEEP_RECLAIM = (100.2, 100.6, 99.4, 100.4)
BEAR_SWEEP_RECLAIM = (99.8, 100.6, 99.4, 99.6)


def make_broker(direction="bull", equity=10_000.0, **overrides):
    cfg = BotConfig(entry_mode="sweep_reclaim", m1_entry_assist=True,
                    m1_assist_mode="entry", **overrides)
    broker = PaperBroker(cfg, equity)
    broker.update_m5_trend(TrendDirection.BULLISH if direction == "bull"
                           else TrendDirection.BEARISH, "t0")
    return broker


def seed(broker, direction="bull", order_id="o1", ob_id="ob1",
         break_kind="BOS", strong=False):
    if direction == "bull":
        entry, stop, target = 100.0, 95.0, 107.5
    else:
        entry, stop, target = 100.0, 105.0, 92.5
    meta = {"break_kind": break_kind, "atr_at_formation": 1.0,
            "ob_entry_price": entry, "ob_stop_price": stop}
    if strong:
        meta["bos_displacement_top_quartile"] = True
    order = PendingOrder(order_id, ob_id, direction, entry, stop, target,
                         0, "t0", True, meta, "armed")
    broker.pending.append(order)
    broker.m1.register_setup(order)
    return order


def test_bull_entry_full_path_with_metadata():
    broker = make_broker()
    order = seed(broker)
    broker.process_m1_candle(m1("2026-04-10T10:04:00+00:00", *BULL_SWEEP_RECLAIM))
    assert order.meta["entry_timeframe"] == "M1"
    assert order.meta["entry_trigger"] == "m1_sweep_reclaim"
    assert order.meta["m1_reclaim_lag_bars"] == 0
    assert order.meta["m1_event_id"]
    assert abs(order.stop - 94.8) < 1e-9                  # M5 rule: 95 - 0.20 ATR
    broker.process_tick(Tick("2026-04-10T10:04:01+00:00", 100.5, 100.55))
    assert len(broker.positions) == 1
    position = broker.positions[0]
    assert position.meta["m1_fill_time"] == "2026-04-10T10:04:01+00:00"
    assert position.meta["m1_spread_at_confirmation"] == 0.05
    assert broker.stats["m1_entries_bos"] == 1
    trades = broker.process_tick(Tick("2026-04-10T10:05:00+00:00", 94.7, 94.75))
    assert len(trades) == 1
    assert broker.stats["m1_losses"] == 1


def test_bear_entry_mirrors_bull():
    broker = make_broker("bear")
    order = seed(broker, "bear")
    broker.process_m1_candle(m1("t1", *BEAR_SWEEP_RECLAIM))
    assert order.meta.get("sweep_reclaim_confirmed")
    assert abs(order.stop - 105.2) < 1e-9                 # 105 + 0.20 ATR
    broker.process_tick(Tick("tt1", 99.5, 99.55))
    assert len(broker.positions) == 1
    assert broker.positions[0].direction == "bear"


def test_strong_choch_allowed_weak_choch_rejected():
    broker = make_broker()
    strong = seed(broker, order_id="s", ob_id="ob_s", break_kind="CHoCH", strong=True)
    weak = seed(broker, order_id="w", ob_id="ob_w", break_kind="CHoCH", strong=False)
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))
    assert strong.meta.get("sweep_reclaim_confirmed")
    assert not weak.meta.get("sweep_reclaim_confirmed")
    assert broker.stats["m1_rejected_break_kind"] == 1
    broker.process_tick(Tick("tt1", 100.5, 100.55))
    assert broker.stats["m1_entries_strong_choch"] == 1


def test_opposite_trend_blocks_confirmation():
    broker = make_broker()
    order = seed(broker)
    broker.current_m5_trend = TrendDirection.BEARISH      # flips before the M1 close
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))
    assert not order.meta.get("sweep_reclaim_confirmed")
    assert broker.stats["m1_rejected_trend_mismatch"] == 1


def test_position_limit_defers_fill_not_confirmation():
    broker = make_broker()
    seed(broker, order_id="o1", ob_id="ob1")
    seed(broker, order_id="o2", ob_id="ob2")
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))   # both confirm
    broker.process_tick(Tick("tt1", 100.5, 100.55))
    assert len(broker.positions) == 1                     # max_open_positions=1
    assert broker.stats["m1_rejected_position_limit"] >= 1


def test_spread_rejection_uses_normal_pipeline():
    broker = make_broker()
    order = seed(broker)
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))
    broker.process_tick(Tick("tt1", 100.0, 100.5))        # spread 0.5 > 0.1 ATR cap
    assert broker.positions == []
    assert order.active is True                           # blocked, not destroyed
    assert broker.stats["m1_rejected_spread"] == 1
    broker.process_tick(Tick("tt2", 100.5, 100.55))       # spread OK now
    assert len(broker.positions) == 1


def test_sizing_rejection_counts_and_cancels():
    broker = make_broker(equity=10.0)                     # volume under broker min
    order = seed(broker)
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))
    broker.process_tick(Tick("tt1", 100.5, 100.55))
    assert broker.positions == []
    assert order.lifecycle_state == "rejected_sizing"
    assert broker.stats["m1_rejected_sizing"] == 1
    assert broker.m1.setups["o1"]["state"] == "cancelled_risk"


def test_duplicate_m1_event_blocked_and_cap_respected():
    broker = make_broker(m1_entry_expiry_bars=1, m1_max_confirmations_per_ob=1)
    order = seed(broker)
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))  # confirm #1
    broker.process_m1_candle(m1("t2", 100.4, 100.9, 100.3, 100.8))  # expiry bar
    assert order.meta.get("sweep_reclaim_confirmed") is None  # expired, reverted
    broker.process_m1_candle(m1("t3", *BULL_SWEEP_RECLAIM))  # would confirm again
    assert not order.meta.get("sweep_reclaim_confirmed")
    assert broker.stats["m1_rejected_confirmation_cap"] == 1  # one per OB


def test_expiry_restores_clean_m5_fallback():
    broker = make_broker(m1_entry_expiry_bars=2)
    order = seed(broker)
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))
    assert abs(order.stop - 94.8) < 1e-9
    broker.process_m1_candle(m1("t2", 100.4, 100.9, 100.3, 100.8))
    broker.process_m1_candle(m1("t3", 100.4, 100.9, 100.3, 100.8))
    assert order.stop == 95.0                             # stop restored
    assert "sweep_reclaim_confirmed" not in order.meta
    assert "entry_trigger" not in order.meta
    assert broker.stats["m1_confirmations_expired"] == 1
    # The untouched M5 path can still confirm and fill this setup.
    broker.process_signal_candle(Candle("T1", 101.0, 101.5, 99.0, 101.0), 1)
    assert order.meta.get("sweep_reclaim_confirmed")
    assert order.meta["entry_trigger"] == "m5_sweep_reclaim"
    broker.process_tick(Tick("tt1", 100.5, 100.55))
    assert len(broker.positions) == 1
    assert broker.positions[0].meta["entry_timeframe"] == "M5"


def test_m1_fill_consumes_setup_no_m5_duplicate():
    broker = make_broker()
    order = seed(broker)
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))
    broker.process_tick(Tick("tt1", 100.5, 100.55))
    assert len(broker.positions) == 1
    assert order.lifecycle_state == "filled"
    # A later perfect M5 sweep/reclaim cannot resurrect the filled order.
    broker.process_signal_candle(Candle("T1", 101.0, 101.5, 99.0, 101.0), 1)
    broker.process_tick(Tick("tt2", 100.6, 100.65))
    assert len(broker.positions) == 1                     # still exactly one


def test_refine_stop_uses_m1_sweep_extreme_when_flagged():
    broker = make_broker(m1_refine_stop=True, m1_stop_atr_buffer=0.10)
    order = seed(broker)
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))   # sweep low = 99.4
    assert order.meta["m1_stop_refined"] is True
    assert abs(order.stop - 99.3) < 1e-9                  # 99.4 - 0.10 * ATR(1.0)


def test_refined_stop_recorded_tighter_or_wider_than_m5():
    # Tighter than the M5 stop (94.8): sweep extreme 99.4 - 0.1 = 99.3.
    broker = make_broker(m1_refine_stop=True, m1_stop_atr_buffer=0.10)
    order = seed(broker)
    broker.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))
    assert order.meta["m1_stop_refined"] is True
    assert order.stop > 94.8                              # tighter than M5 rule
    # Wider: a huge buffer puts the refined stop below the M5 stop.
    broker2 = make_broker(m1_refine_stop=True, m1_stop_atr_buffer=6.0)
    order2 = seed(broker2)
    broker2.process_m1_candle(m1("t1", *BULL_SWEEP_RECLAIM))
    assert order2.meta["m1_stop_refined"] is True
    assert order2.stop < 94.8                             # wider than M5 rule


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
