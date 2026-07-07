"""Data loading & timeframe resampling.

Input: an M5 OHLC CSV (columns: time, open, high, low, close[, volume]).
We resample M5 -> M15 once up front, then the backtest walks both series in
lockstep using FIXED-SIZE rolling buffers (see backtest/engine.py) to avoid the
O(n^2) growing-slice trap.
"""
from __future__ import annotations
import os
import pandas as pd


def load_m5(path: str, datetime_col: str = "time", tz: str = "UTC") -> pd.DataFrame:
    """Load an M5 OHLC CSV into a clean, integer-indexed DataFrame."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Data file not found: {path}")

    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    dt = datetime_col.lower()

    if dt not in df.columns:
        raise ValueError(f"datetime column '{datetime_col}' not in {list(df.columns)}")

    df[dt] = pd.to_datetime(df[dt], utc=True, errors="coerce")
    df = df.dropna(subset=[dt]).sort_values(dt).reset_index(drop=True)

    needed = {"open", "high", "low", "close"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"missing OHLC columns: {missing}")

    df = df.rename(columns={dt: "time"})
    keep = ["time", "open", "high", "low", "close"]
    if "volume" in df.columns:
        keep.append("volume")
    return df[keep].reset_index(drop=True)


def resample(df_m5: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    """Resample M5 candles up to a higher timeframe (default M15)."""
    g = df_m5.set_index("time")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in g.columns:
        agg["volume"] = "sum"
    out = (g.resample(f"{minutes}min", label="left", closed="left")
             .agg(agg)
             .dropna(subset=["open", "high", "low", "close"]))
    return out.reset_index()


def align_index(df_htf: pd.DataFrame, ts, htf_minutes: int = 15,
                ltf_minutes: int = 5) -> int:
    """Return the latest *closed* HTF candle at the close of the LTF bar.

    HTF rows are labelled by their opening time.  Since the resampled frame is
    built up-front, selecting ``time <= ts`` would expose the final high/low of
    the still-forming HTF candle.  The cutoff below is its latest legal open.
    """
    ltf_close = pd.Timestamp(ts) + pd.Timedelta(minutes=ltf_minutes)
    latest_closed_open = ltf_close - pd.Timedelta(minutes=htf_minutes)
    # Datetimes are sorted by load/resample; binary search avoids scanning the
    # entire HTF frame on every LTF bar (which would make the run O(n^2)).
    pos = int(df_htf["time"].searchsorted(latest_closed_open, side="right")) - 1
    return pos
