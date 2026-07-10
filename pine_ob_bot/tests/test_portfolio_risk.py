"""Portfolio risk cap (--portfolio-risk-cap): projected total open risk may
never exceed the cap; reject-only (no automatic volume reduction).

Risk is always the entry-to-stop risk_money booked at entry as a fraction
of equity -- floating PnL never enters the projection.

Run with:  pytest pine_ob_bot/tests/test_portfolio_risk.py -v
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


def make_broker(cap, max_positions=2):
    cfg = BotConfig(entry_mode="sweep_reclaim", risk_fraction=0.005,
                    choch_risk_cap_fraction=None, max_open_positions=max_positions,
                    portfolio_risk_cap=cap)
    broker = PaperBroker(cfg, 10_000.0)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    broker.process_signal_candle(Candle("t1", 101.0, 102.0, 100.5, 101.5), 1)
    return broker


def confirmed_order(order_id, ob_id):
    """Entry ~100, stop 95: at 0.5% risk of 10k -> risk_money = $50 = 0.005."""
    return PendingOrder(order_id, ob_id, "bull", 100.0, 95.0, 107.5, 0, "t0",
                        True, {"break_kind": "BOS", "atr_at_formation": 1.0,
                               "sweep_reclaim_confirmed": True}, "armed")


FILL_TICK = Tick("tt1", 99.98, 100.0)                     # spread 0.02 < 0.1 cap


def test_cap_off_keeps_legacy_behaviour():
    broker = make_broker(cap=None)
    broker.pending.extend([confirmed_order("a", "ob_a"), confirmed_order("b", "ob_b")])
    broker.process_tick(FILL_TICK)
    assert len(broker.positions) == 2                     # legacy burst fill
    assert "open_risk_before_entry" not in broker.positions[0].meta
    assert broker.stats["rejected_portfolio_risk_cap"] == 0


def test_trade_below_cap_is_accepted_with_metadata():
    broker = make_broker(cap=0.02)
    broker.pending.extend([confirmed_order("a", "ob_a"), confirmed_order("b", "ob_b")])
    broker.process_tick(FILL_TICK)
    assert len(broker.positions) == 2
    first, second = broker.positions
    assert abs(first.meta["open_risk_before_entry"] - 0.0) < 1e-12
    assert abs(second.meta["open_risk_before_entry"] - 0.005) < 1e-9
    assert abs(second.meta["projected_open_risk_fraction"] - 0.01) < 1e-9
    assert abs(broker.stats["max_observed_open_risk_fraction"] - 0.01) < 1e-9


def test_trade_above_cap_is_rejected_not_downsized():
    broker = make_broker(cap=0.0075)
    broker.pending.extend([confirmed_order("a", "ob_a"), confirmed_order("b", "ob_b")])
    broker.process_tick(FILL_TICK)
    assert len(broker.positions) == 1                     # only the first fits
    rejected = next(o for o in broker.pending if o.id == "b")
    assert rejected.active is False
    assert rejected.lifecycle_state == "rejected_portfolio_risk"
    assert rejected.meta["rejection_reason"] == "PORTFOLIO_RISK_CAP_EXCEEDED"
    assert abs(rejected.meta["projected_open_risk_fraction"] - 0.01) < 1e-9
    assert rejected.meta["portfolio_risk_cap"] == 0.0075
    assert broker.stats["rejected_portfolio_risk_cap"] == 1
    assert broker.stats["setups_blocked_portfolio_risk"] == 1
    # Volume was never reduced to squeeze under the cap.
    assert broker.positions[0].volume == 0.1


def test_capacity_frees_after_position_closes():
    broker = make_broker(cap=0.0075)
    broker.pending.extend([confirmed_order("a", "ob_a"), confirmed_order("b", "ob_b")])
    broker.process_tick(FILL_TICK)
    assert len(broker.positions) == 1
    closed = broker.process_tick(Tick("tt2", 94.9, 94.95))  # stop out -> flat
    assert len(closed) == 1
    broker.pending.append(confirmed_order("c", "ob_c"))
    broker.process_tick(Tick("tt3", 99.98, 100.0))
    assert len(broker.positions) == 1                     # a fresh order fits again
    assert broker.positions[0].order_id == "c"
    assert broker.stats["rejected_portfolio_risk_cap"] == 1  # unchanged


def test_risk_is_entry_to_stop_money_not_floating_pnl():
    broker = make_broker(cap=0.02)
    broker.pending.extend([confirmed_order("a", "ob_a"), confirmed_order("b", "ob_b")])
    broker.process_tick(FILL_TICK)
    first, second = broker.positions
    # risk_money == |entry-stop| / tick_size * tick_value * volume, exactly.
    expected = abs(first.entry - first.stop) / 0.01 * 1.0 * first.volume
    assert abs(first.risk_money - expected) < 1e-9
    # The second fill's projection used the first's BOOKED risk fraction.
    assert abs(second.meta["open_risk_before_entry"]
               - first.risk_money / broker.equity) < 1e-12


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
