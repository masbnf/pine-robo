"""Classic, symmetric M5 pivot detector used for entry timing only.

This is deliberately independent from ``PineSwingOBEngine`` (the "Trend
Swing" state machine that owns Major Swing High/Low, Major BOS/CHoCH and the
overall M5 Trend State). The classic pivot here is a simple, well-known
fixed-window swing point: a candidate bar whose high (or low) is strictly the
most extreme value across ``left_bars`` bars before it AND ``right_bars`` bars
after it. It is used only for local pullback/trigger timing -- confirming or
triggering an existing Order Block setup -- and it must never be able to
change the M5 Trend State on its own (see ``trend_filter.py`` /
``PineSwingOBEngine.trend`` for that).

Confirmation is strictly causal: a candidate at bar index ``i`` cannot be
published until ``right_bars`` closed bars after it exist, i.e. not before the
bar at index ``i + right_bars`` has itself closed. ``EntryPivot.confirmed_at``
records that later timestamp; ``EntryPivot.pivot_time`` records the earlier,
original candidate bar's timestamp. Trading decisions must key off
``confirmed_at`` (or the moment ``process()`` returns the pivot), never off
``pivot_time`` -- using ``pivot_time`` to backdate an entry would be exactly
the look-ahead this module is designed to prevent.

Note on the ``pivot_time``/``confirmed_at`` type: the rest of this project
(``Candle.time``, ``StructureBreak.time``, ``OrderBlock.formed_time``, ...)
uniformly uses ISO-8601 strings rather than ``datetime`` objects, including
through persistence (``dump_state``/``load_state`` round-trips as plain
JSON-friendly values). ``EntryPivot`` follows that existing convention (str)
rather than the literal ``datetime`` type sketched in the original request,
to stay consistent with every other timestamp in the codebase and avoid a
parse/format cost on every bar.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .models import Candle

PIVOT_HIGH = "pivot_high"
PIVOT_LOW = "pivot_low"


@dataclass(frozen=True, slots=True)
class EntryPivot:
    candle_index: int
    pivot_time: str
    confirmed_at: str
    kind: str  # "pivot_high" | "pivot_low"
    price: float
    left_bars: int
    right_bars: int


class ClassicEntryPivotDetector:
    """Causal, symmetric-window pivot detector (default 5 left / 5 right).

    Feed one closed M5 candle at a time via ``process()``. Each candidate bar
    is evaluated exactly once, the instant its right-side window completes,
    so every pivot is published exactly one time -- never re-emitted, never
    backdated.
    """

    def __init__(self, left_bars: int = 5, right_bars: int = 5) -> None:
        if left_bars < 1:
            raise ValueError("left_bars must be at least 1")
        if right_bars < 1:
            raise ValueError("right_bars must be at least 1")
        self.left_bars = left_bars
        self.right_bars = right_bars
        self.candles: list[Candle] = []
        # Absolute bar index of self.candles[0], for continuity across a
        # trimmed dump_state/load_state round-trip (mirrors PineSwingOBEngine).
        self.index_offset = 0

    def process(self, candle: Candle) -> list[EntryPivot]:
        self.candles.append(candle)
        local_i = len(self.candles) - 1
        candidate_local = local_i - self.right_bars
        if candidate_local - self.left_bars < 0:
            return []
        left_window = self.candles[candidate_local - self.left_bars:candidate_local]
        right_window = self.candles[candidate_local + 1:candidate_local + 1 + self.right_bars]
        if len(left_window) < self.left_bars or len(right_window) < self.right_bars:
            return []
        candidate = self.candles[candidate_local]
        confirmed: list[EntryPivot] = []
        if (all(candidate.high > bar.high for bar in left_window) and
                all(candidate.high > bar.high for bar in right_window)):
            confirmed.append(EntryPivot(candidate_local + self.index_offset, candidate.time,
                                        candle.time, PIVOT_HIGH, candidate.high,
                                        self.left_bars, self.right_bars))
        if (all(candidate.low < bar.low for bar in left_window) and
                all(candidate.low < bar.low for bar in right_window)):
            confirmed.append(EntryPivot(candidate_local + self.index_offset, candidate.time,
                                        candle.time, PIVOT_LOW, candidate.low,
                                        self.left_bars, self.right_bars))
        return confirmed

    def dump_state(self, max_candles: int | None = None) -> dict:
        start = 0
        candles = self.candles
        if max_candles is not None and len(self.candles) > max_candles:
            start = len(self.candles) - max_candles
            candles = self.candles[start:]
        return {"candles": [asdict(x) for x in candles],
                "left_bars": self.left_bars, "right_bars": self.right_bars,
                "index_offset": self.index_offset + start}

    def load_state(self, data: dict) -> None:
        self.left_bars = int(data.get("left_bars", self.left_bars))
        self.right_bars = int(data.get("right_bars", self.right_bars))
        self.candles = [Candle(**x) for x in data.get("candles", [])]
        self.index_offset = int(data.get("index_offset", 0))
