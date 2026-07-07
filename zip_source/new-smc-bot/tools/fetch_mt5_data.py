#!/usr/bin/env python3
"""
Download REAL XAUUSD M5 candles from a running MetaTrader 5 terminal and write
them to the CSV the backtest reads (config.BACKTEST['data_file']).

Requirements (Windows only — MT5 has no Linux/Mac python package):
    pip install MetaTrader5 pandas
    MetaTrader 5 terminal must be installed and running / logged in.

Usage:
    python tools/fetch_mt5_data.py

All settings (login, server, symbol, bars) live in config/settings.py -> MT5.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timedelta, timezone

# make the project root importable when run as `python tools/fetch_mt5_data.py`
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[!] MetaTrader5 package not installed.")
    print("    Run:  pip install MetaTrader5")
    print("    (Windows only — the MT5 python API does not exist on Linux/Mac.)")
    sys.exit(1)

import pandas as pd
from config import settings as cfg


TF_MAP = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}


def connect() -> bool:
    m = cfg.MT5
    kwargs = dict(login=int(m["login"]), password=m["password"], server=m["server"])
    if m.get("terminal_path"):
        ok = mt5.initialize(m["terminal_path"], **kwargs)
    else:
        ok = mt5.initialize(**kwargs)
    if not ok:
        print(f"[!] mt5.initialize failed: {mt5.last_error()}")
        print("    Make sure the MT5 terminal is open and you are logged into the account.")
        return False
    info = mt5.account_info()
    if info is not None:
        print(f"[+] Connected: login {info.login} | {info.server} | balance {info.balance}")
    return True


def resolve_symbol(want: str) -> str | None:
    """Return a tradable symbol name, falling back to any XAU* symbol."""
    if mt5.symbol_select(want, True) and mt5.symbol_info(want) is not None:
        return want
    # fallback: search broker's symbol list
    for s in (mt5.symbols_get() or []):
        if "XAU" in s.name.upper():
            if mt5.symbol_select(s.name, True):
                print(f"[i] '{want}' not found — using '{s.name}' instead.")
                return s.name
    return None


def fetch() -> int:
    m = cfg.MT5
    if not connect():
        return 1

    try:
        symbol = resolve_symbol(m["symbol"])
        if symbol is None:
            print(f"[!] No XAUUSD-like symbol found on {m['server']}.")
            return 1

        tf = TF_MAP.get(m["timeframe"].upper(), mt5.TIMEFRAME_M5)
        lookback_days = m.get("lookback_days")
        date_from, date_to = m.get("date_from"), m.get("date_to")

        if date_from and date_to:
            # explicit DATE RANGE (out-of-sample tests)
            utc_from = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            utc_to = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
            rates = mt5.copy_rates_range(symbol, tf, utc_from, utc_to)
            mode = f"{date_from} -> {date_to}"
        elif lookback_days:
            # fetch by DATE range: the last N days up to now
            utc_to = datetime.now(timezone.utc)
            utc_from = utc_to - timedelta(days=int(lookback_days))
            rates = mt5.copy_rates_range(symbol, tf, utc_from, utc_to)
            mode = f"last {lookback_days} days"
        else:
            # fetch by COUNT: the most recent N bars
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, int(m["bars"]))
            mode = f"{m['bars']} bars"

        if rates is None or len(rates) == 0:
            print(f"[!] No data returned ({mode}): {mt5.last_error()}")
            return 1

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        vol_col = "tick_volume" if "tick_volume" in df.columns else None
        cols = ["time", "open", "high", "low", "close"] + ([vol_col] if vol_col else [])
        out = df[cols].rename(columns={vol_col: "volume"} if vol_col else {})

        out_rel = m.get("out_file") or cfg.BACKTEST["data_file"]
        out_path = os.path.join(ROOT, out_rel)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        out.to_csv(out_path, index=False)

        print(f"[+] {symbol} {m['timeframe']} ({mode}): wrote {len(out)} candles -> {out_path}")
        print(f"    range: {out['time'].iloc[0]}  ->  {out['time'].iloc[-1]}")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(fetch())
