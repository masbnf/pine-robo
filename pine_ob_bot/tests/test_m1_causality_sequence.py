"""M1 Entry Assist: causality guards and sequence validation.

Run with:  pytest pine_ob_bot/tests/test_m1_causality_sequence.py -v
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


M1_NEUTRAL = (101.0, 101.4, 100.6, 101.2)     # above a 100/95 bull edge
M1_SWEEP_ONLY = (100.2, 100.3, 99.4, 99.7)    # wick + close below the edge
M1_SWEEP_RECLAIM = (100.2, 100.6, 99.4, 100.4)
M1_RECLAIM_ONLY = (100.2, 100.6, 100.1, 100.4)


def make_broker(mode="entry", **overrides):
    cfg = BotConfig(entry_mode="sweep_reclaim", m1_entry_assist=True,
                    m1_assist_mode=mode, **overrides)
    broker = PaperBroker(cfg, 10_000.0)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    return broker


def seed_setup(broker, order_id="o1", ob_id="ob1"):
    order = PendingOrder(order_id, ob_id, "bull", 100.0, 95.0, 107.5, 0, "t0",
                         True, {"break_kind": "BOS", "atr_at_formation": 1.0,
                                "ob_entry_price": 100.0, "ob_stop_price": 95.0},
                         "armed")
    broker.pending.append(order)
    broker.m1.register_setup(order)
    return order


def test_m1_closed_before_setup_exists_is_never_consumed():
    """The boundary rule: an M1 that closed inside (or at the close of) the
    M5 candle that CREATED the setup is processed before the setup exists,
    so it can never confirm it. Only later M1 bars are eligible."""
    broker = make_broker()
    # A perfect sweep+reclaim M1 closes BEFORE the setup is registered...
    broker.process_m1_candle(m1("t-1", *M1_SWEEP_RECLAIM))
    order = seed_setup(broker)
    assert order.meta["first_eligible_m1_index"] == 1     # only the NEXT bar
    assert not order.meta.get("sweep_reclaim_confirmed")
    assert broker.m1.setups["o1"]["state"] == "idle"
    # ...while the same pattern on the next (eligible) bar confirms.
    broker.process_m1_candle(m1("t1", *M1_SWEEP_RECLAIM))
    assert order.meta.get("sweep_reclaim_confirmed")
    assert broker.stats["m1_rejected_pre_setup_lookahead"] == 0


def test_confirmation_only_on_closed_bar_and_fill_on_next_tick():
    broker = make_broker()
    order = seed_setup(broker)
    broker.process_m1_candle(m1("t1", *M1_SWEEP_ONLY))
    assert broker.m1.setups["o1"]["state"] == "swept"
    assert not order.meta.get("sweep_reclaim_confirmed")  # nothing intra-bar
    assert broker.process_tick(Tick("tt1", 100.4, 100.45)) == []
    assert broker.positions == []                         # no fill pre-confirm
    broker.process_m1_candle(m1("t2", *M1_RECLAIM_ONLY))
    assert order.meta.get("sweep_reclaim_confirmed")
    assert order.meta["entry_trigger"] == "m1_sweep_reclaim"
    assert broker.positions == []                         # still no fill: no tick yet
    broker.process_tick(Tick("tt2", 100.5, 100.55))
    assert len(broker.positions) == 1                     # first tick AFTER close
    assert broker.positions[0].entry == 100.55
    assert broker.stats["m1_confirmations_filled"] == 1


def test_reclaim_before_sweep_is_never_an_m1_signal():
    broker = make_broker()
    order = seed_setup(broker)
    broker.process_m1_candle(m1("t1", *M1_RECLAIM_ONLY))  # reclaim-shaped, no sweep
    assert broker.m1.setups["o1"]["state"] == "idle"
    assert not order.meta.get("sweep_reclaim_confirmed")
    assert broker.stats["m1_reclaims_detected"] == 0


def test_m1_reclaim_window_expiry():
    broker = make_broker(m1_reclaim_max_bars=1)
    order = seed_setup(broker)
    broker.process_m1_candle(m1("t1", *M1_SWEEP_ONLY))
    # A bar that neither re-sweeps (low pinned AT the edge) nor reclaims
    # (close AT the edge): the sweep ages without renewal.
    broker.process_m1_candle(m1("t2", 100.3, 100.5, 100.0, 100.0))
    broker.process_m1_candle(m1("t3", *M1_RECLAIM_ONLY))
    # The sweep expired after 1 bar; the reclaim alone must not confirm.
    assert not order.meta.get("sweep_reclaim_confirmed")
    assert broker.m1.setups["o1"]["state"] == "idle"


def test_sequence_mode_vetoes_reclaim_before_sweep_m5_confirmation():
    broker = make_broker(mode="sequence")
    order = seed_setup(broker)
    # M1 evidence inside this M5 window: reclaim first, sweep afterwards.
    broker.process_m1_candle(m1("t1", *M1_RECLAIM_ONLY))
    broker.process_m1_candle(m1("t2", *M1_SWEEP_ONLY))
    # The M5 candle alone looks like a valid same-bar sweep+reclaim...
    broker.process_signal_candle(Candle("T1", 101.0, 101.5, 99.0, 101.0), 1)
    assert not order.meta.get("sweep_reclaim_confirmed")  # ...but is vetoed
    assert broker.stats["m1_sequence_invalid"] == 1
    assert order.meta["m1_sequence_status"] == "invalid_sequence"


def test_sequence_mode_passes_valid_ordering_and_never_creates_entries():
    broker = make_broker(mode="sequence")
    order = seed_setup(broker)
    broker.process_m1_candle(m1("t1", *M1_SWEEP_ONLY))
    broker.process_m1_candle(m1("t2", *M1_RECLAIM_ONLY))
    broker.process_signal_candle(Candle("T1", 101.0, 101.5, 99.0, 101.0), 1)
    assert order.meta.get("sweep_reclaim_confirmed")      # M5 path continues
    assert order.meta["m1_sequence_status"] == "valid_sequence"
    assert broker.stats["m1_sequence_valid"] == 1
    assert broker.stats["m1_confirmations_total"] == 0    # sequence creates nothing


def test_sequence_missing_data_and_gap_ambiguity_never_veto():
    broker = make_broker(mode="sequence")
    order = seed_setup(broker)
    broker.process_signal_candle(Candle("T1", 101.0, 101.5, 99.0, 101.0), 1)
    assert order.meta.get("sweep_reclaim_confirmed")      # no data -> no veto
    assert broker.stats["m1_sequence_missing_data"] == 1

    broker2 = make_broker(mode="sequence")
    order2 = seed_setup(broker2)
    broker2.process_m1_candle(m1("t1", *M1_SWEEP_ONLY))
    broker2.m1.note_window_gap()                          # empty minutes inside
    broker2.process_signal_candle(Candle("T1", 101.0, 101.5, 99.0, 101.0), 1)
    assert order2.meta.get("sweep_reclaim_confirmed")     # ambiguous -> no veto
    assert broker2.stats["m1_sequence_ambiguous"] == 1


def test_shadow_mode_records_sequence_but_never_vetoes():
    broker = make_broker(mode="shadow")
    order = seed_setup(broker)
    broker.process_m1_candle(m1("t1", *M1_RECLAIM_ONLY))
    broker.process_m1_candle(m1("t2", *M1_SWEEP_ONLY))
    broker.process_signal_candle(Candle("T1", 101.0, 101.5, 99.0, 101.0), 1)
    assert order.meta.get("sweep_reclaim_confirmed")      # shadow never changes real
    assert broker.stats["m1_sequence_invalid"] == 1       # ...but still classifies


def test_sequence_window_resets_at_m5_close():
    broker = make_broker(mode="sequence")
    order = seed_setup(broker)
    broker.process_m1_candle(m1("t1", *M1_RECLAIM_ONLY))  # would be invalid...
    broker.process_signal_candle(Candle("T1", 101.0, 101.5, 100.4, 101.2), 1)  # no confirm
    # New M5 window: valid ordering now.
    broker.process_m1_candle(m1("t2", *M1_SWEEP_ONLY))
    broker.process_m1_candle(m1("t3", *M1_RECLAIM_ONLY))
    broker.process_signal_candle(Candle("T2", 101.0, 101.5, 99.0, 101.0), 2)
    assert order.meta.get("sweep_reclaim_confirmed")
    assert broker.stats["m1_sequence_invalid"] == 0


def test_trend_change_and_ob_invalidation_cancel_m1_state():
    broker = make_broker()
    order = seed_setup(broker)
    broker.process_m1_candle(m1("t1", *M1_SWEEP_ONLY))
    broker.update_m5_trend(TrendDirection.BEARISH, "t1x")
    assert broker.m1.setups["o1"]["state"] == "cancelled_trend"
    assert broker.stats["m1_cancelled_trend_change"] == 1

    broker2 = make_broker()
    order2 = seed_setup(broker2)
    broker2.process_m1_candle(m1("t1", *M1_SWEEP_ONLY))
    broker2.cancel_ob("ob1")
    assert broker2.m1.setups["o1"]["state"] == "cancelled_ob_invalid"
    assert broker2.stats["m1_cancelled_ob_invalid"] == 1
    # A dead OB can never confirm afterwards.
    broker2.process_m1_candle(m1("t2", *M1_RECLAIM_ONLY))
    assert not order2.meta.get("sweep_reclaim_confirmed")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
