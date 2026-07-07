"""Market structure: swings, BOS (break of structure), CHoCH (change of character).

All functions take a window DataFrame (already sliced to the right lookback by the
strategy) with columns: open, high, low, close. The DataFrame's index is the
absolute candle position so detected objects carry global indices.
"""
from __future__ import annotations
from typing import List, Optional
import pandas as pd

from .types import Swing, BOS, CHoCH, Dir


def structure_events(df: pd.DataFrame, left: int, right: int) -> list[dict]:
    """Return confirmed BOS/CHoCH events without repainting.

    A pivot becomes usable only ``right`` candles after its origin. Each pivot
    can break once. A break against the current bias is CHoCH; otherwise BOS.
    The close—not the wick—must cross the level.
    """
    swings = find_swings(df, left, right)
    if not swings:
        return []

    index_values = df.index.to_numpy()
    positions = {int(idx): pos for pos, idx in enumerate(index_values)}
    confirmed_at: dict[int, list[Swing]] = {}
    for swing in swings:
        origin_pos = positions.get(swing.idx)
        if origin_pos is None or origin_pos + right >= len(df):
            continue
        confirm_pos = origin_pos + right
        confirmed_at.setdefault(confirm_pos, []).append(swing)

    active_high = active_low = None
    high_crossed = low_crossed = True
    bias: Optional[Dir] = None
    events: list[dict] = []

    closes = df["close"].to_numpy()
    for pos, close_value in enumerate(closes):
        idx = int(index_values[pos])
        for swing in confirmed_at.get(pos, []):
            if swing.kind == "high":
                active_high, high_crossed = swing, False
            else:
                active_low, low_crossed = swing, False

        close = float(close_value)
        if active_high is not None and not high_crossed and close > active_high.price:
            tag = "CHoCH" if bias == Dir.BEAR else "BOS"
            events.append({"direction": Dir.BULL, "tag": tag,
                           "pivot": active_high, "break_idx": idx})
            high_crossed, bias = True, Dir.BULL
        if active_low is not None and not low_crossed and close < active_low.price:
            tag = "CHoCH" if bias == Dir.BULL else "BOS"
            events.append({"direction": Dir.BEAR, "tag": tag,
                           "pivot": active_low, "break_idx": idx})
            low_crossed, bias = True, Dir.BEAR

    return events


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


def detect_bos(df: pd.DataFrame, swings: List[Swing]) -> Optional[BOS]:
    """Detect the most recent break of structure within the window.

    Bullish BOS: a candle closes above the most recent swing high that pre-dates it.
    Bearish BOS: a candle closes below the most recent swing low that pre-dates it.
    Returns the latest BOS found, or None.
    """
    closes = df["close"]
    result: Optional[BOS] = None

    for pos in df.index:
        c = float(closes.loc[pos])
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


def detect_choch(df: pd.DataFrame, swings: List[Swing], bias: Dir) -> Optional[CHoCH]:
    """Detect a change of character in the direction of `bias`.

    For a BUY bias we want a *bullish* CHoCH: price closes above the last
    minor swing high (the micro down-move ends). Mirror for SELL.
    """
    closes = df["close"]
    for pos in df.index:
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
