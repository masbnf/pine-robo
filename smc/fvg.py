"""Fair Value Gaps (3-candle imbalance).

Bullish FVG: gap between candle[i-1].high and candle[i+1].low (low > prev high).
Bearish FVG: gap between candle[i-1].low and candle[i+1].high (high < prev low).
The gap is anchored to the middle candle index `i`.
"""
from __future__ import annotations
from typing import List
import pandas as pd

from .types import FVG, Dir


def find_fvgs(df: pd.DataFrame) -> List[FVG]:
    """Detect all FVGs in the window."""
    out: List[FVG] = []
    idx = df.index.to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()

    for k in range(1, len(df) - 1):
        # bullish: third candle's low above first candle's high
        if low[k + 1] > high[k - 1]:
            out.append(FVG(direction=Dir.BULL, idx=int(idx[k]),
                           top=float(low[k + 1]), bottom=float(high[k - 1])))
        # bearish: third candle's high below first candle's low
        if high[k + 1] < low[k - 1]:
            out.append(FVG(direction=Dir.BEAR, idx=int(idx[k]),
                           top=float(low[k - 1]), bottom=float(high[k + 1])))
    return out


def mark_filled(df: pd.DataFrame, fvgs: List[FVG]) -> List[FVG]:
    """Flag FVGs that later price has traded back through (filled)."""
    for g in fvgs:
        after = df[df.index > g.idx]
        if after.empty:
            continue
        if g.direction == Dir.BULL:
            g.filled = bool((after["low"] <= g.bottom).any())
        else:
            g.filled = bool((after["high"] >= g.top).any())
    return fvgs


def unfilled(fvgs: List[FVG], direction: Dir) -> List[FVG]:
    return [g for g in fvgs if not g.filled and g.direction == direction]
