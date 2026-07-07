"""Liquidity: buy-side (BSL) above swing highs, sell-side (SSL) below swing lows.

Also detects whether a pool has been swept (price wicked beyond it then closed
back). Used both as an entry filter (sweep before entry) and as a TP target.
"""
from __future__ import annotations
from typing import List, Optional
import pandas as pd

from .types import Liquidity, Swing, Dir


def find_liquidity(swings: List[Swing]) -> List[Liquidity]:
    """Turn swing highs/lows into BSL/SSL pools."""
    pools: List[Liquidity] = []
    for s in swings:
        if s.kind == "high":
            pools.append(Liquidity(kind="BSL", price=s.price, idx=s.idx))
        else:
            pools.append(Liquidity(kind="SSL", price=s.price, idx=s.idx))
    return pools


def detect_sweep(df: pd.DataFrame, pools: List[Liquidity], kind: str) -> Optional[Liquidity]:
    """Return the most recent swept pool of the given kind, else None.

    A BSL sweep: a candle's high pierces the pool price but it closes back below.
    An SSL sweep: a candle's low pierces below the pool price but closes back above.
    """
    swept: Optional[Liquidity] = None
    for p in pools:
        if p.kind != kind:
            continue
        after = df[df.index > p.idx]
        if after.empty:
            continue
        if kind == "BSL":
            hit = after[(after["high"] > p.price) & (after["close"] < p.price)]
        else:
            hit = after[(after["low"] < p.price) & (after["close"] > p.price)]
        if not hit.empty:
            p.swept = True
            if swept is None or p.idx > swept.idx:
                swept = p
    return swept


def next_target(pools: List[Liquidity], price: float, direction: Dir,
                min_distance: float = 0.0) -> Optional[float]:
    """Nearest opposing liquidity to use as TP, at least `min_distance` away.

    BUY  -> nearest BSL above price that is >= price + min_distance.
    SELL -> nearest SSL below price that is <= price - min_distance.

    `min_distance` lets the caller skip liquidity that's too close (which would
    give a sub-minimum reward:risk) and reach for the next real target instead.
    Returns None if no pool is far enough.
    """
    # skip already-swept pools: consumed liquidity is no longer a magnet
    if direction == Dir.BULL:
        cand = sorted(p.price for p in pools
                      if p.kind == "BSL" and not p.swept
                      and p.price >= price + min_distance)
        return cand[0] if cand else None          # nearest qualifying above
    else:
        cand = sorted((p.price for p in pools
                       if p.kind == "SSL" and not p.swept
                       and p.price <= price - min_distance),
                      reverse=True)
        return cand[0] if cand else None          # nearest qualifying below
