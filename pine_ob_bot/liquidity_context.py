"""Observational swing-liquidity tracker. It never accepts/rejects a trade."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import uuid4

from .models import Candle


@dataclass(slots=True)
class LiquidityPool:
    id: str
    kind: str                 # BSL above swing high, SSL below swing low
    level: float
    created_index: int
    created_time: str
    state: str = "active"
    event_index: int | None = None
    event_time: str | None = None
    event_depth: float | None = None
    event_atr: float | None = None


class LiquidityTracker:
    def __init__(self, swing_length: int = 10, atr_period: int = 200):
        self.swing_length, self.atr_period = swing_length, atr_period
        self.candles: list[Candle] = []
        self.true_ranges: list[float] = []
        self.atr_values: list[float | None] = []
        self.leg = 0
        self.pools: list[LiquidityPool] = []
        self.last_event: dict[str, LiquidityPool | None] = {"BSL": None, "SSL": None}

    def process(self, candle: Candle) -> None:
        i = len(self.candles)
        previous = self.candles[-1].close if self.candles else candle.close
        tr = max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous))
        self.true_ranges.append(tr)
        self.atr_values.append(self._atr(tr))
        self.candles.append(candle)
        # Only pools known before this close may generate an event on this bar.
        for pool in self.pools:
            if pool.state not in ("active", "touched"):
                continue
            if pool.kind == "BSL" and candle.high >= pool.level:
                if candle.high > pool.level and candle.close < pool.level:
                    self._event(pool, "swept", i, candle.time, candle.high - pool.level)
                elif candle.close > pool.level:
                    self._event(pool, "broken", i, candle.time, candle.high - pool.level)
                else:
                    pool.state = "touched"
            elif pool.kind == "SSL" and candle.low <= pool.level:
                if candle.low < pool.level and candle.close > pool.level:
                    self._event(pool, "swept", i, candle.time, pool.level - candle.low)
                elif candle.close < pool.level:
                    self._event(pool, "broken", i, candle.time, pool.level - candle.low)
                else:
                    pool.state = "touched"
        self._confirm_swing(i)
        if len(self.pools) > 500:
            self.pools = self.pools[-500:]

    def _confirm_swing(self, i: int) -> None:
        n = self.swing_length
        if i < n:
            return
        candidate = i - n
        window = self.candles[candidate + 1:i + 1]
        new_leg = self.leg
        kind = None
        if window and self.candles[candidate].high > max(x.high for x in window):
            new_leg, kind = 0, "BSL"
        elif window and self.candles[candidate].low < min(x.low for x in window):
            new_leg, kind = 1, "SSL"
        if new_leg == self.leg or kind is None:
            return
        self.leg = new_leg
        source = self.candles[candidate]
        level = source.high if kind == "BSL" else source.low
        self.pools.append(LiquidityPool(uuid4().hex, kind, level, candidate, source.time))

    def _event(self, pool: LiquidityPool, state: str, index: int, time: str, depth: float) -> None:
        pool.state, pool.event_index, pool.event_time, pool.event_depth = state, index, time, depth
        pool.event_atr = self.atr_values[-1] if self.atr_values else None
        self.last_event[pool.kind] = pool

    def setup_snapshot(self, direction: str, leg_start_index: int | None,
                       bos_index: int) -> dict:
        """Opposite sweep bounded by the structural leg that produced this BOS."""
        kind = "SSL" if direction == "bull" else "BSL"
        candidates = [x for x in self.pools if x.kind == kind and x.state == "swept"
                      and x.event_index is not None and leg_start_index is not None
                      and leg_start_index <= x.event_index <= bos_index]
        event = max(candidates, key=lambda x: x.event_index, default=None)
        leg_bars = bos_index - leg_start_index if leg_start_index is not None else None
        return {
            "setup_m5_opposite_sweep": event is not None,
            "setup_m5_leg_start_index": leg_start_index,
            "setup_m5_leg_bars": leg_bars,
            "setup_m5_sweep_index": event.event_index if event else None,
            "setup_m5_sweep_depth_atr": (event.event_depth / event.event_atr)
            if event and event.event_depth is not None and event.event_atr else None,
            "setup_m5_sweep_leg_position": ((event.event_index - leg_start_index) / leg_bars)
            if event and leg_start_index is not None and leg_bars else None,
        }

    def _atr(self, tr: float) -> float | None:
        count, n = len(self.true_ranges), self.atr_period
        if count < n:
            return None
        if count == n:
            return sum(self.true_ranges) / n
        previous = self.atr_values[-1]
        assert previous is not None
        return (previous * (n - 1) + tr) / n

    def snapshot(self, direction: str, price: float, prefix: str) -> dict:
        i = len(self.candles) - 1
        atr = self.atr_values[-1] if self.atr_values else None
        active_bsl = [x for x in self.pools if x.kind == "BSL" and x.state in ("active", "touched") and x.level > price]
        active_ssl = [x for x in self.pools if x.kind == "SSL" and x.state in ("active", "touched") and x.level < price]
        bsl = min(active_bsl, key=lambda x: x.level, default=None)
        ssl = max(active_ssl, key=lambda x: x.level, default=None)
        opposite = "SSL" if direction == "bull" else "BSL"
        event = self.last_event[opposite]
        data = {
            "nearest_bsl_level": bsl.level if bsl else None,
            "nearest_ssl_level": ssl.level if ssl else None,
            "nearest_bsl_distance_atr": (bsl.level - price) / atr if bsl and atr else None,
            "nearest_ssl_distance_atr": (price - ssl.level) / atr if ssl and atr else None,
            "opposite_last_event": event.state if event else "none",
            "opposite_event_age_bars": i - event.event_index if event and event.event_index is not None else None,
            "opposite_sweep_depth_atr": event.event_depth / atr if event and event.event_depth is not None and atr else None,
            "opposite_sweep_seen": bool(event and event.state == "swept"),
            "active_bsl_count": len(active_bsl), "active_ssl_count": len(active_ssl),
        }
        return {f"{prefix}_{key}": value for key, value in data.items()}

    def dump_state(self, max_candles: int | None = None,
                   index_offset: int | None = None) -> dict:
        start = 0
        pools = self.pools
        if index_offset is not None:
            start = max(0, index_offset)
            last_ids = {item.id for item in self.last_event.values() if item}
            pools = [item for item in self.pools
                     if item.state in ("active", "touched") or item.id in last_ids
                     or (item.event_index is not None and item.event_index >= start)]
        elif max_candles is not None and len(self.candles) > max_candles:
            start = len(self.candles) - max_candles
            last_ids = {item.id for item in self.last_event.values() if item}
            pools = [item for item in self.pools
                     if item.state in ("active", "touched") or item.id in last_ids
                     or (item.event_index is not None and item.event_index >= start)]
            active_created = [item.created_index for item in pools
                              if item.state in ("active", "touched")]
            if active_created:
                start = min(start, min(active_created))
        pool_states = []
        for value in pools:
            item = asdict(value)
            item["created_index"] -= start
            if item.get("event_index") is not None:
                item["event_index"] -= start
            pool_states.append(item)
        return {"candles": [asdict(x) for x in self.candles[start:]],
                "true_ranges": self.true_ranges[start:], "atr_values": self.atr_values[start:],
                "leg": self.leg, "pools": pool_states,
                "last_event_ids": {k: v.id if v else None for k, v in self.last_event.items()}}

    def load_state(self, data: dict) -> None:
        self.candles = [Candle(**x) for x in data.get("candles", [])]
        self.true_ranges, self.atr_values = data.get("true_ranges", []), data.get("atr_values", [])
        self.leg = data.get("leg", 0)
        self.pools = [LiquidityPool(**x) for x in data.get("pools", [])]
        by_id = {x.id: x for x in self.pools}
        ids = data.get("last_event_ids", {})
        self.last_event = {kind: by_id.get(ids.get(kind)) for kind in ("BSL", "SSL")}
