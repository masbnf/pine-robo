"""Supply / Demand zones.

A zone is the order-block BODY plus a small ATR buffer on the far side. This is
much tighter than spanning the whole origin window (which was ~2.7x too wide and
tapped too easily).
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

from .types import Zone, OrderBlock, Dir


def zone_from_ob(df: pd.DataFrame, ob: OrderBlock, atr_value: float,
                 atr_mult: float = 0.5) -> Optional[Zone]:
    """Zone = OB body +/- (atr_mult * ATR) buffer on the far side.

    BULL (demand): top = ob.top, bottom = ob.bottom - buffer.
    BEAR (supply): top = ob.top + buffer, bottom = ob.bottom.
    The buffer is clamped to the OB's own wick so the zone never exceeds the
    candle's real price range. `df` is unused now but kept for signature stability.
    """
    buf = max(atr_value, 0.0) * atr_mult
    if ob.direction == Dir.BULL:
        top = ob.top
        bottom = max(ob.bottom - buf, ob.wick_low if ob.wick_low else ob.bottom - buf)
    else:
        top = min(ob.top + buf, ob.wick_high if ob.wick_high else ob.top + buf)
        bottom = ob.bottom

    if top <= bottom:
        return None
    return Zone(direction=ob.direction, top=top, bottom=bottom, origin_idx=ob.idx)
