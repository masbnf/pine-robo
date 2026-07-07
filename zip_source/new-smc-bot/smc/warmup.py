"""Closed-candle H1/M15 warm-up state derived from the supplied Pine script."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import pandas as pd

from .types import Dir, OrderBlock


@dataclass
class LiquidityZone:
    kind: str                 # BSL (high/sell liquidity) or SSL (low/buy liquidity)
    price: float
    created_idx: int
    score: int = 1
    sweeps: int = 0
    in_touch: bool = False
    in_sweep: bool = False        # one continuous penetration = one sweep episode


@dataclass
class WarmupOB:
    direction: Dir
    top: float
    bottom: float
    created_idx: int


class WarmupState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_idx: Optional[int] = None
        self.rows: list[dict] = []
        self.h1_trend = 0
        self.h1_max = None
        self.h1_min = None
        self.liquidity: list[LiquidityZone] = []
        self.order_blocks: list[WarmupOB] = []
        self.choch_levels: list[tuple[Dir, float, int]] = []
        self._pivot_blocks: dict[tuple[str, int], list[tuple[float, int]]] = {}
        self._active_ob_block = None
        self._last_bear = self._last_bull = None
        self._h1_current = None
        self._h1_bars: list[dict] = []
        self._h1_high_pivots: list[float] = []
        self._h1_low_pivots: list[float] = []

    def update(self, htf: pd.DataFrame) -> None:
        """Consume every unseen, already-closed M15 candle exactly once."""
        if htf.empty:
            return
        if self.last_idx is not None and int(htf.index[-1]) <= self.last_idx:
            return
        if self.last_idx is None:
            unseen = htf
        else:
            start = int(htf.index.searchsorted(self.last_idx, side="right"))
            unseen = htf.iloc[start:]
        for idx, row in unseen.iterrows():
            item = {"idx": int(idx), "time": row["time"],
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"])}
            self.rows.append(item)
            self.rows = self.rows[-1200:]
            self._on_m15(item)
            self.last_idx = int(idx)

    def _on_m15(self, bar: dict) -> None:
        self._update_h1(bar)
        self._update_ob_blocks(bar)
        self._confirm_m15_pivot()
        self._flush_pivot_blocks(bar["idx"])
        self._update_choch(bar)
        self._update_liquidity_lifecycle(bar)

    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows).set_index("idx")

    def _update_h1(self, bar: dict) -> None:
        w = self.cfg
        bucket = pd.Timestamp(bar["time"]).floor("60min")
        if self._h1_current is None or self._h1_current["time"] != bucket:
            if self._h1_current is not None and self._h1_current["count"] == 4:
                self._h1_bars.append(self._h1_current)
                self._h1_bars = self._h1_bars[-(w["h1_lookback"] + 10):]
                self._confirm_h1_pivot()
            self._h1_current = {"time": bucket, "open": bar["open"],
                                "high": bar["high"], "low": bar["low"],
                                "close": bar["close"], "count": 1}
        else:
            self._h1_current["high"] = max(self._h1_current["high"], bar["high"])
            self._h1_current["low"] = min(self._h1_current["low"], bar["low"])
            self._h1_current["close"] = bar["close"]
            self._h1_current["count"] += 1

    def _confirm_h1_pivot(self) -> None:
        s = self.cfg["h1_swing_len"]
        if len(self._h1_bars) < 2 * s + 1:
            return
        k = len(self._h1_bars) - 1 - s
        pivot = self._h1_bars[k]
        window = self._h1_bars[k - s:k + s + 1]
        if pivot["high"] >= max(x["high"] for x in window):
            self._h1_high_pivots.append(pivot["high"])
        if pivot["low"] <= min(x["low"] for x in window):
            self._h1_low_pivots.append(pivot["low"])
        limit = self.cfg["h1_lookback"]
        self._h1_high_pivots = self._h1_high_pivots[-limit:]
        self._h1_low_pivots = self._h1_low_pivots[-limit:]
        self.h1_max = max(self._h1_high_pivots, default=None)
        self.h1_min = min(self._h1_low_pivots, default=None)
        if len(self._h1_high_pivots) >= 2 and len(self._h1_low_pivots) >= 2:
            hh, ph = self._h1_high_pivots[-1], self._h1_high_pivots[-2]
            ll, pl = self._h1_low_pivots[-1], self._h1_low_pivots[-2]
            if hh > ph and ll > pl:
                self.h1_trend = 1
            elif hh < ph and ll < pl:
                self.h1_trend = -1
            elif not self.cfg.get("keep_last_clear_h1_trend", True):
                self.h1_trend = 0

    def _confirm_m15_pivot(self) -> None:
        s = self.cfg["m15_swing_len"]
        if len(self.rows) < 2 * s + 1:
            return
        k = len(self.rows) - 1 - s
        pivot = self.rows[k]
        window = self.rows[k - s:k + s + 1]
        block = pivot["idx"] // self.cfg["m15_block_size"]
        if pivot["high"] >= max(x["high"] for x in window):
            self._pivot_blocks.setdefault(("BSL", block), []).append((pivot["high"], pivot["idx"]))
        if pivot["low"] <= min(x["low"] for x in window):
            self._pivot_blocks.setdefault(("SSL", block), []).append((pivot["low"], pivot["idx"]))

    def _flush_pivot_blocks(self, current_idx: int) -> None:
        confirmed_block = (current_idx - self.cfg["m15_swing_len"]) // self.cfg["m15_block_size"]
        for key in list(self._pivot_blocks):
            kind, block = key
            if block >= confirmed_block:
                continue
            candidates = self._pivot_blocks.pop(key)
            chosen = max(candidates) if kind == "BSL" else min(candidates)
            self._register_liquidity(kind, float(chosen[0]), int(chosen[1]))

    def _register_liquidity(self, kind: str, price: float, idx: int) -> None:
        prox = self.cfg["merge_threshold"]
        nearby = [z for z in self.liquidity if z.kind == kind and abs(z.price - price) <= prox]
        if nearby:
            strongest = max(nearby, key=lambda z: z.price) if kind == "BSL" else min(nearby, key=lambda z: z.price)
            stronger_new = price > strongest.price if kind == "BSL" else price < strongest.price
            if not stronger_new:
                return
            self.liquidity = [z for z in self.liquidity if z not in nearby]
        self.liquidity.append(LiquidityZone(kind, price, idx))
        same = [z for z in self.liquidity if z.kind == kind]
        if len(same) > self.cfg["max_zones"]:
            self.liquidity.remove(min(same, key=lambda z: z.created_idx))

    def _update_ob_blocks(self, bar: dict) -> None:
        block = bar["idx"] // self.cfg["m15_block_size"]
        if self._active_ob_block is None:
            self._active_ob_block = block
        if block != self._active_ob_block:
            if self._last_bear:
                b = self._last_bear
                self.order_blocks.append(WarmupOB(Dir.BULL, max(b["open"], b["close"]),
                                                   min(b["open"], b["close"]), b["idx"]))
            if self._last_bull:
                b = self._last_bull
                self.order_blocks.append(WarmupOB(Dir.BEAR, max(b["open"], b["close"]),
                                                   min(b["open"], b["close"]), b["idx"]))
            self.order_blocks = self.order_blocks[-self.cfg["max_ob_zones"]:]
            self._last_bear = self._last_bull = None
            self._active_ob_block = block
        if bar["close"] < bar["open"]:
            self._last_bear = bar
        elif bar["close"] > bar["open"]:
            self._last_bull = bar

    def _update_choch(self, bar: dict) -> None:
        n = self.cfg["m15_block_size"]
        if len(self.rows) <= n:
            return
        prior = self.rows[-n - 1:-1]
        high, low = max(x["high"] for x in prior), min(x["low"] for x in prior)
        prev_close = self.rows[-2]["close"]
        if bar["close"] > high and prev_close <= high:
            self.choch_levels.append((Dir.BULL, high, bar["idx"]))
        if bar["close"] < low and prev_close >= low:
            self.choch_levels.append((Dir.BEAR, low, bar["idx"]))
        self.choch_levels = [(d, p, i) for d, p, i in self.choch_levels
                             if not ((d == Dir.BULL and bar["close"] < p) or
                                     (d == Dir.BEAR and bar["close"] > p))][-100:]

    def _update_liquidity_lifecycle(self, bar: dict) -> None:
        prox = self.cfg["merge_threshold"]
        for z in list(self.liquidity):
            touching = ((z.kind == "BSL" and z.price - prox <= bar["high"] <= z.price) or
                        (z.kind == "SSL" and z.price <= bar["low"] <= z.price + prox))
            if touching and not z.in_touch:
                z.score += 1
            z.in_touch = touching
            penetrating = ((z.kind == "BSL" and bar["high"] > z.price) or
                           (z.kind == "SSL" and bar["low"] < z.price)) and bar["idx"] > z.created_idx
            new_sweep = penetrating and not z.in_sweep
            z.in_sweep = penetrating
            if not new_sweep:
                continue
            near_h1 = ((self.h1_max is not None and abs(z.price - self.h1_max) <= self.cfg["h1_proximity"]) or
                       (self.h1_min is not None and abs(z.price - self.h1_min) <= self.cfg["h1_proximity"]))
            max_sweeps = (self.cfg.get("near_h1_max_sweeps", 4) if near_h1 else
                          self.cfg.get("touched_max_sweeps", 3) if z.score >= self.cfg["min_score_extra_life"] else
                          self.cfg.get("base_max_sweeps", 2))
            z.sweeps += 1
            if z.sweeps >= max_sweeps:
                self.liquidity.remove(z)

    def trend_allows(self, direction: Dir) -> bool:
        # H1 is a veto, not an entry trigger: range permits both directions;
        # a clear trend blocks only the counter-trend side.
        return self.h1_trend == 0 or self.h1_trend == (1 if direction == Dir.BULL else -1)

    def zone_allows(self, direction: Dir, block: OrderBlock) -> bool:
        prox = self.cfg["merge_threshold"]
        wanted = "SSL" if direction == Dir.BULL else "BSL"
        liquid = any(z.kind == wanted and block.bottom - prox <= z.price <= block.top + prox
                     for z in self.liquidity)
        ob_overlap = any(z.direction == direction and not (z.bottom > block.top or z.top < block.bottom)
                         for z in self.order_blocks)
        choch_near = any(d == direction and block.bottom - prox <= p <= block.top + prox
                         for d, p, _ in self.choch_levels)
        return liquid or ob_overlap or choch_near

    def stop_liquidity(self, direction: Dir, entry: float) -> Optional[float]:
        if direction == Dir.BULL:
            levels = [z.price for z in self.liquidity if z.kind == "SSL" and z.price < entry]
            return max(levels) if levels else None
        levels = [z.price for z in self.liquidity if z.kind == "BSL" and z.price > entry]
        return min(levels) if levels else None

    def target_liquidity(self, direction: Dir, entry: float) -> Optional[float]:
        if direction == Dir.BULL:
            levels = [z.price for z in self.liquidity if z.kind == "BSL" and z.price > entry]
            return min(levels) if levels else None
        levels = [z.price for z in self.liquidity if z.kind == "SSL" and z.price < entry]
        return max(levels) if levels else None
