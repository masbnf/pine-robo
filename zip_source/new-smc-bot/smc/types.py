"""Shared data structures used across SMC modules.

Keeping these as small dataclasses keeps the rest of the codebase typed and
self-documenting. Add fields here as the model grows.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Dir(str, Enum):
    """Direction / bias."""
    BULL = "bull"
    BEAR = "bear"


@dataclass
class Swing:
    """A confirmed swing pivot."""
    idx: int            # absolute candle index in the series
    price: float
    kind: str           # "high" or "low"


@dataclass
class BOS:
    """Break of structure (trend confirmation)."""
    direction: Dir
    broken_swing: Swing     # the swing whose level was broken
    break_idx: int          # candle that closed beyond the swing
    impulse_start: int      # index where the breaking impulse began


@dataclass
class CHoCH:
    """Change of character (LTF trigger)."""
    direction: Dir
    broken_swing: Swing
    break_idx: int


@dataclass
class OrderBlock:
    """Last opposing candle before an impulse."""
    direction: Dir          # bull OB (demand) or bear OB (supply)
    idx: int
    top: float
    bottom: float

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


@dataclass
class Zone:
    """Supply or demand zone (price band)."""
    direction: Dir          # bull = demand, bear = supply
    top: float
    bottom: float
    origin_idx: int

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def overlaps(self, other_top: float, other_bottom: float) -> bool:
        return not (other_bottom > self.top or other_top < self.bottom)


@dataclass
class FVG:
    """Fair value gap (3-candle imbalance)."""
    direction: Dir
    idx: int                # index of the middle candle
    top: float
    bottom: float
    filled: bool = False


@dataclass
class Liquidity:
    """Resting liquidity pool above (BSL) / below (SSL) price."""
    kind: str               # "BSL" (buy-side) or "SSL" (sell-side)
    price: float
    idx: int
    swept: bool = False


@dataclass
class Signal:
    """A complete trade setup ready for execution."""
    direction: Dir
    entry: float
    sl: float
    tp: float
    idx: int                # bar index where the signal fired
    rr: float = 0.0
    lot: float = 0.0
    reason: str = ""
    meta: dict = field(default_factory=dict)
