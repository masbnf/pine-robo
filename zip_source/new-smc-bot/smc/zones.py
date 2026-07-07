"""Supply / Demand zones.

A zone is built from the order block's origin window. We extend the OB band to
the extreme of the origin window so the zone reflects the full imbalance origin
(same 10-15 candle window as the OB per spec).
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

from .types import Zone, OrderBlock, Dir


def zone_from_ob(df: pd.DataFrame, ob: OrderBlock, window: int) -> Optional[Zone]:
    """Build a supply/demand Zone around an order block."""
    lo = max(df.index.min(), ob.idx - window)
    seg = df.loc[lo:ob.idx]
    if seg.empty:
        return None

    if ob.direction == Dir.BULL:        # demand zone: from OB low up to OB open/high
        top = float(ob.top)
        bottom = float(seg["low"].min())
    else:                                # supply zone
        top = float(seg["high"].max())
        bottom = float(ob.bottom)

    return Zone(direction=ob.direction, top=top, bottom=bottom, origin_idx=ob.idx)
