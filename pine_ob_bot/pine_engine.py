"""Bar-close port of the Swing Structure / Swing Order Block Pine state machine."""
from __future__ import annotations

from dataclasses import asdict
from uuid import uuid4

from .config import BotConfig
from .models import Candle, OrderBlock, Pivot, StructureBreak


class PineSwingOBEngine:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.candles: list[Candle] = []
        self.parsed_highs: list[float] = []
        self.parsed_lows: list[float] = []
        self.true_ranges: list[float] = []
        self.atr_values: list[float | None] = []
        self.leg = 0  # Pine initializes leg() to bearish leg
        self.trend = 0
        self.swing_high: Pivot | None = None
        self.swing_low: Pivot | None = None
        self.order_blocks: list[OrderBlock] = []
        self.last_time: str | None = None

    def process(self, candle: Candle) -> tuple[list[StructureBreak], list[OrderBlock], list[str]]:
        if self.last_time is not None and candle.time <= self.last_time:
            return [], [], []
        i = len(self.candles)
        prev_close = self.candles[-1].close if self.candles else candle.close
        tr = max(candle.high - candle.low, abs(candle.high - prev_close), abs(candle.low - prev_close))
        self.true_ranges.append(tr)
        atr = self._next_atr(tr)
        self.atr_values.append(atr)
        volatile = atr is not None and candle.high - candle.low >= 2 * atr
        self.parsed_highs.append(candle.low if volatile else candle.high)
        self.parsed_lows.append(candle.high if volatile else candle.low)
        self.candles.append(candle)

        self._update_swing(i)
        breaks: list[StructureBreak] = []
        formed: list[OrderBlock] = []
        if self.swing_high and not self.swing_high.crossed and prev_close <= self.swing_high.level < candle.close:
            kind = "CHoCH" if self.trend == -1 else "BOS"
            br = StructureBreak("bull", kind, self.swing_high.level, i, candle.time)
            ob = self._make_ob(self.swing_high, "bull", i, candle.time, kind)
            self.swing_high.crossed = True
            self.trend = 1
            breaks.append(br)
            if ob:
                self.order_blocks.insert(0, ob); formed.append(ob)
        if self.swing_low and not self.swing_low.crossed and prev_close >= self.swing_low.level > candle.close:
            kind = "CHoCH" if self.trend == 1 else "BOS"
            br = StructureBreak("bear", kind, self.swing_low.level, i, candle.time)
            ob = self._make_ob(self.swing_low, "bear", i, candle.time, kind)
            self.swing_low.crossed = True
            self.trend = -1
            breaks.append(br)
            if ob:
                self.order_blocks.insert(0, ob); formed.append(ob)

        invalidated: list[str] = []
        for ob in self.order_blocks:
            crossed = (ob.direction == "bear" and candle.high > ob.high) or (
                ob.direction == "bull" and candle.low < ob.low)
            if ob.active and crossed:
                ob.active = False
                invalidated.append(ob.id)
        self.order_blocks = self.order_blocks[:100]
        self.last_time = candle.time
        return breaks, formed, invalidated

    def _next_atr(self, tr: float) -> float | None:
        n = self.cfg.atr_period
        count = len(self.true_ranges)
        if count < n:
            return None
        if count == n:
            return sum(self.true_ranges) / n
        previous = self.atr_values[-1]
        assert previous is not None
        return (previous * (n - 1) + tr) / n

    def _update_swing(self, i: int) -> None:
        n = self.cfg.trend_swing_length
        if i < n:
            return
        candidate = i - n
        window = self.candles[candidate + 1:i + 1]
        new_leg = self.leg
        if window and self.candles[candidate].high > max(x.high for x in window):
            new_leg = 0
        elif window and self.candles[candidate].low < min(x.low for x in window):
            new_leg = 1
        if new_leg == self.leg:
            return
        self.leg = new_leg
        c = self.candles[candidate]
        if new_leg == 1:
            self.swing_low = Pivot(c.low, candidate, c.time)
        else:
            self.swing_high = Pivot(c.high, candidate, c.time)

    def _make_ob(self, pivot: Pivot, direction: str, i: int, formed_time: str,
                 break_kind: str) -> OrderBlock | None:
        # Pine array.slice(start, end) excludes the structure-break candle.
        if pivot.bar_index >= i:
            return None
        values = self.parsed_lows if direction == "bull" else self.parsed_highs
        segment = values[pivot.bar_index:i]
        if not segment:
            return None
        extreme = min(segment) if direction == "bull" else max(segment)
        source = pivot.bar_index + segment.index(extreme)
        c = self.candles[source]
        return OrderBlock(uuid4().hex, direction, self.parsed_highs[source],
                          self.parsed_lows[source], source, c.time, i, formed_time,
                          True, break_kind, self.atr_values[i])

    def dump_state(self, max_candles: int | None = None) -> dict:
        start = 0
        blocks = self.order_blocks
        if max_candles is not None and len(self.candles) > max_candles:
            start = len(self.candles) - max_candles
            blocks = [item for item in self.order_blocks if item.active]
            relevant = [item.bar_index for item in (self.swing_high, self.swing_low) if item]
            relevant.extend(item.source_index for item in blocks)
            relevant.extend(item.formed_index for item in blocks)
            if relevant:
                start = min(start, min(relevant))

        def pivot_state(value):
            if value is None:
                return None
            item = asdict(value); item["bar_index"] -= start
            return item

        block_states = []
        for value in blocks:
            item = asdict(value)
            item["source_index"] -= start
            item["formed_index"] -= start
            block_states.append(item)
        return {
            "candles": [asdict(x) for x in self.candles[start:]],
            "parsed_highs": self.parsed_highs[start:], "parsed_lows": self.parsed_lows[start:],
            "true_ranges": self.true_ranges[start:], "atr_values": self.atr_values[start:],
            "leg": self.leg, "trend": self.trend,
            "swing_high": pivot_state(self.swing_high), "swing_low": pivot_state(self.swing_low),
            "order_blocks": block_states, "last_time": self.last_time,
            "index_offset": start,
        }

    def load_state(self, data: dict) -> None:
        self.candles = [Candle(**x) for x in data.get("candles", [])]
        self.parsed_highs = data.get("parsed_highs", [])
        self.parsed_lows = data.get("parsed_lows", [])
        self.true_ranges = data.get("true_ranges", [])
        self.atr_values = data.get("atr_values", [])
        self.leg, self.trend = data.get("leg", 0), data.get("trend", 0)
        self.swing_high = Pivot(**data["swing_high"]) if data.get("swing_high") else None
        self.swing_low = Pivot(**data["swing_low"]) if data.get("swing_low") else None
        self.order_blocks = [OrderBlock(**x) for x in data.get("order_blocks", [])]
        self.last_time = data.get("last_time")
