from __future__ import annotations

from dataclasses import asdict, dataclass

from .models import Candle, OrderBlock, StructureBreak


def displacement_snapshot(candle: Candle, ob: OrderBlock,
                          breaks: list[StructureBreak]) -> dict:
    """Describe the structure-break candle without affecting trade admission."""
    br = next((item for item in breaks
               if item.direction == ob.direction and item.kind == ob.break_kind), None)
    atr = ob.atr_at_formation
    candle_range = candle.high - candle.low
    body = abs(candle.close - candle.open)
    if br and atr:
        close_through = (candle.close - br.pivot_level if ob.direction == "bull"
                         else br.pivot_level - candle.close) / atr
    else:
        close_through = None
    close_location = None
    if candle_range > 0:
        close_location = ((candle.close - candle.low) / candle_range
                          if ob.direction == "bull"
                          else (candle.high - candle.close) / candle_range)
    directional = (candle.close > candle.open if ob.direction == "bull"
                   else candle.close < candle.open)
    return {
        "bos_body_atr": body / atr if atr else None,
        "bos_range_atr": candle_range / atr if atr else None,
        "bos_body_to_range": body / candle_range if candle_range else None,
        "bos_close_through_atr": close_through,
        "bos_close_location": close_location,
        "bos_directional_body": directional,
    }


@dataclass(slots=True)
class ChochEvent:
    direction: str
    bar_index: int
    time: str
    pivot_level: float


class ChochContext:
    """Causal observation-only context for confirmed M5 swing CHoCH events."""

    def __init__(self) -> None:
        self.last_by_direction: dict[str, ChochEvent] = {}
        self.last: ChochEvent | None = None

    def process(self, breaks: list[StructureBreak]) -> None:
        for item in breaks:
            if item.kind != "CHoCH":
                continue
            event = ChochEvent(item.direction, item.bar_index, item.time,
                               item.pivot_level)
            self.last_by_direction[item.direction] = event
            self.last = event

    def snapshot(self, trade_direction: str, bar_index: int) -> dict:
        same = self.last_by_direction.get(trade_direction)
        opposite_direction = "bear" if trade_direction == "bull" else "bull"
        opposite = self.last_by_direction.get(opposite_direction)
        latest = self.last
        return {
            "m5_last_choch_direction": latest.direction if latest else "none",
            "m5_last_choch_alignment": (
                "none" if latest is None else
                "aligned" if latest.direction == trade_direction else "opposite"
            ),
            "m5_last_choch_age_bars": bar_index - latest.bar_index if latest else None,
            "m5_same_choch_age_bars": bar_index - same.bar_index if same else None,
            "m5_opposite_choch_age_bars": bar_index - opposite.bar_index if opposite else None,
        }

    def dump_state(self, index_offset: int = 0) -> dict:
        def event_state(value):
            if value is None:
                return None
            item = asdict(value); item["bar_index"] -= index_offset
            return item
        return {
            "last": event_state(self.last),
            "last_by_direction": {key: event_state(value)
                                  for key, value in self.last_by_direction.items()},
        }

    def load_state(self, data: dict) -> None:
        self.last = ChochEvent(**data["last"]) if data.get("last") else None
        self.last_by_direction = {
            key: ChochEvent(**value)
            for key, value in data.get("last_by_direction", {}).items()
        }


class DisplacementContext:
    """Rolling, causal percentile rank for BOS close-through strength."""

    def __init__(self, window: int = 100, min_samples: int = 20) -> None:
        self.window = window
        self.min_samples = min_samples
        self.values: list[float] = []

    def observe(self, value: float | None) -> dict:
        history = self.values[-self.window:]
        percentile = None
        if value is not None and len(history) >= self.min_samples:
            percentile = sum(item <= value for item in history) / len(history)
        if value is not None:
            self.values.append(float(value))
            if len(self.values) > self.window:
                self.values = self.values[-self.window:]
        return {"bos_close_through_percentile": percentile,
                "bos_displacement_top_quartile": percentile is not None and percentile >= .75,
                "bos_displacement_history_samples": len(history)}

    def dump_state(self) -> dict:
        return {"window": self.window, "min_samples": self.min_samples,
                "values": self.values}

    def load_state(self, data: dict) -> None:
        self.window = int(data.get("window", self.window))
        self.min_samples = int(data.get("min_samples", self.min_samples))
        self.values = [float(value) for value in data.get("values", [])][-self.window:]
