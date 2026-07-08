"""Unit tests for the Entry Pivot freshness gate (max_entry_pivot_age_bars).

Age is measured from the pivot's CONFIRMATION bar
(candle_index + right_bars), the boundary is inclusive, only the latest
same-direction confirmed pivot is consulted (no fallback to older pivots),
and max_entry_pivot_age_bars=None must reproduce the legacy ever-seen rule
exactly.

Run with:  pytest pine_ob_bot/tests/test_entry_pivot_freshness.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.entry_pivot import (PIVOT_HIGH, PIVOT_LOW,
                                         ClassicEntryPivotDetector, EntryPivot)
    from pine_ob_bot.models import Candle, OrderBlock
    from pine_ob_bot.paper import PaperBroker
    from pine_ob_bot.trend_filter import TrendDirection
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from entry_pivot import (PIVOT_HIGH, PIVOT_LOW,
                             ClassicEntryPivotDetector, EntryPivot)
    from models import Candle, OrderBlock
    from paper import PaperBroker
    from trend_filter import TrendDirection


def make_broker(max_age=None, trend="bullish"):
    cfg = BotConfig(max_entry_pivot_age_bars=max_age)
    cfg.validate()
    broker = PaperBroker(cfg, 10_000.0)
    broker.enable_entry_pivot_gate()
    broker.update_m5_trend(TrendDirection(trend), "t0")
    return broker


def make_ob(formed_index, direction="bull", tag=0):
    return OrderBlock(f"ob{formed_index}_{tag}", direction, 101.0, 99.0,
                      formed_index - 1, "t", formed_index, "t", True, "BOS", 1.0)


def make_pivot(kind, candle_index, right=2):
    return EntryPivot(candle_index, "pt", "ct", kind, 100.0, 3, right)


def test_validation_rejects_zero_and_negative():
    for bad in (0, -3):
        try:
            BotConfig(max_entry_pivot_age_bars=bad).validate()
        except ValueError:
            continue
        raise AssertionError(f"{bad} must be rejected")
    BotConfig(max_entry_pivot_age_bars=None).validate()
    BotConfig(max_entry_pivot_age_bars=1).validate()


def test_none_reproduces_legacy_ever_seen_rule():
    broker = make_broker(max_age=None)
    broker.update_entry_pivot(make_pivot(PIVOT_LOW, 10))  # confirmed at 12
    for i in range(300):
        idx = 500 + i * 3
        order = broker.add_ob(make_ob(idx, tag=i))
        assert order is not None  # ancient pivot admits everything (legacy)
        assert order.meta["entry_pivot_age_bars_at_setup"] == idx - 12
    assert broker.stats["rejected_stale_entry_pivot"] == 0


def test_fresh_inclusive_and_stale_boundaries():
    for age, accepted in ((5, True), (6, True), (7, False)):
        broker = make_broker(max_age=6)
        broker.update_entry_pivot(make_pivot(PIVOT_LOW, 100, right=2))
        order = broker.add_ob(make_ob(102 + age))
        assert (order is not None) == accepted, (age, accepted)
        if accepted:
            assert broker.stats["accepted_fresh_entry_pivot"] == 1
            assert order.meta["entry_pivot_age_bars_at_setup"] == age
            assert order.meta["entry_pivot_confirmation_index"] == 102
            assert order.meta["entry_pivot_type"] == "low"
            assert order.meta["max_entry_pivot_age_bars"] == 6
        else:
            assert broker.stats["rejected_stale_entry_pivot"] == 1
            assert broker.stats["rejected_no_entry_pivot"] == 0


def test_missing_pivot_counts_no_pivot_not_stale():
    broker = make_broker(max_age=6)
    assert broker.add_ob(make_ob(200)) is None
    assert broker.stats["rejected_no_entry_pivot"] == 1
    assert broker.stats["rejected_stale_entry_pivot"] == 0


def test_direction_rules_buy_low_sell_high():
    broker = make_broker(max_age=6)
    broker.update_entry_pivot(make_pivot(PIVOT_HIGH, 100))  # only a HIGH
    assert broker.add_ob(make_ob(104, "bull")) is None
    assert broker.stats["rejected_no_entry_pivot"] == 1

    broker = make_broker(max_age=6, trend="bearish")
    broker.update_entry_pivot(make_pivot(PIVOT_LOW, 100))   # only a LOW
    assert broker.add_ob(make_ob(104, "bear")) is None

    broker = make_broker(max_age=6, trend="bearish")
    broker.update_entry_pivot(make_pivot(PIVOT_HIGH, 100))
    order = broker.add_ob(make_ob(104, "bear"))
    assert order is not None and order.meta["entry_pivot_type"] == "high"


def test_newest_pivot_selected_and_no_fallback_to_older():
    broker = make_broker(max_age=6)
    broker.update_entry_pivot(make_pivot(PIVOT_LOW, 50))    # older
    broker.update_entry_pivot(make_pivot(PIVOT_LOW, 100))   # newest
    order = broker.add_ob(make_ob(105))
    assert order.meta["entry_pivot_candle_index"] == 100

    broker = make_broker(max_age=6)
    broker.update_entry_pivot(make_pivot(PIVOT_LOW, 50))
    broker.update_entry_pivot(make_pivot(PIVOT_LOW, 100))
    assert broker.add_ob(make_ob(120)) is None  # newest is stale -> reject
    assert broker.stats["rejected_stale_entry_pivot"] == 1


def test_unconfirmed_pivot_acts_as_missing_no_lookahead():
    broker = make_broker(max_age=None)
    broker.update_entry_pivot(make_pivot(PIVOT_LOW, 100, right=5))  # conf 105
    assert broker.add_ob(make_ob(103)) is None  # setup before confirmation
    assert broker.stats["rejected_no_entry_pivot"] == 1


def test_asymmetric_left_never_adds_confirmation_delay():
    bars = [Candle(f"t{i}", 100, 100 + 0.01 * i, 99 - 0.01 * i, 100)
            for i in range(10)]
    bars.append(Candle("t10", 100, 100.05, 95.0, 100))  # candidate low at 10
    bars += [Candle(f"t{11 + i}", 100, 100.04, 99.5, 100) for i in range(4)]

    def confirm_bars(left):
        det = ClassicEntryPivotDetector(left, 2)
        out = []
        for i, c in enumerate(bars):
            for p in det.process(c):
                if p.kind == PIVOT_LOW:
                    out.append((i, p))
        return out

    wide = confirm_bars(10)
    assert len(wide) == 1
    bar_i, pivot = wide[0]
    assert pivot.candle_index == 10 and pivot.right_bars == 2
    assert bar_i == 12  # published exactly when bar candidate+right closes
    assert pivot.candle_index + pivot.right_bars == 12
    narrow = confirm_bars(2)
    assert [i for i, _ in narrow] == [12]  # left size changes candidacy only


def test_dump_load_with_index_offset_keeps_age_causal():
    broker = make_broker(max_age=6)
    broker.update_entry_pivot(make_pivot(PIVOT_LOW, 100))
    data = broker.dump_state(index_offset=90)
    restored = make_broker(max_age=6)
    restored.load_state(data)
    assert restored.last_pivot_low.candle_index == 10  # shifted by offset
    order = restored.add_ob(make_ob(18))               # age exactly 6
    assert order is not None
    assert order.meta["entry_pivot_age_bars_at_setup"] == 6
    assert restored.add_ob(make_ob(19, tag=1)) is None  # age 7 -> stale
    assert restored.stats["rejected_stale_entry_pivot"] == 1


def test_age_telemetry_and_safe_average():
    broker = make_broker(max_age=None)
    broker.update_entry_pivot(make_pivot(PIVOT_LOW, 100))
    for k, idx in enumerate((104, 106, 110)):
        broker.add_ob(make_ob(idx, tag=k))
    stats = broker.stats
    assert stats["entry_pivot_age_samples"] == 3
    assert stats["entry_pivot_age_sum_bars"] == 2 + 4 + 8
    assert stats["entry_pivot_age_max_bars"] == 8
    # zero-sample derivation must not divide by zero
    empty = make_broker(max_age=None).stats
    samples = empty["entry_pivot_age_samples"]
    assert (None if not samples else 1) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
