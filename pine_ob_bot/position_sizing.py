"""Deterministic, broker-safe position sizing.

Realized risk must never exceed the configured risk percentage. Bumping an
undersized volume up to the broker's ``volume_min`` would silently multiply
risk (sometimes several times over), so an undersized setup is rejected
instead of resized upward. This module is the single sizing path shared by
paper trading and MT5 Demo mirroring -- Demo never recomputes volume, it only
ever narrows what paper already sized (see ``demo_executor._fit_open_request``).
"""
from __future__ import annotations

from dataclasses import dataclass
import math

CALCULATED_VOLUME_BELOW_BROKER_MINIMUM = "CALCULATED_VOLUME_BELOW_BROKER_MINIMUM"
ACTUAL_RISK_EXCEEDS_ALLOWED_RISK = "ACTUAL_RISK_EXCEEDS_ALLOWED_RISK"
INVALID_STOP_DISTANCE = "INVALID_STOP_DISTANCE"
INVALID_SYMBOL_SPEC = "INVALID_SYMBOL_SPEC"
INVALID_RISK_INPUT = "INVALID_RISK_INPUT"

# Float-precision slack only, applied to lot-step/lot-minimum comparisons.
# Never used to justify additional risk.
_STEP_EPSILON = 1e-9
_RISK_TOLERANCE = 1.01


@dataclass(frozen=True, slots=True)
class PositionSizeResult:
    volume: float
    allowed_risk: float
    actual_risk: float
    is_valid: bool
    rejection_reason: str | None


def calculate_safe_position_size(
    equity: float,
    risk_percent: float,
    entry_price: float,
    stop_loss: float,
    tick_size: float,
    tick_value: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
) -> PositionSizeResult:
    """Size a position so realized risk never exceeds ``equity * risk_percent``.

    The lot is always rounded DOWN to ``volume_step`` (risk can only shrink
    from the raw calculation, never grow). If the rounded lot is below
    ``volume_min`` the setup is rejected -- it is never bumped up to the
    minimum, since that would multiply the realized risk past what was
    configured. ``is_valid`` is False for every rejection; callers must not
    open a position (paper or Demo) when it is False.
    """
    if equity <= 0 or risk_percent <= 0:
        return PositionSizeResult(0.0, 0.0, 0.0, False, INVALID_RISK_INPUT)
    if tick_size <= 0 or tick_value <= 0 or volume_step <= 0 or volume_min <= 0 or volume_max <= 0:
        return PositionSizeResult(0.0, 0.0, 0.0, False, INVALID_SYMBOL_SPEC)

    allowed_risk = equity * risk_percent
    distance = abs(entry_price - stop_loss)
    if distance <= 0:
        return PositionSizeResult(0.0, allowed_risk, 0.0, False, INVALID_STOP_DISTANCE)

    loss_per_lot = distance / tick_size * tick_value
    raw_volume = allowed_risk / loss_per_lot

    # Clamp to volume_max BEFORE rounding to the step, then always floor --
    # both operations can only ever reduce volume, never increase it.
    capped_volume = min(raw_volume, volume_max)
    normalized_volume = max(0.0, math.floor((capped_volume + _STEP_EPSILON) / volume_step)
                            * volume_step)

    if normalized_volume < volume_min - _STEP_EPSILON:
        actual_risk = loss_per_lot * normalized_volume
        return PositionSizeResult(normalized_volume, allowed_risk, actual_risk, False,
                                  CALCULATED_VOLUME_BELOW_BROKER_MINIMUM)

    actual_risk = loss_per_lot * normalized_volume
    if actual_risk > allowed_risk * _RISK_TOLERANCE:
        return PositionSizeResult(normalized_volume, allowed_risk, actual_risk, False,
                                  ACTUAL_RISK_EXCEEDS_ALLOWED_RISK)

    return PositionSizeResult(normalized_volume, allowed_risk, actual_risk, True, None)
