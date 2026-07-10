"""M1Builder: causal M1 aggregation from Bid/Ask ticks.

Run with:  pytest pine_ob_bot/tests/test_m1_aggregation.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.m1 import M1Builder
    from pine_ob_bot.models import Tick
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from m1 import M1Builder
    from models import Tick


def t(stamp: str, bid: float, ask: float | None = None) -> Tick:
    return Tick(f"2026-04-10T{stamp}+00:00", bid, ask if ask is not None else bid + 0.1)


def test_minute_ohlc_from_ticks_bid_and_ask():
    builder = M1Builder()
    assert builder.push(t("10:04:01", 100.0, 100.2)) is None
    assert builder.push(t("10:04:20", 100.8, 100.9)) is None
    assert builder.push(t("10:04:40", 99.5, 99.8)) is None
    assert builder.push(t("10:04:59", 100.3, 100.4)) is None
    closed = builder.push(t("10:05:00", 101.0, 101.1))   # first tick of next minute
    assert closed is not None
    assert closed.time == "2026-04-10T10:04:00+00:00"
    assert (closed.open, closed.high, closed.low, closed.close) == (100.0, 100.8, 99.5, 100.3)
    assert (closed.bid_open, closed.bid_close) == (100.0, 100.3)
    assert (closed.ask_open, closed.ask_high, closed.ask_low, closed.ask_close) == \
        (100.2, 100.9, 99.8, 100.4)
    assert abs(closed.spread_close - 0.1) < 1e-9
    assert abs(closed.spread_high - 0.3) < 1e-9
    assert closed.tick_count == 4
    assert abs(closed.spread_average - (0.2 + 0.1 + 0.3 + 0.1) / 4) < 1e-9
    assert closed.source_timezone == "UTC"


def test_m1_and_m5_boundaries_share_the_same_grid():
    """The tick at 10:05:00 closes BOTH the 10:04 M1 and the 10:00 M5; the
    M1 bucket floor must be exactly the minute grid the M5 bucket uses."""
    builder = M1Builder()
    builder.push(t("10:04:59", 100.0))
    closed = builder.push(t("10:05:00", 100.5))
    assert closed.time.endswith("10:04:00+00:00")
    assert builder.time.endswith("10:05:00+00:00")


def test_duplicate_ticks_counted_and_ignored():
    builder = M1Builder()
    builder.push(t("10:04:01", 100.0))
    builder.push(t("10:04:01", 100.0))                    # exact duplicate
    assert builder.counters["m1_duplicate_ticks"] == 1
    assert builder.tick_count == 1


def test_out_of_order_ticks_are_dropped_safely():
    builder = M1Builder()
    builder.push(t("10:05:01", 100.0))
    assert builder.push(t("10:04:30", 99.0)) is None      # older minute
    assert builder.counters["m1_out_of_order_ticks"] == 1
    assert builder.low == 100.0                           # never applied


def test_empty_minutes_recorded_never_synthesized():
    builder = M1Builder()
    builder.push(t("10:04:30", 100.0))
    closed = builder.push(t("10:08:10", 101.0))           # 10:05..10:07 empty
    assert closed is not None and closed.time.endswith("10:04:00+00:00")
    assert builder.counters["m1_empty_minutes"] == 3
    assert builder.counters["m1_tick_gaps"] == 1
    assert builder.counters["m1_bars_created"] == 1       # NO synthetic bars


def test_partial_bar_flagged_by_tick_count():
    builder = M1Builder(partial_min_ticks=3)
    builder.push(t("10:04:30", 100.0))
    closed = builder.push(t("10:05:00", 100.5))
    assert closed.partial is True
    assert builder.counters["m1_partial_bars"] == 1


def test_dump_load_roundtrips_the_forming_bar():
    builder = M1Builder()
    builder.push(t("10:04:01", 100.0, 100.2))
    builder.push(t("10:04:30", 100.6, 100.7))
    state = builder.dump_state()

    restored = M1Builder()
    restored.load_state(state)
    closed = restored.push(t("10:05:00", 100.4))
    assert closed is not None
    assert (closed.open, closed.high, closed.close) == (100.0, 100.6, 100.6)
    assert closed.tick_count == 2
    # A duplicate of the last pre-restart tick is still recognized.
    restored2 = M1Builder()
    restored2.load_state(state)
    restored2.push(t("10:04:30", 100.6, 100.7))
    assert restored2.counters["m1_duplicate_ticks"] == 1


def test_min_max_ticks_per_bar_counters():
    builder = M1Builder()
    builder.push(t("10:04:01", 100.0))
    builder.push(t("10:04:02", 100.1))
    builder.push(t("10:05:00", 100.2))                    # closes 2-tick bar
    builder.push(t("10:06:00", 100.3))                    # closes 1-tick bar
    assert builder.counters["m1_max_ticks_per_bar"] == 2
    assert builder.counters["m1_min_ticks_per_bar"] == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
