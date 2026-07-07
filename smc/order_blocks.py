"""Order Block detection.

The valid OB is the LAST opposing candle in the pre-impulse window before the
move that caused the BOS. We search only within `window` candles preceding the
impulse so we never grab stale blocks (per the MTF spec).
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

from .types import OrderBlock, Dir


def find_order_block(df: pd.DataFrame, impulse_start: int,
                     direction: Dir, window: int) -> Optional[OrderBlock]:
    """Find the order block feeding an impulse.

    direction = Dir.BULL  -> demand OB  -> last *bearish* candle before the up-move
    direction = Dir.BEAR  -> supply OB  -> last *bullish* candle before the down-move

    Searches the `window` candles up to and including `impulse_start`.
    """
    lo = max(df.index.min(), impulse_start - window)
    seg = df.loc[lo:impulse_start]
    if seg.empty:
        return None

    if direction == Dir.BULL:
        opposing = seg[seg["close"] < seg["open"]]   # bearish candles
    else:
        opposing = seg[seg["close"] > seg["open"]]   # bullish candles

    if opposing.empty:
        return None

    ob_idx = opposing.index.max()                    # the LAST opposing candle
    row = df.loc[ob_idx]
    o, c = float(row["open"]), float(row["close"])
    # SMC order block = the candle BODY (open<->close), not the full wick range,
    # which on XAUUSD M15 is ~2x too wide and taps too easily.
    return OrderBlock(
        direction=direction,
        idx=int(ob_idx),
        top=max(o, c),
        bottom=min(o, c),
        wick_high=float(row["high"]),   # full range kept for context
        wick_low=float(row["low"]),
    )
