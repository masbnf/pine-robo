#!/usr/bin/env python3
"""
LIVE forward-test on MetaTrader 5 (DEMO).

This runs the *exact same* MTFStrategy used by the backtest, so live behaviour
matches your backtested results. Every M5 bar close it pulls fresh candles,
resamples M15, asks the strategy for a signal, and (optionally) places a demo
order with SL/TP attached server-side.

    python run_live.py

SAFETY:
  • config.LIVE['enable_trading'] is False by default -> it only LOGS signals,
    it does NOT place orders. Flip it to True to trade the demo account.
  • config.LIVE['require_demo'] makes the bot refuse to run on a non-demo login.
  • One position at a time, plus the same cooldown as the backtest.
  • Ctrl-C to stop. SL/TP live on the broker server, so they hold even if the
    script is closed.

This script never trades real money on your behalf — YOU run it, on YOUR demo
account, and you can stop it anytime.
"""
from __future__ import annotations
import os
import sys
import time
import random
import logging
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# make the Windows console tolerate non-ASCII log characters
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[!] MetaTrader5 not installed.  pip install MetaTrader5  (Windows only)")
    sys.exit(1)

import pandas as pd
from config import settings as cfg
from data import loader
from strategy import MTFStrategy
from smc.types import Dir
from utils import get_logger

log = get_logger(cfg)
TF = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15}


def attach_live_log():
    """Add a dedicated timestamped file handler for the live session."""
    path = cfg.LIVE.get("log_file")
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    log.info("=" * 60)
    log.info(f"LIVE SESSION START — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


# ----------------------------------------------------------------------
# connection helpers
# ----------------------------------------------------------------------
def connect() -> bool:
    m = cfg.MT5
    kw = dict(login=int(m["login"]), password=m["password"], server=m["server"])
    ok = mt5.initialize(m["terminal_path"], **kw) if m.get("terminal_path") \
        else mt5.initialize(**kw)
    if not ok:
        log.error(f"mt5.initialize failed: {mt5.last_error()}")
        return False

    info = mt5.account_info()
    if info is None:
        log.error("could not read account_info()")
        return False

    is_demo = info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
    log.info(f"Connected: {info.login} | {info.server} | balance {info.balance} | "
             f"{'DEMO' if is_demo else 'REAL'}")
    if cfg.LIVE["require_demo"] and not is_demo:
        log.error("Account is NOT a demo account and require_demo=True -> aborting.")
        return False
    return True


def resolve_symbol(want: str):
    if mt5.symbol_select(want, True) and mt5.symbol_info(want) is not None:
        return want
    for s in (mt5.symbols_get() or []):
        if "XAU" in s.name.upper() and mt5.symbol_select(s.name, True):
            log.info(f"'{want}' not found — using '{s.name}'")
            return s.name
    return None


# ----------------------------------------------------------------------
# data shaping — build stable, epoch-based integer indices so the
# strategy's armed-zone state machine counts bars consistently over time
# ----------------------------------------------------------------------
def get_frames(symbol: str, bars: int):
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, bars)
    if rates is None or len(rates) < 50:
        return None, None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.iloc[:-1]                              # drop the still-forming last bar
    # the broker can return duplicate-timestamp bars -> drop them, else the
    # epoch-based index gets duplicate labels and .loc[pos] returns a Series
    df = df.drop_duplicates(subset="time", keep="last").sort_values("time")

    htf_min = cfg.TIMEFRAMES["htf_minutes"]
    ltf_min = cfg.TIMEFRAMES["ltf_minutes"]
    epoch = pd.Timestamp("1970-01-01", tz="UTC")

    m5 = df[["time", "open", "high", "low", "close"]].copy()
    # bar number since epoch -> monotonic, stable. Use Timedelta floor-division so
    # it is PRECISION-AGNOSTIC (works whether time is datetime64[ns] or [us]); a raw
    # astype("int64")//10**9 breaks on [us] data and collapses adjacent bars.
    m5.index = ((m5["time"] - epoch) // pd.Timedelta(minutes=ltf_min)).astype("int64")
    m5 = m5[~m5.index.duplicated(keep="last")]

    m15 = loader.resample(m5.reset_index(drop=True), htf_min)
    m15.index = ((m15["time"] - epoch) // pd.Timedelta(minutes=htf_min)).astype("int64")
    m15 = m15[~m15.index.duplicated(keep="last")]

    # keep only FULLY-CLOSED M15 candles — identical rule to the backtest's
    # align_index: usable iff T <= last_M5_open - (htf - ltf). This includes the
    # M15 candle that just closed with this M5 bar, but never a forming one.
    cutoff = m5["time"].iloc[-1] - pd.Timedelta(minutes=htf_min - ltf_min)
    m15 = m15[m15["time"] <= cutoff]
    return m5, m15


# ----------------------------------------------------------------------
# order placement
# ----------------------------------------------------------------------
def position_size(symbol: str, balance: float, entry: float, sl: float) -> float:
    info = mt5.symbol_info(symbol)
    risk_money = balance * cfg.RISK["risk_per_trade"]
    dist = abs(entry - sl)
    if dist <= 0 or info is None:
        return info.volume_min if info else 0.01
    # broker-accurate: loss per 1.0 lot = (dist / tick_size) * tick_value
    tick = info.trade_tick_size or cfg.INSTRUMENT["point"]
    tick_val = info.trade_tick_value or cfg.INSTRUMENT["pip_value"]
    loss_per_lot = (dist / tick) * tick_val
    lot = risk_money / loss_per_lot if loss_per_lot > 0 else info.volume_min

    step = info.volume_step or 0.01
    lot = max(info.volume_min, min(lot, info.volume_max, cfg.RISK["max_lot"]))
    lot = round(round(lot / step) * step, 2)
    return max(lot, info.volume_min)


def place_order(symbol: str, sig) -> bool:
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    is_buy = sig.direction == Dir.BULL
    price = tick.ask if is_buy else tick.bid

    bal = mt5.account_info().balance if cfg.LIVE["use_live_balance"] \
        else cfg.RISK["account_balance"]
    lot = position_size(symbol, bal, price, sig.sl)

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": sig.sl,
        "tp": sig.tp,
        "deviation": cfg.LIVE["deviation"],
        "magic": cfg.LIVE["magic"],
        "comment": "smc-mtf",
        "type_time": mt5.ORDER_TIME_GTC,
    }
    for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        req["type_filling"] = filling
        res = mt5.order_send(req)
        if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"ORDER OK  {sig.direction.value.upper()} {lot} @ {res.price} "
                     f"SL {sig.sl} TP {sig.tp}")
            return True
        if res is not None and res.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            log.error(f"order_send failed: retcode {res.retcode} ({res.comment})")
            return False
    log.error("order_send failed for all filling modes")
    return False


def my_positions(symbol: str):
    """All open positions opened by THIS bot (matched by magic number)."""
    return [p for p in (mt5.positions_get(symbol=symbol) or [])
            if p.magic == cfg.LIVE["magic"]]


def _send_with_filling(req):
    """order_send, retrying across filling modes (brokers differ)."""
    res = None
    for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        req["type_filling"] = filling
        res = mt5.order_send(req)
        if res is not None and res.retcode == mt5.TRADE_RETCODE_DONE:
            return res
        if res is not None and res.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            return res                      # a real (non-filling) error
    return res


def close_position(pos):
    tick = mt5.symbol_info_tick(pos.symbol)
    is_buy = pos.type == mt5.POSITION_TYPE_BUY
    price = tick.bid if is_buy else tick.ask
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol, "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": pos.ticket, "price": price, "deviation": cfg.LIVE["deviation"],
        "magic": cfg.LIVE["magic"], "comment": "smc-test-close", "type_time": mt5.ORDER_TIME_GTC,
    }
    return _send_with_filling(req)


def startup_test_trade(symbol: str):
    """Open + immediately close ONE minimum-lot RANDOM trade to prove the live
    execution pipeline works end-to-end (DEMO only). Requires the market open."""
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info is None or tick is None:
        log.warning("startup test: no symbol info/tick — skipping")
        return
    is_buy = random.choice([True, False])
    lot = info.volume_min
    price = tick.ask if is_buy else tick.bid
    log.info(f"STARTUP TEST: opening {'BUY' if is_buy else 'SELL'} {lot} @ {price} ...")
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price, "deviation": cfg.LIVE["deviation"], "magic": cfg.LIVE["magic"],
        "comment": "smc-test", "type_time": mt5.ORDER_TIME_GTC,
    }
    res = _send_with_filling(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        log.warning(f"STARTUP TEST: open FAILED (market closed?) — retcode "
                    f"{getattr(res, 'retcode', None)} {getattr(res, 'comment', '')}")
        return
    log.info(f"STARTUP TEST: opened @ {res.price}. Closing in 2s ...")
    time.sleep(2)
    pos = None
    for p in my_positions(symbol):
        if p.comment == "smc-test":
            pos = p
    if pos is None:
        ps = my_positions(symbol)
        pos = ps[-1] if ps else None
    if pos is None:
        log.warning("STARTUP TEST: opened but couldn't find the position to close")
        return
    cres = close_position(pos)
    if cres is not None and cres.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"STARTUP TEST: closed @ {cres.price} - execution pipeline OK")
    else:
        log.warning(f"STARTUP TEST: close FAILED — retcode "
                    f"{getattr(cres, 'retcode', None)} {getattr(cres, 'comment', '')}")


# ----------------------------------------------------------------------
# main loop
# ----------------------------------------------------------------------
def main() -> int:
    attach_live_log()
    if not connect():
        return 1
    symbol = resolve_symbol(cfg.MT5["symbol"])
    if symbol is None:
        log.error("no XAUUSD-like symbol available")
        return 1

    # startup execution self-test: open + close one random min-lot demo trade
    if cfg.LIVE.get("startup_test_trade"):
        startup_test_trade(symbol)

    strategy = MTFStrategy(cfg)
    max_htf = max(cfg.LOOKBACK["htf_structure"], cfg.LOOKBACK["htf_liquidity"])
    if getattr(cfg, "EMA", {}).get("enabled"):
        max_htf = max(max_htf, cfg.EMA.get("period", 200) + 5)
    if getattr(cfg, "HTF_TREND", {}).get("enabled"):
        h1_m15_bars = (cfg.HTF_TREND["lookback"] * cfg.HTF_TREND["tf_minutes"]
                       // cfg.TIMEFRAMES["htf_minutes"])
        max_htf = max(max_htf, h1_m15_bars + 5)
    max_ltf = max(cfg.LOOKBACK["ltf_choch"], cfg.ATR["period"] + 2)
    if getattr(cfg, "RSI", {}).get("enabled"):
        max_ltf = max(max_ltf, cfg.RSI.get("lookback", 40))

    # execution layer — mirror the backtester exactly
    cooldown_bars = cfg.RISK.get("cooldown_bars", 0)
    max_open = cfg.RISK.get("max_open_positions", 1)
    fil = cfg.FILTERS
    cap = 1 if fil.get("one_trade_per_zone") else fil.get("max_trades_per_zone", 999)
    zone_counts: dict = {}        # zone_idx -> times traded (per-zone cap)
    last_sig = None               # (dir, entry, sl, tp) of last trade (dedup)

    if cfg.BACKTEST.get("entry_trigger", "fvg") != "choch":
        log.warning("entry_trigger != 'choch': live sends MARKET orders, so it only "
                    "matches the backtest in 'choch' mode. Set it to 'choch'.")

    mode = "LIVE TRADING" if cfg.LIVE["enable_trading"] else "SIGNAL-ONLY (no orders)"
    log.info(f"Running {symbol}  mode={mode}  max_open={max_open}  (Ctrl-C to stop)")

    last_bar = None
    cooldown_until = 0           # M5 bar-number until which we don't open
    open_tickets: set = set()    # our positions seen last cycle (to detect closures)
    bars_seen = 0
    hb = cfg.LIVE.get("heartbeat_bars", 0)
    try:
        while True:
            m5, m15 = get_frames(symbol, cfg.LIVE["bars_to_pull"])
            if m5 is None:
                time.sleep(cfg.LIVE["poll_seconds"])
                continue

            cur_bar = int(m5.index[-1])
            if cur_bar == last_bar:                 # no new closed bar yet
                time.sleep(cfg.LIVE["poll_seconds"])
                continue
            last_bar = cur_bar
            bars_seen += 1

            # heartbeat: prove the bot is alive and show where setups die
            d = strategy.diag
            if hb and bars_seen % hb == 0:
                top = max(d.counts.items(), key=lambda kv: kv[1], default=("-", 0))
                log.info(f"HEARTBEAT bars={bars_seen} open={len(my_positions(symbol))} "
                         f"| evals={d.total} signals={d.signals} | top reject: {top[0]}={top[1]}")

            # detect closed positions -> start cooldown (mirrors backtest _try_close)
            positions = my_positions(symbol)
            cur_tickets = {p.ticket for p in positions}
            if open_tickets - cur_tickets:          # one of ours closed since last bar
                cooldown_until = cur_bar + cooldown_bars
            open_tickets = cur_tickets

            # capacity + cooldown gates (same as the backtest loop)
            if len(positions) >= max_open:
                continue
            if cur_bar < cooldown_until:
                continue

            htf_win = m15.iloc[-(max_htf + 1):]
            ltf_win = m5.iloc[-(max_ltf + 1):]
            try:
                sig = strategy.evaluate(htf_win, ltf_win)
            except Exception as e:
                log.warning(f"evaluate failed this bar (skipping): {e}")
                continue
            if sig is None:
                continue

            # per-zone cap + dedup (mirror backtest _admit)
            key = sig.meta.get("zone_idx", sig.idx)
            if zone_counts.get(key, 0) >= cap:
                continue
            sig_key = (sig.direction.value, sig.entry, sig.sl, sig.tp)
            if sig_key == last_sig:
                continue

            log.info(f"SIGNAL {sig.direction.value.upper()} entry~{sig.entry} "
                     f"SL {sig.sl} TP {sig.tp} RR {sig.rr}")
            if cfg.LIVE["enable_trading"]:
                if place_order(symbol, sig):
                    zone_counts[key] = zone_counts.get(key, 0) + 1
                    last_sig = sig_key
            else:
                last_sig = sig_key                  # avoid logging the same signal twice
                log.info("  (signal-only mode: order NOT sent — set LIVE.enable_trading=True)")
    except KeyboardInterrupt:
        log.info("stopped by user")
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
