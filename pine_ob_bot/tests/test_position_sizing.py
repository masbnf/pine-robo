"""Unit tests for the single, shared position-sizing function.

Covers every scenario required for the "never risk more than the configured
percentage" fix: an undersized lot must be rejected outright, never bumped up
to ``volume_min``; lots are always floored to ``volume_step``; ``volume_max``
is only ever a ceiling, never a blind assignment; and a battery of invalid /
degenerate inputs must all refuse to size a trade.

Run with:  pytest pine_ob_bot/tests/test_position_sizing.py -v
(run from the repository root that contains the ``pine_ob_bot`` package; if
``pine_ob_bot`` isn't importable as a package -- e.g. running pytest from
inside this folder -- the fallback import below adds the parent directory to
sys.path instead.)
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.position_sizing import (
        ACTUAL_RISK_EXCEEDS_ALLOWED_RISK,
        CALCULATED_VOLUME_BELOW_BROKER_MINIMUM,
        INVALID_RISK_INPUT,
        INVALID_STOP_DISTANCE,
        INVALID_SYMBOL_SPEC,
        calculate_safe_position_size,
    )
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from position_sizing import (
        ACTUAL_RISK_EXCEEDS_ALLOWED_RISK,
        CALCULATED_VOLUME_BELOW_BROKER_MINIMUM,
        INVALID_RISK_INPUT,
        INVALID_STOP_DISTANCE,
        INVALID_SYMBOL_SPEC,
        calculate_safe_position_size,
    )


# Common broker/symbol spec used by most cases below (XAUUSD-like, $1/tick/lot).
SPEC = dict(tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=100.0,
            volume_step=0.01)


def test_volume_above_broker_minimum_is_accepted():
    result = calculate_safe_position_size(
        equity=10_000, risk_percent=0.01, entry_price=1.00, stop_loss=0.90, **SPEC)
    assert result.is_valid is True
    assert result.rejection_reason is None
    assert result.allowed_risk == 100.0
    assert result.volume == 10.0
    assert abs(result.actual_risk - 100.0) < 1e-9  # float dust only
    assert result.actual_risk <= result.allowed_risk * 1.01


def test_volume_exactly_at_broker_minimum_is_accepted():
    # allowed_risk=1.0, distance=0.10, tick math -> raw volume == 0.10 == volume_min exactly.
    result = calculate_safe_position_size(
        equity=10_000, risk_percent=0.0001, entry_price=1.00, stop_loss=0.90,
        tick_size=0.01, tick_value=1.0, volume_min=0.10, volume_max=100.0, volume_step=0.01)
    assert result.volume == 0.10
    assert result.is_valid is True
    assert result.rejection_reason is None
    assert result.actual_risk <= result.allowed_risk * 1.01


def test_volume_below_broker_minimum_is_rejected_not_bumped_up():
    # raw volume normalizes to 0.05 lots, but the broker's minimum is 0.10 --
    # the setup must be rejected, and the returned volume must stay at the
    # (too-small) calculated value, never jump to volume_min.
    result = calculate_safe_position_size(
        equity=10_000, risk_percent=0.01, entry_price=100.0, stop_loss=80.0,
        tick_size=0.01, tick_value=1.0, volume_min=0.10, volume_max=100.0, volume_step=0.01)
    assert result.is_valid is False
    assert result.rejection_reason == CALCULATED_VOLUME_BELOW_BROKER_MINIMUM
    assert result.volume == 0.05
    assert result.volume != 0.10  # never silently raised to volume_min


def test_volume_rounds_down_to_step_never_up():
    # raw volume ~0.5714 lots at a 0.1 step must floor to 0.5, not round to 0.6.
    result = calculate_safe_position_size(
        equity=10_000, risk_percent=0.01, entry_price=10.00, stop_loss=8.25,
        tick_size=0.01, tick_value=1.0, volume_min=0.10, volume_max=100.0, volume_step=0.10)
    assert result.volume == 0.5
    assert result.is_valid is True
    assert result.actual_risk < result.allowed_risk  # flooring only ever shrinks risk


def test_actual_risk_never_exceeds_allowed_risk():
    cases = [
        dict(equity=10_000, risk_percent=0.01, entry_price=1.00, stop_loss=0.90, **SPEC),
        dict(equity=25_000, risk_percent=0.02, entry_price=2000.0, stop_loss=1995.0,
             tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=50.0, volume_step=0.01),
        dict(equity=500, risk_percent=0.005, entry_price=1.2345, stop_loss=1.2300,
             tick_size=0.0001, tick_value=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01),
    ]
    for kwargs in cases:
        result = calculate_safe_position_size(**kwargs)
        if result.is_valid:
            assert result.actual_risk <= result.allowed_risk * 1.01


def test_volume_above_broker_maximum_is_capped_not_assigned_blindly():
    # allowed_risk=100, distance=0.5 -> loss_per_lot=50 -> raw volume=2.0,
    # which exceeds volume_max (1.0). The result must be the capped/
    # normalized volume with its risk re-checked, not a blind assignment.
    result = calculate_safe_position_size(
        equity=10_000, risk_percent=0.01, entry_price=100.5, stop_loss=100.0,
        tick_size=0.01, tick_value=1.0, volume_min=0.01, volume_max=1.0, volume_step=0.01)
    assert result.volume == 1.0
    assert result.is_valid is True
    # Because raw_volume > volume_max implies loss_per_lot*volume_max < allowed_risk
    # by construction, capping can only ever shrink realized risk below the target.
    assert result.actual_risk < result.allowed_risk


def test_stop_loss_equal_entry_is_rejected():
    result = calculate_safe_position_size(
        equity=10_000, risk_percent=0.01, entry_price=1.2345, stop_loss=1.2345, **SPEC)
    assert result.is_valid is False
    assert result.rejection_reason == INVALID_STOP_DISTANCE
    assert result.volume == 0.0
    assert result.actual_risk == 0.0


def test_invalid_tick_size_is_rejected():
    result = calculate_safe_position_size(
        equity=10_000, risk_percent=0.01, entry_price=1.00, stop_loss=0.90,
        tick_size=0.0, tick_value=1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01)
    assert result.is_valid is False
    assert result.rejection_reason == INVALID_SYMBOL_SPEC


def test_invalid_tick_value_is_rejected():
    result = calculate_safe_position_size(
        equity=10_000, risk_percent=0.01, entry_price=1.00, stop_loss=0.90,
        tick_size=0.01, tick_value=-1.0, volume_min=0.01, volume_max=100.0, volume_step=0.01)
    assert result.is_valid is False
    assert result.rejection_reason == INVALID_SYMBOL_SPEC


def test_invalid_volume_step_or_bounds_is_rejected():
    for bad_field in ("volume_min", "volume_max", "volume_step"):
        kwargs = dict(equity=10_000, risk_percent=0.01, entry_price=1.00, stop_loss=0.90, **SPEC)
        kwargs[bad_field] = 0.0
        result = calculate_safe_position_size(**kwargs)
        assert result.is_valid is False
        assert result.rejection_reason == INVALID_SYMBOL_SPEC


def test_zero_or_negative_equity_creates_no_order():
    for equity in (0, -100):
        result = calculate_safe_position_size(
            equity=equity, risk_percent=0.01, entry_price=1.00, stop_loss=0.90, **SPEC)
        assert result.is_valid is False
        assert result.rejection_reason == INVALID_RISK_INPUT
        assert result.volume == 0.0


def test_zero_or_negative_risk_percent_creates_no_order():
    for risk_percent in (0, -0.01):
        result = calculate_safe_position_size(
            equity=10_000, risk_percent=risk_percent, entry_price=1.00, stop_loss=0.90, **SPEC)
        assert result.is_valid is False
        assert result.rejection_reason == INVALID_RISK_INPUT
        assert result.volume == 0.0


def test_requirement_scenario_equity_10k_risk_1pct_below_minimum():
    """The exact scenario called out in the fix request.

    Equity=10,000, Risk%=1%% -> Allowed Risk=100. A very wide stop makes the
    correctly calculated volume smaller than the broker's minimum lot.
    Expected: the order is rejected, volume is NOT bumped up to volume_min,
    and actual risk does not become 1,000 (10x over budget) as the old
    "force to volume_min" behavior would have produced.
    """
    equity, risk_percent = 10_000.0, 0.01
    entry, stop = 2000.0, 1000.0  # distance = 1000
    tick_size, tick_value = 0.01, 1.0
    volume_min, volume_max, volume_step = 0.01, 100.0, 0.01

    result = calculate_safe_position_size(
        equity=equity, risk_percent=risk_percent, entry_price=entry, stop_loss=stop,
        tick_size=tick_size, tick_value=tick_value, volume_min=volume_min,
        volume_max=volume_max, volume_step=volume_step)

    assert result.allowed_risk == 100.0
    assert result.is_valid is False
    assert result.rejection_reason == CALCULATED_VOLUME_BELOW_BROKER_MINIMUM
    assert result.volume != volume_min  # never forced up to the broker minimum

    # What the OLD buggy behavior would have done: force volume up to
    # volume_min and accept it, multiplying realized risk far past the cap.
    loss_per_lot = abs(entry - stop) / tick_size * tick_value
    old_buggy_actual_risk = loss_per_lot * volume_min
    assert old_buggy_actual_risk == 1000.0  # confirms the scenario matches the report
    assert result.actual_risk != old_buggy_actual_risk
    assert result.actual_risk < result.allowed_risk  # our result never risks 1,000
