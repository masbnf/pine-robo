#!/usr/bin/env python3
"""
Entry point. Run the SMC/MTF XAUUSD backtest.

    python run_backtest.py

Everything you tune lives in config/settings.py — you should rarely need to edit
this file. Outputs a summary to the console and writes trades to trades.csv.
"""
from __future__ import annotations
import os
import sys

from config import settings as cfg
from backtest import Backtester


def main() -> int:
    bt = Backtester(cfg)
    try:
        report = bt.run()
    except FileNotFoundError as e:
        print(f"\n[!] {e}")
        print("    Put an M5 OHLC CSV at the path in settings.BACKTEST['data_file'],")
        print("    or run `python tools/make_sample_data.py` to generate test data.\n")
        return 1

    df = bt.trades_dataframe()
    if not df.empty:
        out = "trades.csv"
        df.to_csv(out, index=False)
        print(f"\nWrote {len(df)} trades -> {out}")
    else:
        print("\nNo trades generated. Loosen FILTERS in config/settings.py or check data.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
