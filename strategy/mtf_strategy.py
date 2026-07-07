"""Multi-Timeframe SMC strategy engine.

Implements the full BUY / SELL sequence from the spec, reading every window and
fractal size from config/settings.py. The engine is *pure*: given the current
HTF and LTF windows it returns a Signal or None. All state (open trades, traded
zones) lives in the backtest engine, so this stays easy to unit-test.

BUY sequence
  1. M15 (structure window):   bullish BOS, price in a higher-low, not extended
  2. M15 (liquidity window):   SSL below a recent swing low swept
  3. M15 (OB/zone window):     price retraces into demand OB, overlapping unfilled bull FVG
  4. M5  (choch window):       bullish CHoCH inside that zone
  5. M5  (entry window):       enter on retrace into freshest M5 FVG/OB
  6. SL below immediate M5 swing low (ATR-capped); TP = next M15 BSL
SELL is the mirror.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd

from smc import structure as st
from smc import order_blocks as ob_mod
from smc import zones as zn
from smc import liquidity as lq
from smc import fvg as fvg_mod
from smc.indicators import atr, rsi, ema
from smc.types import Dir, Signal
from utils.diagnostics import Diagnostics


class MTFStrategy:
    def __init__(self, cfg):
        self.cfg = cfg
        self.LB = cfg.LOOKBACK
        self.FR = cfg.FRACTALS
        self.ATR = cfg.ATR
        self.RSI = getattr(cfg, "RSI", {"enabled": False})
        self.EMA = getattr(cfg, "EMA", {"enabled": False})
        self.HTF_TREND = getattr(cfg, "HTF_TREND", {"enabled": False})
        self.SCORING = getattr(cfg, "SCORING", {"enabled": False})
        self.RISK = cfg.RISK
        self.FIL = cfg.FILTERS
        self.point = cfg.INSTRUMENT["point"]
        self.diag = Diagnostics()      # per-run filter funnel (read by the backtester)
        # armed-zone state machine: once price taps a zone and a fresh M5 BOS
        # confirms it, we "arm" it and then wait up to ltf_zone_arm bars for an
        # M5 CHoCH (instead of demanding both in the same bar). One armed slot
        # per direction. `_pending` holds zones that were touched but are still
        # waiting for that M5 confirmation.
        self._armed = {Dir.BULL: None, Dir.BEAR: None}
        self._pending = {Dir.BULL: None, Dir.BEAR: None}

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------
    def evaluate(self, htf: pd.DataFrame, ltf: pd.DataFrame) -> Optional[Signal]:
        """Return a Signal if the full sequence is satisfied at the latest LTF bar."""
        for direction in (Dir.BULL, Dir.BEAR):
            sig = self._evaluate_direction(direction, htf, ltf)
            if sig is not None:
                return sig
        return None

    # ------------------------------------------------------------------
    # one side
    # ------------------------------------------------------------------
    def _evaluate_direction(self, direction: Dir,
                            htf: pd.DataFrame, ltf: pd.DataFrame) -> Optional[Signal]:
        cfg = self.cfg
        self.diag.start()
        sf = {}   # score_factors — populated throughout this method

        # ---- STEP 1: HTF bias (BOS) ----------------------------------
        struct_win = htf.tail(self.LB["htf_structure"])
        htf_swings = st.find_swings(struct_win,
                                    self.FR["htf_swing_left"], self.FR["htf_swing_right"])
        a_htf = atr(struct_win, self.ATR["period"])
        min_body = a_htf * self.FIL.get("bos_min_body_atr", 0.0)
        bos = st.detect_bos(struct_win, htf_swings, min_body=min_body)
        if bos is None:
            self.diag.reject("no_bos")
            return None
        if bos.direction != direction:
            self.diag.reject("bos_wrong_direction")
            return None

        price = float(ltf["close"].iloc[-1])

        # BOS quality scoring (captured regardless of later filters)
        if bos.break_idx in struct_win.index:
            _bc = float(struct_win.loc[bos.break_idx, "close"])
            _bo = float(struct_win.loc[bos.break_idx, "open"])
            sf["bos_excess"] = (
                abs(_bc - bos.broken_swing.price) / a_htf if a_htf > 0 else 0.0
            )
            sf["bos_body"] = abs(_bc - _bo) / a_htf if a_htf > 0 else 0.0
        else:
            sf["bos_excess"] = 0.0
            sf["bos_body"]   = 0.0

        # not extended: distance from impulse origin within N*ATR
        if a_htf > 0 and self.FIL["not_extended_atr"]:
            origin = float(htf.loc[bos.impulse_start, "close"])
            ext = abs(price - origin) / a_htf            # over-extension in ATR units
            if ext > self.FIL["not_extended_atr"]:
                self.diag.reject("extended", margin=ext)
                return None

        # ---- STEP 1b: H1 macro trend gate (optional) -----------------
        if self.HTF_TREND.get("enabled"):
            _ht = self.HTF_TREND
            h1_df = self._resample_h1(htf, _ht["tf_minutes"])
            h1_win = h1_df.tail(_ht["lookback"])
            h1_dir = None
            h1_bos = None
            if len(h1_win) >= _ht["swing_left"] + _ht["swing_right"] + 2:
                h1_sw  = st.find_swings(h1_win, _ht["swing_left"], _ht["swing_right"])
                h1_bos = st.detect_bos(h1_win, h1_sw)
                h1_dir = h1_bos.direction if h1_bos is not None else None
            # scoring: H1 alignment & freshness
            h1_fresh_bars = (
                h1_win.index[-1] - h1_bos.break_idx
                if h1_bos is not None and not h1_win.empty
                   and h1_bos.break_idx in h1_win.index
                else 999
            )
            sf["h1_aligned"] = (h1_dir == direction)
            sf["h1_fresh"]   = h1_fresh_bars <= self.SCORING.get("h1_fresh_bars", 8)
            # direction gate
            if h1_dir is not None and h1_dir != direction:
                self.diag.reject("against_h1_trend", margin=float(h1_fresh_bars))
                return None
            if not _ht.get("block_only_clear", True) and h1_dir is None:
                self.diag.reject("against_h1_trend", margin=float(h1_fresh_bars))
                return None
        else:
            sf["h1_aligned"] = False
            sf["h1_fresh"]   = False

        # ---- STEP 1c: HTF EMA trend gate (optional) -----------------
        if self.EMA.get("enabled"):
            ev = ema(htf, self.EMA.get("period", 200))
            if ev is not None:                           # None = not enough bars yet
                if (direction == Dir.BULL and price < ev) or \
                   (direction == Dir.BEAR and price > ev):
                    self.diag.reject("ema_trend")
                    return None

        # ---- STEP 2: liquidity sweep ---------------------------------
        liq_win = htf.tail(self.LB["htf_liquidity"])
        liq_swings = st.find_swings(liq_win,
                                    self.FR["htf_swing_left"], self.FR["htf_swing_right"])
        pools = lq.find_liquidity(liq_swings)
        # mark sweeps on BOTH sides: the entry gate uses one side, while the TP
        # selection needs the OTHER side's swept flags (to skip consumed liquidity).
        swept_ssl = lq.detect_sweep(liq_win, pools, "SSL")
        swept_bsl = lq.detect_sweep(liq_win, pools, "BSL")
        want_swept = swept_ssl if direction == Dir.BULL else swept_bsl
        if self.FIL["require_liquidity_sweep"] and want_swept is None:
            self.diag.reject("no_sweep")
            return None
        # sweep quality scoring
        if want_swept is not None:
            _sw_kind  = "SSL" if direction == Dir.BULL else "BSL"
            _sw_after = liq_win[liq_win.index > want_swept.idx]
            if _sw_kind == "SSL":
                _sw_hit = _sw_after[
                    (_sw_after["low"]   < want_swept.price) &
                    (_sw_after["close"] > want_swept.price)
                ]
            else:
                _sw_hit = _sw_after[
                    (_sw_after["high"]  > want_swept.price) &
                    (_sw_after["close"] < want_swept.price)
                ]
            if not _sw_hit.empty:
                _col_ext = "low" if _sw_kind == "SSL" else "high"
                _ext = float(liq_win.loc[_sw_hit.index[0], _col_ext])
                sf["sweep_depth"] = (
                    abs(want_swept.price - _ext) / a_htf if a_htf > 0 else 0.0
                )
                sf["sweep_speed"] = int(_sw_hit.index[0] - want_swept.idx)
            else:
                sf["sweep_depth"] = 0.0
                sf["sweep_speed"] = 99
        else:
            sf["sweep_depth"] = 0.0
            sf["sweep_speed"] = 99

        # ---- STEP 3: HTF order block + zone (+ FVG overlap) ----------
        ob = ob_mod.find_order_block(htf, bos.impulse_start, direction, self.LB["htf_ob"])
        if ob is None:
            self.diag.reject("no_ob")
            return None
        zone = zn.zone_from_ob(htf, ob, a_htf,
                               getattr(self.cfg, "ZONES", {}).get("ob_atr_mult", 0.5))
        if zone is None:
            self.diag.reject("no_zone")
            return None

        # ---- STEP 3b: touch the (widened) zone -> pending ------------
        # tolerance > 0 lets price near the zone count as a tap.
        tol = self.FIL.get("zone_tap_tolerance_atr", 0.0) * a_htf
        in_zone = (zone.bottom - tol) <= price <= (zone.top + tol)
        cur_idx = int(ltf.index[-1])
        armed = self._armed.get(direction)
        pending = self._pending.get(direction)

        if in_zone and armed is None:
            # FVG overlap is validated at the moment of touch (not every later bar)
            if self.FIL["require_fvg_overlap"]:
                fvg_win = htf.tail(self.LB["htf_fvg"])
                gaps = fvg_mod.mark_filled(fvg_win, fvg_mod.find_fvgs(fvg_win))
                live = fvg_mod.unfilled(gaps, direction)
                if not any(zone.overlaps(g.top, g.bottom) for g in live):
                    self.diag.reject("no_fvg_overlap")
                    return None
            # start pending (or restart only if this is a different zone)
            if pending is None or pending["origin"] != zone.origin_idx:
                pending = {"zone": zone, "touched_idx": cur_idx, "origin": zone.origin_idx}
                self._pending[direction] = pending

        # nothing touched and nothing armed -> price never reached the zone
        if armed is None and pending is None:
            dist = (zone.bottom - price) if price < zone.bottom else (price - zone.top)
            self.diag.reject("zone_not_tapped",
                             margin=(dist / a_htf) if a_htf > 0 else dist)
            return None

        # ---- STEP 3c: PENDING -> ARMED once a fresh M5 BOS confirms the touch
        # (a wider M15 zone catches more taps; this keeps quality up by only
        # arming touches where the M5 chart itself reacts with a structure break)
        if armed is None:
            waited_pending = cur_idx - pending["touched_idx"]
            if waited_pending > self.LB.get("ltf_zone_arm", self.LB["ltf_choch"]):
                self._pending[direction] = None
                self.diag.reject("arm_expired", margin=float(waited_pending))
                return None
            m5_win = ltf.tail(self.LB["ltf_choch"])
            m5_swings = st.find_swings(m5_win,
                                       self.FR["ltf_swing_left"], self.FR["ltf_swing_right"])
            m5_bos = st.detect_bos(m5_win, m5_swings)
            if (m5_bos is None or m5_bos.direction != direction or
                    m5_bos.break_idx <= pending["touched_idx"]):
                self.diag.reject("zone_no_m5_bos")
                return None
            armed = {"zone": pending["zone"], "armed_idx": cur_idx, "origin": pending["origin"]}
            self._armed[direction] = armed
            self._pending[direction] = None

        # armed but waited too long -> disarm
        waited = cur_idx - armed["armed_idx"]
        if waited > self.LB.get("ltf_zone_arm", self.LB["ltf_choch"]):
            self._armed[direction] = None
            self.diag.reject("arm_expired", margin=float(waited))
            return None

        # ---- STEP 4: M5 CHoCH while the zone is armed ----------------
        # (price may have drifted slightly out of the exact band — that's the fix)
        choch_win = ltf.tail(self.LB["ltf_choch"])
        ltf_swings = st.find_swings(choch_win,
                                    self.FR["ltf_swing_left"], self.FR["ltf_swing_right"])
        # freshest CHoCH, and it must have formed AFTER the zone was armed
        choch = st.detect_choch(choch_win, ltf_swings, direction,
                                after_idx=armed["armed_idx"])
        if choch is None:
            self.diag.reject("no_choch")
            return None

        # CHoCH quality scoring
        sf["choch_fresh"] = cur_idx - choch.break_idx
        if choch.break_idx in choch_win.index:
            a_ltf = atr(ltf.tail(self.ATR["period"] + 2), self.ATR["period"])
            _cb = abs(
                float(choch_win.loc[choch.break_idx, "close"]) -
                float(choch_win.loc[choch.break_idx, "open"])
            )
            sf["choch_body"] = _cb / a_ltf if a_ltf > 0 else 0.0
        else:
            sf["choch_body"] = 0.0

        # ---- STEP 4b: RSI confirmation (optional) -------------------
        if self.RSI.get("enabled") and not self._rsi_confirms(direction, ltf):
            self.diag.reject("rsi_block")
            return None

        # ---- STEP 4c: entry-distance filter (optional) --------------
        # reject entries where price has run too far from the zone (stale chase)
        mzd = self.FIL.get("max_zone_dist_atr", 0)
        if mzd and a_htf > 0:
            zmid = (armed["zone"].top + armed["zone"].bottom) / 2.0
            dist = abs(price - zmid) / a_htf
            if dist > mzd:
                self.diag.reject("too_far_from_zone", margin=dist)
                return None

        # ── SCORING GATE ──────────────────────────────────────────────
        if self.SCORING.get("enabled"):
            _score = self._compute_score(sf)
            sf["total"] = _score
            if _score < self.SCORING["min_score"]:
                self.diag.reject("low_score", margin=float(_score))
                return None
        else:
            sf["total"] = -1

        # ---- STEP 5 & 6: entry refinement, SL, TP --------------------
        sig = self._build_signal(direction, htf, ltf, choch_win, ltf_swings, pools, price)
        if sig is not None:
            sig.meta["zone_idx"] = armed["origin"]   # for one-trade-per-zone dedupe
            sig.meta["arm_wait"] = cur_idx - armed["armed_idx"]   # M5 bars zone waited
            zmid = (armed["zone"].top + armed["zone"].bottom) / 2.0
            sig.meta["zone_dist_atr"] = (abs(price - zmid) / a_htf) if a_htf > 0 else 0.0
            sig.meta["score"] = sf.get("total", -1)
            sig.meta["score_detail"] = {
                k: (round(v, 2) if isinstance(v, float) else v)
                for k, v in sf.items() if k != "total"
            }
            self._armed[direction] = None            # disarm after a valid entry
        return sig

    # ------------------------------------------------------------------
    def _compute_score(self, sf: dict) -> int:
        """
        0-10 quality score from dimension values captured during binary-gate evaluation.

        H1 macro  : 0-2 pts   M15 BOS   : 0-3 pts
        M15 Sweep : 0-2 pts   M5 CHoCH  : 0-3 pts  (highest weight)
        Total     : 0-10 pts  |  min_score=5 ≈ 73% pass rate
        """
        sc = self.SCORING
        s = 0

        # ── H1 (0-2) ─────────────────────────────────────────────────
        if sf.get("h1_aligned"):                            s += 1
        if sf.get("h1_aligned") and sf.get("h1_fresh"):    s += 1

        # ── M15 BOS (0-3) ────────────────────────────────────────────
        if sf.get("bos_excess", 0) > sc["bos_excess_weak"]:    s += 1
        if sf.get("bos_excess", 0) > sc["bos_excess_strong"]:  s += 1
        if sf.get("bos_body",   0) > sc["bos_body_min"]:       s += 1

        # ── M15 Sweep (0-2) ──────────────────────────────────────────
        if sf.get("sweep_depth", 0)   > sc["sweep_depth_min"]:  s += 1
        if sf.get("sweep_speed", 99) <= sc["sweep_speed_max"]:  s += 1

        # ── M5 CHoCH (0-3) — highest weight ──────────────────────────
        if sf.get("choch_fresh", 99) <= sc["choch_fresh_good"]:  s += 1
        if sf.get("choch_fresh", 99) <= sc["choch_fresh_great"]: s += 1
        if sf.get("choch_body",   0) >  sc["choch_body_min"]:    s += 1

        return s

    # ------------------------------------------------------------------
    @staticmethod
    def _resample_h1(htf: pd.DataFrame, minutes: int) -> pd.DataFrame:
        """Resample a completed-M15 window up to H1.

        Drops the last H1 bar if it is not yet complete — a H1 bar at time T
        is fully closed only when the M15 window contains data up to T + H1_minutes.
        Without this filter the last bar would use a partial close/high/low
        (look-ahead bias identical to the one already fixed in align_index).
        """
        if "time" not in htf.columns:
            return pd.DataFrame()
        g = htf.set_index("time")
        agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
        h1_raw = (
            g.resample(f"{minutes}min", label="left", closed="left")
             .agg(agg)
             .dropna()
             .reset_index()
        )
        if h1_raw.empty:
            return h1_raw
        last_m15_close = htf["time"].iloc[-1] + pd.Timedelta(minutes=15)
        h1_raw = h1_raw[
            h1_raw["time"] + pd.Timedelta(minutes=minutes) <= last_m15_close
        ]
        return h1_raw.reset_index(drop=True)

    # ------------------------------------------------------------------
    def _rsi_confirms(self, direction: Dir, ltf: pd.DataFrame) -> bool:
        """BUY: RSI dipped to oversold in the recent window AND is turning up.
        SELL: RSI spiked to overbought AND is turning down."""
        period = self.RSI.get("period", 14)
        win = ltf.tail(self.RSI.get("lookback", 40))
        series = rsi(win, period).dropna()
        if len(series) < 2:
            return False                       # not enough data -> don't confirm
        now, prev = float(series.iloc[-1]), float(series.iloc[-2])
        recent = series.tail(self.RSI.get("turn_lookback", 5))
        if direction == Dir.BULL:
            return (recent.min() <= self.RSI.get("oversold", 30)) and (now > prev)
        else:
            return (recent.max() >= self.RSI.get("overbought", 70)) and (now < prev)

    # ------------------------------------------------------------------
    def _build_signal(self, direction, htf, ltf, choch_win, ltf_swings, pools, price):
        cfg = self.cfg
        entry = price
        entry_win = ltf.tail(self.LB["ltf_entry_fvg"])

        # entry location depends on the trigger:
        #   "choch" -> enter at MARKET (current price) on the CHoCH break (momentum)
        #   "fvg"   -> refine to the freshest M5 FVG mid (wait for a retrace)
        if cfg.BACKTEST.get("entry_trigger", "fvg") == "fvg":
            gaps = fvg_mod.unfilled(fvg_mod.find_fvgs(entry_win), direction)
            if gaps:
                g = gaps[-1]
                entry = (g.top + g.bottom) / 2.0   # mid of freshest gap

        # ---- COST MODEL: bake spread into the real fill price ----
        # BUY fills at the ask (+spread), SELL fills at the bid (-spread).
        # Everything below (SL, TP, RR) is measured from this REAL fill, so the
        # RR we report and filter on is the RR the trade will actually get.
        spread = cfg.BACKTEST["spread_points"] * self.point
        fill = entry + spread if direction == Dir.BULL else entry - spread

        # SL: immediate M5 swing, ATR-capped + buffer
        a = atr(ltf.tail(max(self.ATR["period"] + 2, self.LB["ltf_sl_ref"])),
                self.ATR["period"])
        buf = self.RISK["sl_buffer_points"] * self.point
        sl_cap = self.ATR["sl_cap_mult"] * a if a > 0 else None

        min_stop = self.RISK.get("min_stop_points", 0) * self.point
        # stop must dwarf the spread, else cost eats the trade
        min_stop = max(min_stop, self.RISK.get("stop_spread_mult", 0) * spread)

        if direction == Dir.BULL:
            sl_swing = st.last_swing(ltf_swings, "low")
            swing_lvl = sl_swing.price if sl_swing else float(entry_win["low"].min())
            # SL must sit below BOTH the swing low and the refined fill, then buffered
            sl = min(swing_lvl, fill) - buf
            if sl_cap:
                sl = max(sl, fill - sl_cap)    # cap how far the stop can be
            if sl >= fill:                     # safety: SL must be protective
                self.diag.reject("bad_stop", margin=0.0)
                return None
        else:
            sl_swing = st.last_swing(ltf_swings, "high")
            swing_lvl = sl_swing.price if sl_swing else float(entry_win["high"].max())
            sl = max(swing_lvl, fill) + buf
            if sl_cap:
                sl = min(sl, fill + sl_cap)
            if sl <= fill:                     # safety: SL must be protective
                self.diag.reject("bad_stop", margin=0.0)
                return None

        # TEST: widen the stop (and later the TP) by risk_mult, keeping RR constant.
        # Wider stops let small-stop setups clear the min-stop filter -> more trades.
        rm = getattr(cfg, "TEST", {}).get("risk_mult", 1.0)
        if rm and rm != 1.0:
            sl = (fill - rm * (fill - sl)) if direction == Dir.BULL \
                else (fill + rm * (sl - fill))
            sl = round(sl, cfg.INSTRUMENT["digits"])

        risk = abs(fill - sl)
        if risk < max(min_stop, 1e-9):         # reject micro stops / cost-dominated trades
            ratio = risk / min_stop if min_stop > 0 else 0.0
            self.diag.reject("bad_stop", margin=ratio)
            return None

        # TP: next M15 liquidity, or fixed RR fallback (measured from the real fill)
        tp = self._take_profit(direction, fill, risk, pools)
        if tp is None:
            self.diag.reject("no_tp")
            return None

        # TEST: widen the TP by the same factor so reward:risk is preserved
        if rm and rm != 1.0:
            tp = (fill + rm * (tp - fill)) if direction == Dir.BULL \
                else (fill - rm * (tp - fill))
            tp = round(tp, cfg.INSTRUMENT["digits"])

        # reward, net of a cost buffer for commission/slippage
        cost_buf = self.RISK.get("cost_buffer_points", 0) * self.point
        reward = abs(tp - fill) - cost_buf
        if reward <= 0:
            self.diag.reject("low_rr", margin=0.0)
            return None

        rr = reward / risk
        if rr < self.RISK["min_rr"]:
            self.diag.reject("low_rr", margin=rr)
            return None

        self.diag.success()
        lot = self._position_size(risk)
        return Signal(
            direction=direction, entry=round(fill, cfg.INSTRUMENT["digits"]),
            sl=round(sl, cfg.INSTRUMENT["digits"]), tp=round(tp, cfg.INSTRUMENT["digits"]),
            idx=int(ltf.index[-1]), rr=round(rr, 2), lot=lot,
            reason=f"{direction.value} MTF setup",
            meta={"atr": round(a, 3)},
        )

    # ------------------------------------------------------------------
    def _take_profit(self, direction, entry, risk, pools):
        if self.RISK["tp_mode"] == "liquidity":
            # require the liquidity target to be far enough to satisfy min_rr
            # (plus the cost buffer), so we skip too-close pools and reach the
            # next real target instead of producing a sub-minimum-RR setup.
            cost_buf = self.RISK.get("cost_buffer_points", 0) * self.point
            min_dist = self.RISK["min_rr"] * risk + cost_buf
            tgt = lq.next_target(pools, entry, direction, min_distance=min_dist)
            if tgt is not None:
                return tgt
            # no liquidity far enough:
            if self.RISK.get("tp_fallback", "rr") == "skip":
                return None                       # discard the setup
            # else fall through to a fixed-RR target

        # fallback: fixed RR
        rr = self.RISK["fixed_rr"]
        return entry + rr * risk if direction == Dir.BULL else entry - rr * risk

    def _position_size(self, risk_distance: float) -> float:
        inst = self.cfg.INSTRUMENT
        bal = self.RISK["account_balance"]
        risk_cash = bal * self.RISK["risk_per_trade"]
        cs = inst.get("contract_size", 100)          # oz per 1.0 lot
        if risk_distance <= 0:
            return 0.0
        # loss for 1.0 lot = risk_distance * contract_size  ->  solve for lot
        lot = risk_cash / (risk_distance * cs)

        step = inst.get("lot_step", 0.01)
        min_lot = inst.get("min_lot", 0.01)
        lot = min(lot, self.RISK["max_lot"])
        lot = round(round(lot / step) * step, 2)     # snap to broker step
        return max(lot, min_lot)                      # never below the broker minimum
