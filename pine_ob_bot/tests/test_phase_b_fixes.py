"""Regression tests for the 5 bug fixes made during the "review from scratch"
audit (appearance / code-bug / logic-bug / test phases requested by the
user). Each test targets exactly one fixed code path and is written against
the real project modules -- no reimplementation of the logic under test.

Run with:  pytest pine_ob_bot/tests/test_phase_b_fixes.py -v
"""
from __future__ import annotations

import csv
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.historical import run_historical
    from pine_ob_bot.models import Candle, OrderBlock, PendingOrder, Tick
    from pine_ob_bot.paper import PaperBroker, SymbolSpec
    from pine_ob_bot.pine_engine import PineSwingOBEngine
    from pine_ob_bot.report_worker import ReportWorker
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from historical import run_historical
    from models import Candle, OrderBlock, PendingOrder, Tick
    from paper import PaperBroker, SymbolSpec
    from pine_engine import PineSwingOBEngine
    from report_worker import ReportWorker


# ---------------------------------------------------------------------------
# Fix 1: config.py -- max_open_positions > 1 is legitimate "burst fill"
# capacity: while the broker is flat, up to N pending orders may fill on the
# same tick (proven by tests/test_pine_ob_bot.py
# test_two_position_capacity_fills_two_orders); once any position is open no
# new fills are evaluated. validate() must therefore ACCEPT values above 1
# and only reject non-positive capacity.
# ---------------------------------------------------------------------------
def test_config_accepts_max_open_positions_above_one(tmp_path):
    cfg = BotConfig(max_open_positions=2, db_path=tmp_path / "x.sqlite3")
    cfg.validate()  # burst-fill capacity is supported; must not raise


def test_config_rejects_non_positive_max_open_positions(tmp_path):
    cfg = BotConfig(max_open_positions=0, db_path=tmp_path / "x.sqlite3")
    try:
        cfg.validate()
        assert False, "validate() should reject max_open_positions < 1"
    except ValueError as exc:
        assert "max_open_positions" in str(exc)


def test_config_still_accepts_max_open_positions_equal_one(tmp_path):
    cfg = BotConfig(max_open_positions=1, db_path=tmp_path / "x.sqlite3")
    cfg.validate()  # must not raise


# ---------------------------------------------------------------------------
# Fix 2: paper.py process_tick -- a pending order whose stop was already
# breached by price (before ever reaching its entry) is now marked
# lifecycle_state="invalidated" in lockstep with active=False, instead of
# leaving lifecycle_state stuck at "armed" while active is False.
# ---------------------------------------------------------------------------
def _cfg(tmp_path, **overrides) -> BotConfig:
    return BotConfig(db_path=tmp_path / "x.sqlite3", **overrides)


def _spec() -> SymbolSpec:
    return SymbolSpec(tick_size=0.01, tick_value=1.0, volume_min=0.01,
                      volume_max=100.0, volume_step=0.01)


def test_process_tick_invalidates_order_when_price_already_breached_stop(tmp_path):
    cfg = _cfg(tmp_path)
    broker = PaperBroker(cfg, 10_000.0, _spec())
    order = PendingOrder("o1", "ob1", "bull", entry=105.0, stop=100.0, target=115.0,
                         created_index=0, created_time="t0", active=True,
                         meta={}, lifecycle_state="armed")
    broker.pending.append(order)
    broker.market_index = 0

    # Price already fell to/through the stop before the entry was ever
    # touched -- the order must die as "invalidated", not silently sit
    # active=False/lifecycle_state="armed" (the desync this fix closes).
    tick = Tick("t1", bid=98.9, ask=99.0)
    closed = broker.process_tick(tick)

    assert closed == []
    assert order.active is False
    assert order.lifecycle_state == "invalidated"


def test_process_tick_leaves_healthy_order_armed(tmp_path):
    """Control case: price nowhere near the stop must not touch the order."""
    cfg = _cfg(tmp_path)
    broker = PaperBroker(cfg, 10_000.0, _spec())
    order = PendingOrder("o1", "ob1", "bull", entry=105.0, stop=100.0, target=115.0,
                         created_index=0, created_time="t0", active=True,
                         meta={}, lifecycle_state="armed")
    broker.pending.append(order)
    broker.market_index = 0

    tick = Tick("t1", bid=106.9, ask=107.0)  # above entry, nowhere near stop
    broker.process_tick(tick)

    assert order.active is True
    assert order.lifecycle_state == "armed"


# ---------------------------------------------------------------------------
# Fix 3: pine_engine.py -- the 100-block history cap on order_blocks now
# trims only INACTIVE history, never an active block, so a still-live block
# can't silently stop being checked by the invalidation loop.
# ---------------------------------------------------------------------------
def test_order_blocks_cap_keeps_active_blocks_over_inactive_history(tmp_path):
    cfg = BotConfig(trend_swing_length=50, atr_period=50, db_path=tmp_path / "x.sqlite3")
    engine = PineSwingOBEngine(cfg)
    active = [OrderBlock(f"active-{i}", "bull" if i % 2 else "bear", 6000.0, 5900.0,
                         i, f"t{i}", i, f"t{i}", active=True) for i in range(60)]
    inactive = [OrderBlock(f"inactive-{i}", "bear", 6000.0, 5900.0,
                           i, f"t{i}", i, f"t{i}", active=False) for i in range(60)]
    engine.order_blocks = active + inactive  # 120 total, over the 100 cap

    # i=0 is below trend_swing_length=50, so this candle cannot itself form a
    # new swing/OB. Its range (5920-5950) sits strictly inside every seeded
    # block's [5900, 6000] zone so it can't cross/invalidate any of them --
    # it only exercises the cap-trim logic in isolation.
    filler = Candle("2024-01-01T00:00:00+00:00", 5930.0, 5950.0, 5920.0, 5940.0)
    engine.process(filler)

    assert len(engine.order_blocks) == 100
    kept_ids = {ob.id for ob in engine.order_blocks}
    assert all(ob.id in kept_ids for ob in active), \
        "an active order block was dropped by the >100 history trim"
    assert sum(1 for ob in engine.order_blocks if not ob.active) == 40


# ---------------------------------------------------------------------------
# Fix 4: historical.py -- events appended by the LAST candle's own
# update_m5_trend()/add_ob() calls (after that same candle's top-of-loop
# drain already ran) are now flushed after the loop instead of being
# silently dropped from the per-event log/DB.
# ---------------------------------------------------------------------------
# Same 9-bar sequence used by test_trend_engine_integration.py: bar 7 forms a
# bull OB (bullish BOS), bar 8 forms a bear OB (bearish CHoCH). With the
# default 5/5 Entry Pivot window, no pivot can possibly confirm within 9
# bars, so BOTH formed OBs are rejected with "order_rejected_entry_pivot" --
# bar 8's rejection is appended on the very last iteration of the loop.
_BARS = [
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 100.2, 95.0, 95.5),
    (95.5, 96.0, 95.2, 95.8),
    (95.8, 96.2, 95.3, 96.0),
    (96.0, 105.0, 95.9, 104.0),
    (104.0, 104.5, 103.5, 104.2),
    (104.2, 104.6, 103.8, 104.4),
    (104.4, 106.0, 104.0, 105.5),   # bar 7: bullish BOS -> bull OB rejected (no pivot yet)
    (105.5, 105.8, 94.0, 94.5),     # bar 8 (LAST): bearish CHoCH -> bear OB rejected (no pivot yet)
]


def _write_bars_csv(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "open", "high", "low", "close"])
        for i, (o, h, l, c) in enumerate(_BARS):
            writer.writerow([f"2024-01-01T00:{i:02d}:00+00:00", o, h, l, c])


def test_historical_flushes_final_candle_event_to_log(tmp_path):
    csv_path = tmp_path / "bars.csv"
    _write_bars_csv(csv_path)
    cfg = BotConfig(trend_swing_length=2, atr_period=5, risk_fraction=0.01,
                    allowed_break_kinds=("BOS", "CHoCH"),
                    db_path=tmp_path / "data" / "paper.sqlite3")

    run_historical(csv_path, cfg, initial_equity=10_000.0, spread=0.0)

    db_path = (cfg.db_path.parent / "historical_bars.sqlite3")
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT time FROM events WHERE kind='order_rejected_entry_pivot' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    last_bar_time = "2024-01-01T00:08:00+00:00"
    times = [r[0] for r in rows]
    # Without the final-flush fix this list would contain only bar 7's event
    # (bar 8's own event -- appended during the loop's very last iteration,
    # after that iteration's own top-of-loop drain -- was silently dropped).
    assert len(rows) == 2, f"expected 2 rejected-entry-pivot events, got {times}"
    assert any(t.startswith(last_bar_time[:16]) for t in times), \
        "the last candle's own trend event never reached the log"


# ---------------------------------------------------------------------------
# Fix 5: report_worker.py -- submit(..., block=True) waits for a free queue
# slot instead of dropping the job like the default non-blocking submit.
# Used only for the final shutdown report so the last snapshot is never lost.
# ---------------------------------------------------------------------------
def test_submit_block_true_waits_instead_of_dropping(tmp_path):
    worker = ReportWorker(logging.getLogger("test-report-worker"))
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def blocker():
        started.set()
        release.wait(timeout=5)
        order.append("blocker")

    def recorder(tag):
        order.append(tag)

    try:
        assert worker.submit(blocker) is True
        assert started.wait(timeout=2), "worker never picked up the first job"

        # queue maxsize=2; the worker thread is now stuck inside blocker(),
        # so the queue itself is empty again -- fill it back up.
        assert worker.submit(recorder, "b") is True
        assert worker.submit(recorder, "c") is True
        # Queue is now full: the default non-blocking submit must drop it.
        assert worker.submit(recorder, "dropped") is False

        result = {}

        def call_blocking_submit():
            result["ok"] = worker.submit(recorder, "e_blocked", block=True)

        waiter = threading.Thread(target=call_blocking_submit)
        waiter.start()
        time.sleep(0.2)  # let it genuinely block on the full queue
        assert waiter.is_alive(), "blocking submit returned before a slot freed up"

        release.set()  # let 'blocker' finish -> worker drains 'b', 'c', then 'e_blocked'
        waiter.join(timeout=5)
        assert not waiter.is_alive()
        assert result["ok"] is True
    finally:
        worker.close()

    assert "e_blocked" in order, "the blocking submit's job was dropped, not queued"
    assert "b" in order and "c" in order


if __name__ == "__main__":
    import inspect as _inspect
    _mod = sys.modules[__name__]
    _tmp_counter = [0]
    for _name, _fn in list(vars(_mod).items()):
        if _name.startswith("test_") and callable(_fn):
            import tempfile
            _tmp = Path(tempfile.mkdtemp())
            _fn(_tmp) if "tmp_path" in _inspect.signature(_fn).parameters else _fn()
            print(f"OK {_name}")
    print("all tests passed")
