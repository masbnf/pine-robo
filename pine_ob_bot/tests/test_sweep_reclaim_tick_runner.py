"""sweep_reclaim confirmation on the tick execution path.

The sweep is measured against the OB ENTRY edge, never the stop: a candle
trading through the stop extreme is exactly the condition under which the OB
engine invalidates the block on that same candle (cancel_ob then deactivates
the order), so a stop-based sweep could never survive to the next tick.

Live is tick-only for execution: a closed M5 candle confirms a pending
sweep_reclaim order (PaperBroker.process_signal_candle) and the next live
tick supplies the only executable fill. run_tick_historical() must drive the
broker through the exact same entry point -- these tests cover the broker
behaviour, the stop-break cancellation, and the tick runner's wiring.

Run with:  pytest pine_ob_bot/tests/test_sweep_reclaim_tick_runner.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.models import Candle, PendingOrder, Tick
    from pine_ob_bot.paper import PaperBroker, SymbolSpec
    from pine_ob_bot.tick_historical import run_tick_historical
    from pine_ob_bot.trend_filter import TrendDirection
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from models import Candle, PendingOrder, Tick
    from paper import PaperBroker, SymbolSpec
    from tick_historical import run_tick_historical
    from trend_filter import TrendDirection

T = "2024-01-01T00:{:02d}:00+00:00".format
ATR = 0.10
BUFFER = 0.20  # sweep_reclaim_atr_buffer -> stop moves by 0.20 * ATR = 0.02


def _sweep_broker(direction: str, entry: float, stop: float) -> tuple[PaperBroker, PendingOrder]:
    cfg = BotConfig(entry_mode="sweep_reclaim", sweep_reclaim_atr_buffer=BUFFER,
                    risk_fraction=0.01)
    broker = PaperBroker(cfg, equity=10_000.0, spec=SymbolSpec())
    trend = TrendDirection.BULLISH if direction == "bull" else TrendDirection.BEARISH
    broker.update_m5_trend(trend, T(0))
    target = entry + 1.5 * abs(entry - stop) * (1 if direction == "bull" else -1)
    order = PendingOrder(id="order-1", ob_id="ob-1", direction=direction, entry=entry,
                         stop=stop, target=target, created_index=0, created_time=T(0),
                         active=True, meta={"atr_at_formation": ATR},
                         lifecycle_state="armed")
    broker.pending.append(order)
    return broker, order


def test_signal_candle_confirms_buy_entry_edge_sweep_and_next_tick_fills():
    broker, order = _sweep_broker("bull", entry=1.00, stop=0.90)

    # Price enters the OB through the entry edge but never breaks the stop,
    # and the candle closes back above the entry.
    candle = Candle(T(5), 1.02, 1.04, 0.95, 1.01)
    assert candle.low < order.entry
    assert candle.low > order.stop  # the OB itself stays valid
    assert candle.close > order.entry
    broker.process_signal_candle(candle, bar_index=1)

    assert order.meta["sweep_reclaim_confirmed"] is True
    assert abs(order.stop - (0.90 - BUFFER * ATR)) < 1e-12  # 0.88
    assert broker.positions == []  # closed candles never fill on the tick path
    assert broker.stats["sweep_reclaim_checked"] == 1
    assert broker.stats["sweep_reclaim_entry_swept"] == 1
    assert broker.stats["sweep_reclaim_confirmed"] == 1

    # The next tick supplies the only executable fill price (Ask for a buy).
    broker.process_tick(Tick(T(6), 1.02, 1.021))

    assert len(broker.positions) == 1
    position = broker.positions[0]
    assert position.direction == "bull"
    assert position.entry == 1.021
    assert abs(position.stop - 0.88) < 1e-12
    # Target is re-derived at fill from the buffered stop distance.
    assert abs(position.target - (1.021 + broker.cfg.rr * (1.021 - 0.88))) < 1e-9
    assert broker.stats["sweep_reclaim_filled"] == 1


def test_signal_candle_confirms_sell_entry_edge_sweep_and_next_tick_fills():
    broker, order = _sweep_broker("bear", entry=1.00, stop=1.10)

    # Price pushes above the entry edge without breaking the stop, then
    # closes back below the entry.
    candle = Candle(T(5), 0.98, 1.05, 0.96, 0.99)
    assert candle.high > order.entry
    assert candle.high < order.stop  # the OB itself stays valid
    assert candle.close < order.entry
    broker.process_signal_candle(candle, bar_index=1)

    assert order.meta["sweep_reclaim_confirmed"] is True
    assert abs(order.stop - (1.10 + BUFFER * ATR)) < 1e-12  # 1.12
    assert broker.positions == []

    # Sells fill on Bid.
    broker.process_tick(Tick(T(6), 0.98, 0.981))

    assert len(broker.positions) == 1
    position = broker.positions[0]
    assert position.direction == "bear"
    assert position.entry == 0.98
    assert abs(position.stop - 1.12) < 1e-12
    assert abs(position.target - (0.98 - broker.cfg.rr * (1.12 - 0.98))) < 1e-9
    assert broker.stats["sweep_reclaim_filled"] == 1


def test_no_entry_penetration_means_no_confirmation():
    broker, order = _sweep_broker("bull", entry=1.00, stop=0.90)

    # The candle never trades below the entry edge: no sweep, no confirmation.
    broker.process_signal_candle(Candle(T(5), 1.02, 1.05, 1.005, 1.03), bar_index=1)
    assert order.meta.get("sweep_reclaim_confirmed") is None
    assert order.stop == 0.90  # untouched, no buffer applied
    assert broker.stats["sweep_reclaim_checked"] == 1
    assert broker.stats["sweep_reclaim_entry_swept"] == 0

    broker.process_tick(Tick(T(6), 1.02, 1.021))
    assert broker.positions == []  # unconfirmed orders never fill on ticks


def test_sweep_without_reclaim_close_is_not_confirmed():
    broker, order = _sweep_broker("bull", entry=1.00, stop=0.90)

    # Penetrates the entry edge but closes back inside the OB: swept, not confirmed.
    broker.process_signal_candle(Candle(T(5), 1.02, 1.03, 0.95, 0.99), bar_index=1)
    assert order.meta.get("sweep_reclaim_confirmed") is None
    assert broker.stats["sweep_reclaim_entry_swept"] == 1
    assert broker.stats["sweep_reclaim_confirmed"] == 0


def test_stop_break_cancels_the_order_instead_of_trading_it():
    """Regression for the self-defeating stop-based sweep: a candle through
    the stop extreme is the OB engine's invalidation condition, so the runner
    cancels the order on the same close_bar -- the confirmation (if any) must
    never survive into a tick fill."""
    broker, order = _sweep_broker("bull", entry=1.00, stop=0.90)

    # low < stop and close > entry: the exact pattern the old stop-based
    # condition treated as a tradeable confirmation.
    broker.process_signal_candle(Candle(T(5), 0.95, 1.04, 0.85, 1.02), bar_index=1)
    # Same candle, same close_bar: the engine invalidates the swept OB.
    broker.cancel_ob("ob-1")

    assert order.active is False
    assert order.lifecycle_state == "invalidated"
    assert broker.stats["sweep_reclaim_invalidated"] == 1

    broker.process_tick(Tick(T(6), 1.02, 1.021))
    assert broker.positions == []
    assert broker.stats["sweep_reclaim_filled"] == 0


def test_confirmation_applies_the_buffer_only_once():
    broker, order = _sweep_broker("bull", entry=1.00, stop=0.90)

    broker.process_signal_candle(Candle(T(5), 1.02, 1.04, 0.95, 1.01), bar_index=1)
    assert abs(order.stop - 0.88) < 1e-12
    # A second qualifying candle must not re-confirm or re-buffer the stop.
    broker.process_signal_candle(Candle(T(10), 1.02, 1.04, 0.95, 1.01), bar_index=2)
    assert abs(order.stop - 0.88) < 1e-12
    assert broker.stats["sweep_reclaim_confirmed"] == 1


def test_run_tick_historical_drives_process_signal_candle(tmp_path, monkeypatch):
    """Regression: the tick runner must advance candle-driven state through
    PaperBroker.process_signal_candle (the Live entry point), not through a
    hand-rolled market_index/_advance_lifecycle pair that silently skips the
    sweep_reclaim confirmation step."""
    ticks_path = tmp_path / "ticks_TEST_20240101.csv"
    rows = ["time,bid,ask"]
    # Three M5 buckets (00:00, 00:05, 00:10) -> exactly two closed bars.
    for minute, price in ((0, 1.00), (1, 1.01), (5, 1.02), (6, 1.03), (10, 1.04)):
        rows.append(f"2024-01-01T00:{minute:02d}:30+00:00,{price},{price + 0.01}")
    ticks_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    calls: list[tuple[str, int]] = []
    original = PaperBroker.process_signal_candle

    def spy(self, candle, bar_index):
        calls.append((candle.time, bar_index))
        return original(self, candle, bar_index)

    monkeypatch.setattr(PaperBroker, "process_signal_candle", spy)

    cfg = BotConfig(entry_mode="sweep_reclaim")
    summary = run_tick_historical([ticks_path], cfg, initial_equity=10_000.0,
                                  output_root=tmp_path / "out", label="wiring_test",
                                  warmup_bars=0)

    assert summary["bars"] == 2
    assert calls == [("2024-01-01T00:00:00+00:00", 0),
                     ("2024-01-01T00:05:00+00:00", 1)]
