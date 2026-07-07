"""Order Block detection.

The valid OB is the LAST opposing candle in the pre-impulse window before the
move that caused the BOS. We search only within `window` candles preceding the
impulse so we never grab stale blocks (per the MTF spec).
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

from .types import OrderBlock, Dir


def find_extreme_order_block(df: pd.DataFrame, pivot_idx: int, break_idx: int,
                             direction: Dir, atr_period: int = 200) -> Optional[OrderBlock]:
    """LuxAlgo-style OB used by the clean Pine logic.

    Bullish breaks select the lowest parsed low between pivot and break;
    bearish breaks select the highest parsed high.  The breaking candle is
    excluded because it was not known before the close that confirms the break.
    Large-range candles are normalised with the same high/low swap concept used
    by the Pine script.  Returned bounds are always ordered.
    """
    segment = df[(df.index >= pivot_idx) & (df.index < break_idx)].copy()
    if segment.empty:
        return None

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    measure = tr.rolling(atr_period, min_periods=1).mean()
    volatile = (df["high"] - df["low"]) >= 2.0 * measure
    parsed_high = df["high"].where(~volatile, df["low"])
    parsed_low = df["low"].where(~volatile, df["high"])

    if direction == Dir.BULL:
        ob_idx = int(parsed_low.loc[segment.index].idxmin())
    else:
        ob_idx = int(parsed_high.loc[segment.index].idxmax())

    top = max(float(parsed_high.loc[ob_idx]), float(parsed_low.loc[ob_idx]))
    bottom = min(float(parsed_high.loc[ob_idx]), float(parsed_low.loc[ob_idx]))
    return OrderBlock(direction=direction, idx=ob_idx, top=top, bottom=bottom)


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
    return OrderBlock(
        direction=direction,
        idx=int(ob_idx),
        top=float(row["high"]),
        bottom=float(row["low"]),
    )
