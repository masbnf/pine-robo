"""sweep_reclaim confirmation on the tick execution path.

Live is tick-only for execution: a closed M5 candle confirms a pending
sweep_reclaim order (PaperBroker.process_signal_candle) and the next live
tick supplies the only executable fill. run_tick_historical() must drive the
broker through the exact same entry point -- these tests cover both the
broker-level behaviour and the tick runner's wiring, which the existing
PaperBroker-only tests never exercised.

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


def test_signal_candle_confirms_buy_sweep_reclaim_and_next_tick_fills():
    broker, order = _sweep_broker("bull", entry=1.00, stop=0.90)

    # The closed candle sweeps the stop (low 0.85 < 0.90) and reclaims the
    # entry (close 1.02 > 1.00). No fill may happen here -- signal only.
    broker.process_signal_candle(Candle(T(5), 0.95, 1.05, 0.85, 1.02), bar_index=1)

    assert order.meta.get("sweep_reclaim_confirmed") is True
    assert abs(order.stop - (0.90 - BUFFER * ATR)) < 1e-12  # 0.88
    assert broker.positions == []  # closed candles never fill on the tick path

    # The next tick supplies the only executable fill price (Ask for a buy).
    broker.process_tick(Tick(T(6), 1.02, 1.021))

    assert len(broker.positions) == 1
    position = broker.positions[0]
    assert position.direction == "bull"
    assert position.entry == 1.021
    assert abs(position.stop - 0.88) < 1e-12
    # Target is re-derived at fill from the buffered stop distance.
    assert abs(position.target - (1.021 + broker.cfg.rr * (1.021 - 0.88))) < 1e-9


def test_signal_candle_confirms_sell_sweep_reclaim_and_next_tick_fills():
    broker, order = _sweep_broker("bear", entry=1.00, stop=1.10)

    # Sweep above the stop (high 1.15 > 1.10), reclaim below entry (0.98 < 1.00).
    broker.process_signal_candle(Candle(T(5), 1.05, 1.15, 0.97, 0.98), bar_index=1)

    assert order.meta.get("sweep_reclaim_confirmed") is True
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


def test_unconfirmed_sweep_reclaim_candle_does_not_arm_or_fill():
    broker, order = _sweep_broker("bull", entry=1.00, stop=0.90)

    # Reclaims the entry but never sweeps the stop: not confirmed.
    broker.process_signal_candle(Candle(T(5), 0.95, 1.05, 0.91, 1.02), bar_index=1)
    assert order.meta.get("sweep_reclaim_confirmed") is None
    assert order.stop == 0.90  # untouched, no buffer applied

    broker.process_tick(Tick(T(6), 1.02, 1.03))
    assert broker.positions == []  # unconfirmed orders never fill on ticks


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
