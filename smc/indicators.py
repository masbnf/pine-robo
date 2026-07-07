"""Generic indicators. Currently just ATR; add more here as needed."""
from __future__ import annotations
import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """Wilder-style ATR over the last `period` candles of `df`.

    `df` must have columns: high, low, close. Returns a single float
    (the latest ATR value). Returns 0.0 if not enough data.
    """
    if len(df) < period + 1:
        return 0.0
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    prev_close = np.roll(close, 1)
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    tr = tr[1:]                       # drop first (invalid prev_close)
    return float(pd.Series(tr).rolling(period).mean().iloc[-1])


def ema(df: pd.DataFrame, period: int = 200):
    """Latest EMA of `df['close']`. Returns None if there aren't enough bars."""
    close = df["close"].astype(float)
    if len(close) < period:
        return None
    return float(close.ewm(span=period, adjust=False).mean().iloc[-1])


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder RSI series over `df['close']`. Returns a Series aligned to df.index
    (NaN until enough data). Inspect the last few values for turns/extremes."""
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out[avg_loss == 0.0] = 100.0      # no losses -> RSI 100
    return out
