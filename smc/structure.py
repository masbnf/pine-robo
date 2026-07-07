"""Market structure: swings, BOS (break of structure), CHoCH (change of character).

All functions take a window DataFrame (already sliced to the right lookback by the
strategy) with columns: open, high, low, close. The DataFrame's index is the
absolute candle position so detected objects carry global indices.
"""
from __future__ import annotations
from typing import List, Optional
import pandas as pd

from .types import Swing, BOS, CHoCH, Dir


def find_swings(df: pd.DataFrame, left: int, right: int) -> List[Swing]:
    """Fractal swing detection.

    A swing high = candle whose high is >= the `left` highs before and
    `right` highs after it (strictly greater on at least one side). Mirror
    for swing lows. Returns swings in chronological order.
    """
    swings: List[Swing] = []
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    idx = df.index.to_numpy()
    n = len(df)

    for i in range(left, n - right):
        wl, wr = slice(i - left, i), slice(i + 1, i + 1 + right)
        # swing high
        if highs[i] >= highs[wl].max() and highs[i] >= highs[wr].max() \
                and (highs[i] > highs[wl].max() or highs[i] > highs[wr].max()):
            swings.append(Swing(idx=int(idx[i]), price=float(highs[i]), kind="high"))
        # swing low
        if lows[i] <= lows[wl].min() and lows[i] <= lows[wr].min() \
                and (lows[i] < lows[wl].min() or lows[i] < lows[wr].min()):
            swings.append(Swing(idx=int(idx[i]), price=float(lows[i]), kind="low"))

    swings.sort(key=lambda s: s.idx)
    return swings


def last_swing(swings: List[Swing], kind: str) -> Optional[Swing]:
    for s in reversed(swings):
        if s.kind == kind:
            return s
    return None


def detect_bos(df: pd.DataFrame, swings: List[Swing], min_body: float = 0.0) -> Optional[BOS]:
    """Detect the most recent break of structure within the window.

    Bullish BOS: a candle closes above the most recent swing high that pre-dates it.
    Bearish BOS: a candle closes below the most recent swing low that pre-dates it.
    `min_body` (price units) rejects weak 1-tick breaks: the breaking candle's body
    (|close-open|) must be at least this big to count. Returns the latest BOS, or None.
    """
    closes = df["close"]
    opens = df["open"]
    result: Optional[BOS] = None

    for pos in df.index:
        c = float(closes.loc[pos])
        body = abs(c - float(opens.loc[pos]))
        if body < min_body:
            continue
        prior = [s for s in swings if s.idx < pos]
        if not prior:
            continue
        sh = last_swing(prior, "high")
        sl = last_swing(prior, "low")
        if sh and c > sh.price:
            result = BOS(direction=Dir.BULL, broken_swing=sh,
                         break_idx=int(pos), impulse_start=_impulse_start(df, sh.idx, pos))
        if sl and c < sl.price:
            result = BOS(direction=Dir.BEAR, broken_swing=sl,
                         break_idx=int(pos), impulse_start=_impulse_start(df, sl.idx, pos))
    return result


def detect_choch(df: pd.DataFrame, swings: List[Swing], bias: Dir,
                 after_idx: Optional[int] = None) -> Optional[CHoCH]:
    """Detect the MOST RECENT change of character in the direction of `bias`.

    For a BUY bias we want a *bullish* CHoCH: price closes above the last minor
    swing high (the micro down-move ends). Mirror for SELL.

    Iterates in REVERSE so the first match is the freshest CHoCH (not a stale one
    from 10+ bars ago). If `after_idx` is given, only CHoCH events strictly after
    that bar count — used so the CHoCH must form AFTER the zone was armed.
    """
    closes = df["close"]
    for pos in reversed(list(df.index)):
        if after_idx is not None and pos <= after_idx:
            break
        c = float(closes.loc[pos])
        prior = [s for s in swings if s.idx < pos]
        if not prior:
            continue
        if bias == Dir.BULL:
            sh = last_swing(prior, "high")
            if sh and c > sh.price:
                return CHoCH(direction=Dir.BULL, broken_swing=sh, break_idx=int(pos))
        else:
            sl = last_swing(prior, "low")
            if sl and c < sl.price:
                return CHoCH(direction=Dir.BEAR, broken_swing=sl, break_idx=int(pos))
    return None


def _impulse_start(df: pd.DataFrame, swing_idx: int, break_idx: int) -> int:
    """Approximate where the breaking impulse began: the swing pivot index."""
    return int(swing_idx)
