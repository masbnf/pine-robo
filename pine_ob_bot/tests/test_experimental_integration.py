"""Cross-feature integration of the experimental trade-frequency flags.

Covers the interaction matrix the spec requires: multi-bar window with
revival/re-entry, revival with spread wait, re-entry with the portfolio
cap, burst fills under max-positions+cap, Revival > Re-entry priority, and
an all-flags-on end-to-end historical run (the all-flags-OFF equivalence is
test_legacy_regression.py).

Run with:  pytest pine_ob_bot/tests/test_experimental_integration.py -v
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

FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(FIXTURES))
import gen_legacy_fixture as gen  # noqa: E402


def candle(t, o, h, l, c):
    return Candle(time=t, open=o, high=h, low=l, close=c)


NEUTRAL = (101.0, 102.0, 100.5, 101.5)
SWEEP_NO_RECLAIM = (99.6, 99.9, 99.0, 99.5)   # wick + close below the edge
RECLAIM_ONLY = (100.3, 101.5, 100.2, 101.0)


def seed(broker, order_id="orig", ob_id="ob1", confirmed=False):
    meta = {"break_kind": "BOS", "atr_at_formation": 1.0,
            "ob_entry_price": 100.0, "ob_stop_price": 95.0}
    if confirmed:
        meta["sweep_reclaim_confirmed"] = True
    order = PendingOrder(order_id, ob_id, "bull", 100.0, 95.0, 107.5, 0, "t0",
                         True, meta, "armed")
    broker.pending.append(order)
    return order


def bar(broker, index, shape=NEUTRAL):
    broker.process_signal_candle(candle(f"t{index}", *shape), index)


def test_multibar_window_applies_to_revival_orders():
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim",
                                   controlled_revival=True,
                                   sweep_reclaim_max_bars=2), 10_000.0)
    original = seed(broker)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    bar(broker, 1)
    broker.update_m5_trend(TrendDirection.BEARISH, "t1")
    broker.update_m5_trend(TrendDirection.BULLISH, "t1b")
    bar(broker, 2)                                        # revival order created
    revival = next(o for o in broker.pending if o.meta.get("is_revival"))
    bar(broker, 3, SWEEP_NO_RECLAIM)                      # fresh sweep, no reclaim yet
    bar(broker, 4, RECLAIM_ONLY)                          # lag-1 reclaim inside window 2
    assert revival.meta.get("sweep_reclaim_confirmed")
    assert revival.meta["sweep_reclaim_lag_bars"] == 1
    assert broker.stats["sweep_reclaim_lag1_confirms"] == 1


def test_multibar_window_applies_to_reentry_orders():
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim",
                                   allow_ob_reentry=True,
                                   sweep_reclaim_max_bars=2), 10_000.0)
    seed(broker, confirmed=True)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    bar(broker, 1)
    broker.process_tick(Tick("tt1", 100.4, 100.45))       # first entry fills
    broker.process_tick(Tick("tt2", 94.9, 94.95))         # first entry stops out
    bar(broker, 2)                                        # re-entry order created
    reentry = next(o for o in broker.pending if o.meta.get("is_reentry"))
    bar(broker, 3, SWEEP_NO_RECLAIM)
    bar(broker, 4, RECLAIM_ONLY)                          # lag-1 confirm
    assert reentry.meta.get("sweep_reclaim_confirmed")
    assert reentry.meta["sweep_reclaim_lag_bars"] == 1


def test_revival_plus_spread_wait_fills_after_wait():
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim",
                                   controlled_revival=True,
                                   wait_for_spread_after_confirmation=True,
                                   spread_wait_max_ticks=5), 10_000.0)
    seed(broker)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    bar(broker, 1)
    broker.update_m5_trend(TrendDirection.BEARISH, "t1")
    broker.update_m5_trend(TrendDirection.BULLISH, "t1b")
    bar(broker, 2)
    bar(broker, 3, (101.0, 101.5, 99.0, 101.0))           # fresh sweep + reclaim
    revival = next(o for o in broker.pending if o.meta.get("is_revival"))
    assert revival.meta.get("sweep_reclaim_confirmed")
    broker.process_tick(Tick("2024-01-01T00:00:00+00:00", 100.4, 100.9))  # blocked
    assert broker.stats["spread_wait_started"] == 1
    broker.process_tick(Tick("2024-01-01T00:00:05+00:00", 100.45, 100.5))
    assert len(broker.positions) == 1
    position = broker.positions[0]
    assert position.meta["is_revival"] is True
    assert position.meta["spread_waited"] is True
    assert broker.stats["revival_orders_filled"] == 1
    assert broker.stats["spread_wait_eventually_filled"] == 1


def test_reentry_plus_portfolio_cap_rejects_the_reentry_fill():
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim",
                                   allow_ob_reentry=True, risk_fraction=0.005,
                                   choch_risk_cap_fraction=None,
                                   max_open_positions=2,
                                   portfolio_risk_cap=0.0075), 10_000.0)
    seed(broker, "first", "ob1", confirmed=True)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    bar(broker, 1)
    broker.process_tick(Tick("tt1", 99.98, 100.0))        # entry 1 fills
    broker.process_tick(Tick("tt2", 94.9, 94.95))         # entry 1 stops out
    bar(broker, 2)                                        # re-entry order created
    reentry = next(o for o in broker.pending if o.meta.get("is_reentry"))
    bar(broker, 3, (101.0, 101.5, 99.0, 101.0))           # fresh sweep + reclaim
    assert reentry.meta.get("sweep_reclaim_confirmed")
    # Another independent confirmed setup fills first on the same tick and
    # consumes the cap; the re-entry projection then exceeds it.
    seed(broker, "other", "ob2", confirmed=True)
    broker.pending.sort(key=lambda x: (x.created_index, x.created_time))
    broker.process_tick(Tick("tt3", 99.98, 100.0))
    assert len(broker.positions) == 1
    assert broker.positions[0].order_id == "other"
    assert reentry.active is False
    assert reentry.lifecycle_state == "rejected_portfolio_risk"
    assert broker.stats["rejected_portfolio_risk_cap"] == 1
    assert broker.stats["reentry_orders_filled"] == 0


def test_revival_beats_reentry_on_the_same_ob():
    broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim",
                                   controlled_revival=True, allow_ob_reentry=True,
                                   min_reentry_wait_bars=1), 10_000.0)
    seed(broker, confirmed=True)
    broker.update_m5_trend(TrendDirection.BULLISH, "t0")
    bar(broker, 1)
    broker.process_tick(Tick("tt1", 100.4, 100.45))       # entry 1 fills
    # A second (unfilled) order on the same OB gets trend-cancelled while the
    # position is still open, producing a suspended revival record...
    second = seed(broker, "second", "ob1")
    broker.process_tick(Tick("tt2", 94.9, 94.95))         # position stops out
    broker.update_m5_trend(TrendDirection.BEARISH, "t1b")  # suspends "second"
    assert broker.stats["revival_suspended"] == 1
    assert broker.stats["reentry_candidates"] == 1        # exit also queued a candidate
    broker.update_m5_trend(TrendDirection.BULLISH, "t1c")
    bar(broker, 2)                                        # both would trigger here
    experimental = [o for o in broker.pending
                    if o.meta.get("is_revival") or o.meta.get("is_reentry")]
    assert len(experimental) == 1                         # only ONE order created
    assert experimental[0].meta.get("is_revival") is True  # Revival > Re-entry
    hist = broker.ob_entry_history["ob1"]
    assert hist["candidate_state"] == "superseded_by_revival"


def test_all_flags_on_historical_run_completes_and_reports(tmp_path):
    csv_path = tmp_path / "bars.csv"
    gen.write_csv(csv_path)
    cfg = BotConfig(
        trend_swing_length=9, entry_pivot_left=3, entry_pivot_right=3,
        rr=1.5, risk_fraction=0.005, choch_risk_cap_fraction=None,
        allowed_break_kinds=("BOS", "CHoCH"), strong_choch_only=True,
        entry_mode="sweep_reclaim", sweep_reclaim_atr_buffer=0.20, atr_period=50,
        sweep_reclaim_max_bars=2, controlled_revival=True, allow_ob_reentry=True,
        wait_for_spread_after_confirmation=True, spread_wait_max_bars=3,
        max_open_positions=2, portfolio_risk_cap=0.0075,
        db_path=tmp_path / "all" / "paper.sqlite3")
    cfg.validate()
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from pine_ob_bot.historical import run_historical
    except ImportError:
        from historical import run_historical
    summary = run_historical(csv_path, cfg, initial_equity=10_000.0,
                             spread=0.20, run_label="allflags")
    baseline = 7  # trades of the frozen sweep_reclaim fixture on this CSV
    assert summary["trades"] >= baseline                  # never LOSES trades
    for key in ("revival_suspended", "revival_orders_created",
                "reentry_candidates", "sweep_reclaim_lag1_confirms",
                "rejected_portfolio_risk_cap", "controlled_revival",
                "avg_spread_wait_seconds"):
        assert key in summary


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                import tempfile
                fn(Path(tempfile.mkdtemp()))
            else:
                fn()
            print("ok", name)
