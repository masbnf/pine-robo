"""Backtest engine.

Walks the M5 series bar-by-bar. At each bar it builds FIXED-SIZE rolling
windows for both timeframes (slice [-N:], never a growing slice from 0) and asks
the strategy for a signal. Open trades are then checked against the next bars for
SL/TP. This keeps per-bar cost O(window) instead of O(n) -> total O(n) not O(n^2).
"""
from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import List, Optional
import pandas as pd

from data import loader
from strategy import MTFStrategy
from smc.types import Dir, Signal
from utils import get_logger
from utils.diagnostics import write_run_log


@dataclass
class Trade:
    direction: str
    entry: float
    sl: float
    tp: float
    lot: float
    rr: float
    entry_idx: int
    entry_time: str
    exit_idx: int = -1
    exit_time: str = ""
    exit_price: float = 0.0
    result: str = "open"        # "win" | "loss" | "open"
    pnl: float = 0.0
    zone_key: int = -1
    init_risk: float = 0.0      # |entry - sl| at open, for R-based stop management
    mae: float = 0.0            # max ADVERSE excursion (toward the stop) during the trade
    meta: dict = field(default_factory=dict)   # entry context (arm_wait, zone_dist, ...)


@dataclass
class Pending:
    """A limit order waiting for price to reach its level (realistic fill)."""
    direction: str
    entry: float                # the level the limit sits at
    sl: float
    tp: float
    lot: float
    rr: float
    created_idx: int
    zone_key: int
    meta: dict = field(default_factory=dict)


class Backtester:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = get_logger(cfg)
        self.strategy = MTFStrategy(cfg)
        self.trades: List[Trade] = []
        self._open_trades: List[Trade] = []     # concurrent open positions
        self._pending: List[Pending] = []       # limit orders awaiting fill
        self._zone_counts: dict = {}            # zone_idx -> how many times traded
        self._cooldown_until: int = 0       # no new entries until this bar index
        self._last_sig = None               # (dir, entry, sl, tp) of last trade, for dedup
        self.equity_curve: list = [cfg.RISK["account_balance"]]

    # ------------------------------------------------------------------
    def run(self, m5=None, m15=None) -> dict:
        """Run the backtest.

        Pass pre-loaded `m5`/`m15` DataFrames to reuse data across many runs
        (e.g. a parameter sweep) and avoid re-reading the CSV each time.
        """
        cfg = self.cfg
        bc = cfg.BACKTEST

        if m5 is None:
            m5 = loader.load_m5(bc["data_file"], bc["datetime_col"], bc["tz"])
        if m15 is None:
            m15 = loader.resample(m5, cfg.TIMEFRAMES["htf_minutes"])
        self.log.info(f"Loaded {len(m5)} M5 / {len(m15)} M15 candles")

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
        spread = bc["spread_points"] * cfg.INSTRUMENT["point"]
        max_open = cfg.RISK.get("max_open_positions", 1)
        # "choch" trigger -> market fill; "fvg" trigger -> limit (if entry_mode==limit)
        market_entry = bc.get("entry_trigger", "fvg") == "choch"
        limit_mode = (not market_entry) and bc.get("entry_mode", "limit") == "limit"

        warmup = max(bc["warmup_bars"], max_htf * cfg.TIMEFRAMES["htf_minutes"] // 5)

        for i in range(warmup, len(m5)):
            bar = m5.iloc[i]
            ts = bar["time"]

            # 1) try to fill pending limit orders placed on EARLIER bars
            if self._pending:
                self._process_pending(bar, i)

            # 2) manage ALL open trades against this bar
            if self._open_trades:
                self._manage_all(bar, i)

            # 3) look for a new signal while we have capacity and past the cooldown
            committed = len(self._open_trades) + len(self._pending)
            if committed < max_open and i >= self._cooldown_until:
                h_end = loader.align_index(m15, ts, cfg.TIMEFRAMES["htf_minutes"],
                                           cfg.TIMEFRAMES["ltf_minutes"])
                if h_end < max_htf:
                    continue

                # FIXED-SIZE rolling buffers ([-N:]), not growing slices
                htf_win = m15.iloc[max(0, h_end - max_htf):h_end + 1]
                ltf_win = m5.iloc[max(0, i - max_ltf):i + 1]

                sig = self.strategy.evaluate(htf_win, ltf_win)
                if sig is not None:
                    if limit_mode:
                        self._place_order(sig, i)        # wait for price to reach it
                    else:
                        self._open_trade(sig, bar, i)    # market fill (CHoCH break / instant)

        # cancel anything still pending, then close any runners still open at the end
        self._pending = []
        last = m5.iloc[-1]
        for t in self._open_trades:
            t.exit_idx = len(m5) - 1
            t.exit_time = str(last["time"])
            t.exit_price = float(last["close"])
            t.result = "open"
            self.trades.append(t)
        self._open_trades = []

        report = self._report()

        # write a timestamped diagnostics log for THIS run
        try:
            import os
            log_file = self.cfg.LOGGING.get("log_file") or "logs/backtest.log"
            log_dir = os.path.dirname(log_file) or "logs"
            data_range = f"{m5['time'].iloc[0]}  ->  {m5['time'].iloc[-1]}  ({len(m5)} M5 bars)"
            sections = [self._setup_breakdown(), self._score_breakdown(), self._mae_analysis()]
            extra_text = "\n\n".join(s for s in sections if s)
            path = write_run_log(log_dir, report, self.strategy.diag, data_range,
                                 equity=self.equity_curve,
                                 extra_text=extra_text)
            self.log.info(f"Diagnostics log -> {path}")
        except Exception as e:
            self.log.warning(f"could not write diagnostics log: {e}")

        return report

    # ------------------------------------------------------------------
    def _admit(self, sig: Signal):
        """Apply per-zone cap + dedup. Returns the zone key if allowed, else None."""
        fil = self.cfg.FILTERS
        cap = 1 if fil.get("one_trade_per_zone") else fil.get("max_trades_per_zone", 999)
        key = sig.meta.get("zone_idx", sig.idx)
        if self._zone_counts.get(key, 0) >= cap:
            return None
        sig_key = (sig.direction.value, sig.entry, sig.sl, sig.tp)
        if sig_key == self._last_sig:
            return None
        self._last_sig = sig_key
        self._zone_counts[key] = self._zone_counts.get(key, 0) + 1
        return key

    def _place_order(self, sig: Signal, i: int):
        """Queue a limit order; it only becomes a trade if price reaches the level."""
        key = self._admit(sig)
        if key is None:
            return
        self._pending.append(Pending(
            direction=sig.direction.value, entry=sig.entry, sl=sig.sl, tp=sig.tp,
            lot=sig.lot, rr=sig.rr, created_idx=i, zone_key=key, meta=dict(sig.meta),
        ))

    def _process_pending(self, bar, i: int):
        """Fill limits whose level was reached this bar; cancel expired ones."""
        bc = self.cfg.BACKTEST
        slip = bc.get("slippage_points", 0) * self.cfg.INSTRUMENT["point"]
        expiry = bc.get("entry_expiry_bars", 8)
        hi, lo = float(bar["high"]), float(bar["low"])
        digits = self.cfg.INSTRUMENT["digits"]

        still = []
        for p in self._pending:
            if i - p.created_idx > expiry:
                continue                       # cancelled, never filled
            if p.direction == Dir.BULL.value and lo <= p.entry:
                self._fill(p, round(p.entry + slip, digits), bar, i)   # buy pays up
            elif p.direction == Dir.BEAR.value and hi >= p.entry:
                self._fill(p, round(p.entry - slip, digits), bar, i)   # sell gets less
            else:
                still.append(p)
        self._pending = still

    def _fill(self, p: Pending, fill_price: float, bar, i: int):
        t = Trade(
            direction=p.direction, entry=fill_price, sl=p.sl, tp=p.tp,
            lot=p.lot, rr=p.rr, entry_idx=i, entry_time=str(bar["time"]),
            zone_key=p.zone_key, init_risk=abs(fill_price - p.sl), meta=dict(p.meta),
        )
        self._open_trades.append(t)
        if self.cfg.LOGGING["log_signals"]:
            self.log.info(f"FILL  {t.direction.upper():4} @ {t.entry} "
                          f"SL {t.sl} TP {t.tp} RR {t.rr} lot {t.lot} "
                          f"[{len(self._open_trades)} open]")

    def _open_trade(self, sig: Signal, bar, i: int):
        """Market fill at the signal price (CHoCH break / instant), with slippage."""
        key = self._admit(sig)
        if key is None:
            return
        slip = self.cfg.BACKTEST.get("slippage_points", 0) * self.cfg.INSTRUMENT["point"]
        entry = sig.entry + slip if sig.direction == Dir.BULL else sig.entry - slip
        entry = round(entry, self.cfg.INSTRUMENT["digits"])
        t = Trade(
            direction=sig.direction.value, entry=entry, sl=sig.sl, tp=sig.tp,
            lot=sig.lot, rr=sig.rr, entry_idx=i, entry_time=str(bar["time"]),
            zone_key=key, init_risk=abs(entry - sig.sl), meta=dict(sig.meta),
        )
        self._open_trades.append(t)
        if self.cfg.LOGGING["log_signals"]:
            self.log.info(f"OPEN  {t.direction.upper():4} @ {t.entry} "
                          f"SL {t.sl} TP {t.tp} RR {t.rr} lot {t.lot} "
                          f"[{len(self._open_trades)} open]")

    def _manage_all(self, bar, i: int):
        """Check every open trade against this bar; close those that hit SL/TP, then
        advance the dynamic stops (breakeven / trail) on the survivors for next bar."""
        still_open = []
        for t in self._open_trades:
            self._update_mae(t, bar)         # record how far it ran toward the stop
            if self._try_close(t, bar, i):
                continue
            self._update_stop(t, bar)        # adjust SL AFTER the close check (no look-ahead)
            still_open.append(t)
        self._open_trades = still_open

    def _update_mae(self, t: Trade, bar):
        # adverse = how far price moved AGAINST us (toward the stop)
        adv = (t.entry - float(bar["low"])) if t.direction == Dir.BULL.value \
            else (float(bar["high"]) - t.entry)
        if adv > t.mae:
            t.mae = adv

    def _update_stop(self, t: Trade, bar):
        """Move SL to breakeven after +Nx risk, then optionally trail it."""
        m = self.cfg.MANAGEMENT
        if t.init_risk <= 0:
            return
        digits = self.cfg.INSTRUMENT["digits"]
        buf = m.get("breakeven_buffer_points", 0) * self.cfg.INSTRUMENT["point"]
        be_r = m.get("breakeven_at_r", 0)
        tr_after = m.get("trail_after_r", 0)
        tr_dist = m.get("trail_distance_r", 0) * t.init_risk
        hi, lo = float(bar["high"]), float(bar["low"])

        if t.direction == Dir.BULL.value:
            r = (hi - t.entry) / t.init_risk
            if be_r and r >= be_r:
                t.sl = max(t.sl, round(t.entry + buf, digits))      # to breakeven+
            if tr_after and tr_dist and r >= tr_after:
                t.sl = max(t.sl, round(hi - tr_dist, digits))       # trail up
        else:
            r = (t.entry - lo) / t.init_risk
            if be_r and r >= be_r:
                t.sl = min(t.sl, round(t.entry - buf, digits))
            if tr_after and tr_dist and r >= tr_after:
                t.sl = min(t.sl, round(lo + tr_dist, digits))       # trail down

    def _try_close(self, t: Trade, bar, i: int) -> bool:
        """Return True if the trade closed on this bar."""
        hi, lo = float(bar["high"]), float(bar["low"])
        hit_sl = hit_tp = False

        if t.direction == Dir.BULL.value:
            if lo <= t.sl:
                hit_sl = True
            elif hi >= t.tp:
                hit_tp = True
        else:
            if hi >= t.sl:
                hit_sl = True
            elif lo <= t.tp:
                hit_tp = True

        if not (hit_sl or hit_tp):
            return False

        # adverse slippage on stop-loss fills (TP is a limit -> fills at the level)
        slip = self.cfg.BACKTEST.get("slippage_points", 0) * self.cfg.INSTRUMENT["point"]
        if hit_sl:
            exit_price = (t.sl - slip) if t.direction == Dir.BULL.value else (t.sl + slip)
        else:
            exit_price = t.tp
        t.exit_idx = i
        t.exit_time = str(bar["time"])
        t.exit_price = round(exit_price, self.cfg.INSTRUMENT["digits"])
        t.pnl = self._pnl(t)
        # classify by realized pnl, not by which level was hit: a breakeven/trailing
        # stop can be hit_sl yet finish at/above entry -> not a real "loss"
        t.result = "win" if t.pnl > 0 else "loss"
        self.trades.append(t)
        self._cooldown_until = i + self.cfg.RISK.get("cooldown_bars", 0)
        if self.cfg.LOGGING["log_signals"]:
            self.log.info(f"CLOSE {t.direction.upper():4} {t.result.upper():4} "
                          f"@ {exit_price}  pnl ${t.pnl:.2f}")
        return True

    def _pnl(self, t: Trade) -> float:
        cs = self.cfg.INSTRUMENT.get("contract_size", 100)
        move = (t.exit_price - t.entry) if t.direction == Dir.BULL.value \
            else (t.entry - t.exit_price)
        gross = move * cs * t.lot          # $ = price move * oz/lot * lots
        return round(gross - self.cfg.BACKTEST["commission"], 2)

    # ------------------------------------------------------------------
    def _mae_analysis(self) -> str:
        """Split the entry->STOP distance into 4 quarters and look at how far each
        trade ran TOWARD the stop (max adverse excursion) before it resolved. The
        key question: once a trade goes X% toward the stop, does it carry on to the
        full stop, or recover? This tells you whether a TIGHTER stop would save the
        same losers with a smaller loss, or just cut trades that would've recovered."""
        rows = [t for t in self.trades
                if t.result in ("win", "loss") and abs(t.entry - t.sl) > 0]
        if not rows:
            return ""

        def frac(t):
            return min(t.mae / abs(t.entry - t.sl), 1.0)   # 0..1 (1 = reached the stop)

        L = ["MAE ANALYSIS  (max run TOWARD THE STOP before the trade resolved)",
             "-" * 72]

        # 1) distribution: deepest quarter each trade dipped to, and how it ended
        L.append("  deepest reached toward stop |  trades |   won  |  lost(SL)")
        labels = ["Q1  0-25%", "Q2  25-50%", "Q3  50-75%", "Q4  75-100%"]
        for b in range(4):
            grp = [t for t in rows if (3 if frac(t) >= 1.0 else int(frac(t) * 4)) == b]
            w = sum(1 for t in grp if t.result == "win")
            ls = sum(1 for t in grp if t.result == "loss")
            L.append(f"  {labels[b]:<26} | {len(grp):>6}  | {w:>5}  | {ls:>6}")

        # 2) conditional: of trades that went >= X toward the stop, how many still
        #    ended at the full stop (vs recovered into a win)?
        L.append("")
        L.append("  went at least ... toward stop ->  still ended at SL  (recovery view)")
        for thr, name in [(0.25, "25% toward stop"), (0.50, "50% toward stop"),
                          (0.75, "75% toward stop")]:
            reached = [t for t in rows if frac(t) >= thr]
            lost = [t for t in reached if t.result == "loss"]
            rec = len(reached) - len(lost)
            rate = (len(lost) / len(reached) * 100) if reached else 0.0
            L.append(f"  {name:<24} | {len(reached):>4} trades | "
                     f"{rate:5.1f}% to SL | {rec:>4} recovered")

        L.append("")
        L.append("  reading: if trades that reach 75% toward the stop almost always end")
        L.append("           at SL, a tighter stop there loses the SAME trades but with a")
        L.append("           smaller loss. If many recover from deep, keep the wide stop.")
        return "\n".join(L)

    # ------------------------------------------------------------------
    def _report(self) -> dict:
        closed = [t for t in self.trades if t.result in ("win", "loss")]
        wins = [t for t in closed if t.result == "win"]
        losses = [t for t in closed if t.result == "loss"]
        pnl = sum(t.pnl for t in closed)
        start = self.cfg.RISK["account_balance"]

        rep = {
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
            "net_pnl": round(pnl, 2),
            "final_balance": round(start + pnl, 2),
            "return_pct": round(pnl / start * 100, 1) if start else 0.0,
            "avg_rr": round(sum(t.rr for t in closed) / len(closed), 2) if closed else 0.0,
        }
        rep.update(self._risk_metrics(closed, wins, losses, start))

        self.log.info("-" * 48)
        for k, v in rep.items():
            self.log.info(f"{k:>18}: {v}")
        self.log.info("-" * 48)
        return rep

    # ------------------------------------------------------------------
    def _risk_metrics(self, closed, wins, losses, start) -> dict:
        """Equity-path risk stats: drawdown, profit factor, streaks, averages."""
        # equity curve (after each closed trade) + drawdown
        eq = start
        self.equity_curve = [start]
        peak, max_dd, max_dd_pct = start, 0.0, 0.0
        for t in closed:
            eq += t.pnl
            self.equity_curve.append(round(eq, 2))
            peak = max(peak, eq)
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd
            if peak > 0 and dd / peak * 100 > max_dd_pct:
                max_dd_pct = dd / peak * 100

        gross_profit = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)          # positive number
        pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

        # consecutive streaks
        max_loss_streak = max_win_streak = cur_l = cur_w = 0
        for t in closed:
            if t.result == "loss":
                cur_l += 1; cur_w = 0
            else:
                cur_w += 1; cur_l = 0
            max_loss_streak = max(max_loss_streak, cur_l)
            max_win_streak = max(max_win_streak, cur_w)

        avg_win = (gross_profit / len(wins)) if wins else 0.0
        avg_loss = (-gross_loss / len(losses)) if losses else 0.0
        expectancy = (sum(t.pnl for t in closed) / len(closed)) if closed else 0.0

        return {
            "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
            "max_drawdown": round(max_dd, 2),
            "max_drawdown_pct": round(max_dd_pct, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
            "largest_win": round(max((t.pnl for t in wins), default=0.0), 2),
            "largest_loss": round(min((t.pnl for t in losses), default=0.0), 2),
            "max_win_streak": max_win_streak,
            "max_loss_streak": max_loss_streak,
        }

    # ------------------------------------------------------------------
    def _setup_breakdown(self) -> str:
        """Group closed trades by entry characteristics to expose WHICH kind of
        entry bleeds to the stop — so you can filter it or fix the trigger."""
        rows = [t for t in self.trades if t.result in ("win", "loss")]
        if not rows:
            return ""

        def stats(g):
            n = len(g)
            w = sum(1 for t in g if t.result == "win")
            gp = sum(t.pnl for t in g if t.pnl > 0)
            gl = -sum(t.pnl for t in g if t.pnl < 0)
            pf = gp / gl if gl > 0 else float("inf")
            return n, (w / n * 100 if n else 0.0), sum(t.pnl for t in g), pf

        def line(label, g):
            if not g:
                return None
            n, wr, net, pf = stats(g)
            pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
            return f"  {label:<20} | {n:>4} | {wr:5.1f}% | net {net:>8.1f} | PF {pfs}"

        def hr(t):
            try:
                return pd.Timestamp(t.entry_time).hour
            except Exception:
                return -1

        L = ["SETUP BREAKDOWN  (where your entries win vs bleed to stop)", "-" * 72]
        L.append("  group                |trades| win%  |   net    | PF")

        L.append("  -- by direction --")
        for d, lbl in (("bull", "BUY"), ("bear", "SELL")):
            ln = line(lbl, [t for t in rows if t.direction == d])
            if ln: L.append(ln)

        L.append("  -- by session (UTC hour) --")
        for name, hrs in [("Asian 00-07", range(0, 7)), ("London 07-12", range(7, 12)),
                          ("NY overlap 12-16", range(12, 16)), ("NY 16-21", range(16, 21)),
                          ("Late 21-24", range(21, 24))]:
            ln = line(name, [t for t in rows if hr(t) in hrs])
            if ln: L.append(ln)

        L.append("  -- by zone-armed wait (M5 bars to CHoCH) --")
        for name, lo, hi in [("0-20", 0, 20), ("20-50", 20, 50),
                             ("50-100", 50, 100), ("100+", 100, 10**9)]:
            ln = line(name, [t for t in rows if lo <= t.meta.get("arm_wait", 0) < hi])
            if ln: L.append(ln)

        L.append("  -- by planned R:R --")
        for name, lo, hi in [("<2", 0, 2), ("2-3", 2, 3), ("3-4", 3, 4), ("4+", 4, 10**9)]:
            ln = line(name, [t for t in rows if lo <= t.rr < hi])
            if ln: L.append(ln)

        L.append("  -- by distance to zone at entry (xATR) --")
        for name, lo, hi in [("0-0.5", 0, 0.5), ("0.5-1", 0.5, 1.0),
                             ("1-2", 1.0, 2.0), ("2+", 2.0, 10**9)]:
            ln = line(name, [t for t in rows if lo <= t.meta.get("zone_dist_atr", 0) < hi])
            if ln: L.append(ln)

        L.append("")
        L.append("  reading: any bucket with PF < 1 or low win% is dragging you down —")
        L.append("           filter that case out, or fix the entry for it.")
        return "\n".join(L)

    # ------------------------------------------------------------------
    def _score_breakdown(self) -> str:
        """Score-gate performance: PF by bucket + per-dimension win/loss averages."""
        rows = [t for t in self.trades if t.result in ("win", "loss")]
        scored = [t for t in rows if t.meta.get("score", -1) >= 0]
        if not scored:
            return ""

        def stats(g):
            n = len(g)
            w = sum(1 for t in g if t.result == "win")
            gp = sum(t.pnl for t in g if t.pnl > 0)
            gl = -sum(t.pnl for t in g if t.pnl < 0)
            pf = gp / gl if gl > 0 else float("inf")
            return n, (w / n * 100 if n else 0.0), sum(t.pnl for t in g), pf

        def line(label, g):
            if not g:
                return None
            n, wr, net, pf = stats(g)
            pfs = "inf" if pf == float("inf") else f"{pf:.2f}"
            return f"  {label:<14} | {n:>4} | {wr:5.1f}% | net {net:>8.1f} | PF {pfs}"

        L = ["SCORE BREAKDOWN", "-" * 72]
        L.append("  -- by total score --")
        L.append("  score         |trades| win%  |   net    | PF")
        for lo, hi, lbl in [(5, 6, "score 5"), (6, 7, "score 6"), (7, 8, "score 7"),
                            (8, 9, "score 8"), (9, 11, "score 9-10"), (0, 5, "score <5*")]:
            g = [t for t in scored if lo <= t.meta.get("score", -1) < hi]
            ln = line(lbl, g)
            if ln:
                L.append(ln)
        L.append("  (* score <5 only present if SCORING disabled or min_score lowered)")

        # dimension averages: wins vs losses
        dims = [
            ("h1_aligned",  "H1 aligned  (bool)",   False),
            ("h1_fresh",    "H1 fresh    (bool)",    False),
            ("bos_excess",  "BOS excess  (xATR)",    False),
            ("bos_body",    "BOS body    (xATR)",    False),
            ("sweep_depth", "Sweep depth (xATR)",    False),
            ("sweep_speed", "Sweep speed (bars)",    True),   # lower=better → invert delta sign
            ("choch_fresh", "CHoCH fresh (M5 bars)", True),
            ("choch_body",  "CHoCH body  (xATR)",    False),
        ]
        wins_s  = [t for t in scored if t.result == "win"]
        losses_s = [t for t in scored if t.result == "loss"]

        def dim_avg(group, key):
            vals = [t.meta.get("score_detail", {}).get(key) for t in group]
            vals = [float(v) for v in vals if v is not None]
            return sum(vals) / len(vals) if vals else None

        L.append("")
        L.append("  -- dimension averages: WINS vs LOSSES (higher delta = stronger signal) --")
        L.append(f"  {'dimension':<26} | {'wins avg':>8} | {'loss avg':>8} | {'delta':>7}")
        L.append("  " + "-" * 58)
        for key, label, lower_better in dims:
            wa = dim_avg(wins_s,   key)
            la = dim_avg(losses_s, key)
            if wa is None or la is None:
                continue
            delta = (la - wa) if lower_better else (wa - la)
            sign  = "+" if delta >= 0 else ""
            L.append(f"  {label:<26} | {wa:>8.2f} | {la:>8.2f} | {sign}{delta:>6.2f}")
        L.append("")
        L.append("  reading: positive delta means this dimension is higher in wins.")
        L.append("           Dimensions with delta near 0 are not discriminating — consider")
        L.append("           removing their point or recalibrating the threshold.")
        return "\n".join(L)

    def trades_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame([asdict(t) for t in self.trades])
