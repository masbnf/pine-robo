#!/usr/bin/env python3
"""Generate synthetic XAUUSD M5 OHLC data for smoke-testing the bot.

NOT real market data — just a random walk with occasional impulses so the
structure/BOS/sweep logic has something to bite on.

    python tools/make_sample_data.py
"""
from __future__ import annotations
import os
import numpy as np
import pandas as pd


def make(n: int = 4000, start_price: float = 2350.0, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    times = pd.date_range("2025-01-01", periods=n, freq="5min", tz="UTC")

    price = start_price
    rows = []
    for t in times:
        drift = rng.normal(0, 0.4)
        if rng.random() < 0.03:                 # occasional impulse
            drift += rng.choice([-1, 1]) * rng.uniform(2, 6)
        o = price
        c = price + drift
        hi = max(o, c) + abs(rng.normal(0, 0.5))
        lo = min(o, c) - abs(rng.normal(0, 0.5))
        rows.append((t, round(o, 2), round(hi, 2), round(lo, 2), round(c, 2)))
        price = c

    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(here, "data", "XAUUSD_M5.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df = make()
    df.to_csv(out, index=False)
    print(f"Wrote {len(df)} rows -> {out}")
