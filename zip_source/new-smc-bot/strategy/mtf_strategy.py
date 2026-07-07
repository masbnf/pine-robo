"""Executable, closed-candle implementation of SMC_clean_logic.pine.

The Pine source is an indicator. Its R:R boxes are made executable here as:
confirmed structure break -> OB limit entry -> opposite OB edge stop -> fixed TP.
"""
from __future__ import annotations

from typing import Optional
import math
import pandas as pd

from smc import fvg as fvg_mod
from smc import order_blocks as ob_mod
from smc import structure as st
from smc.warmup import WarmupState
from smc.types import Dir, Signal
from utils.diagnostics import Diagnostics


class MTFStrategy:
    """Original public name retained for compatibility with the backtest engine."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.clean = cfg.CLEAN_SMC
        self.point = cfg.INSTRUMENT["point"]
        self.diag = Diagnostics()
        self._last_bos: Optional[tuple] = None
        self._armed = {Dir.BULL: None, Dir.BEAR: None}
        self._swing_events = None
        self._internal_events = None
        self.warmup = WarmupState(cfg.WARMUP)

    def prepare(self, ltf: pd.DataFrame) -> None:
        """Precompute confirmed events once; lookup remains causal by break index."""
        swing_len = self.clean.get("swing_length", 10)
        internal_len = self.clean.get("internal_length", 5)
        self._swing_events = {
            e["break_idx"]: e for e in st.structure_events(ltf, swing_len, swing_len)
        }
        self._internal_events = {
            e["break_idx"]: e for e in st.structure_events(ltf, internal_len, internal_len)
        }

    def evaluate(self, htf: pd.DataFrame, ltf: pd.DataFrame) -> Optional[Signal]:
        self.diag.start()
        window = ltf.tail(self.clean.get("structure_lookback", 120))
        current_idx = int(ltf.index[-1])
        row = ltf.iloc[-1]
        self.warmup.update(htf)

        # 1) A confirmed swing BOS creates/refreshes the directional OB setup.
        if self._swing_events is not None:
            bos = self._swing_events.get(current_idx)
        else:
            swing_len = self.clean.get("swing_length", 10)
            events = st.structure_events(window, swing_len, swing_len)
            bos = events[-1] if events and events[-1]["break_idx"] == current_idx else None
        if bos is not None and bos["tag"] == "BOS":
                bos_key = (bos["direction"].value, bos["break_idx"], bos["pivot"].idx)
                if bos_key != self._last_bos:
                    self._last_bos = bos_key
                    block = ob_mod.find_extreme_order_block(
                        window, bos["pivot"].idx, bos["break_idx"],
                        bos["direction"], atr_period=200,
                    )
                    block_valid = block is not None and block.top > block.bottom
                    zone_allowed = (block_valid and
                                    (not self.cfg.WARMUP.get("require_zone_overlap", False) or
                                     self.warmup.zone_allows(bos["direction"], block)))
                    if (block_valid and
                            self.warmup.trend_allows(bos["direction"]) and zone_allowed):
                        self._armed[bos["direction"]] = {
                            "block": block, "bos": bos, "tapped": False,
                            "expires": current_idx + self.clean.get("retest_expiry_bars", 60),
                        }

        # 2) Invalidate/arm on OB retest. A wick touch is sufficient for a tap.
        for direction in (Dir.BULL, Dir.BEAR):
            armed = self._armed[direction]
            if armed is None:
                continue
            block = armed["block"]
            if current_idx > armed["expires"]:
                self._armed[direction] = None
                continue
            invalidated = (direction == Dir.BULL and float(row["close"]) < block.bottom) or \
                          (direction == Dir.BEAR and float(row["close"]) > block.top)
            if invalidated:
                self._armed[direction] = None
                continue
            touched = float(row["low"]) <= block.top and float(row["high"]) >= block.bottom
            if current_idx > armed["bos"]["break_idx"] and touched:
                armed["tapped"] = True

        # 3) After a tap, require a same-direction internal CHoCH on this close.
        if self._internal_events is not None:
            confirmation = self._internal_events.get(current_idx)
        else:
            internal_len = self.clean.get("internal_length", 5)
            events = st.structure_events(window, internal_len, internal_len)
            confirmation = events[-1] if events and events[-1]["break_idx"] == current_idx else None
        if confirmation is not None:
            direction = confirmation["direction"]
            armed = self._armed[direction]
            if (confirmation["break_idx"] == current_idx and
                    confirmation["tag"] == "CHoCH" and armed is not None and
                    armed["tapped"] and self.warmup.trend_allows(direction)):
                if self.clean.get("require_fvg_overlap", False):
                    gaps = fvg_mod.mark_filled(window, fvg_mod.find_fvgs(window))
                    live = fvg_mod.unfilled(gaps, direction)
                    block = armed["block"]
                    if not any(not (g.bottom > block.top or g.top < block.bottom) for g in live):
                        self.diag.reject("no_fvg_overlap")
                        return None
                if self.clean.get("htf_ema_enabled", False):
                    period = self.clean.get("htf_ema_period", 200)
                    if len(htf) < period:
                        self.diag.reject("not_enough_htf_ema")
                        return None
                    ema_value = float(htf["close"].ewm(span=period, adjust=False).mean().iloc[-1])
                    htf_close = float(htf["close"].iloc[-1])
                    against_trend = ((direction == Dir.BULL and htf_close < ema_value) or
                                     (direction == Dir.BEAR and htf_close > ema_value))
                    if against_trend:
                        self.diag.reject("htf_ema_trend")
                        return None
                signal = self._signal_from_confirmation(
                    direction, confirmation, armed["bos"], armed["block"],
                    current_idx, float(row["close"]),
                )
                self._armed[direction] = None
                if signal is not None:
                    self.diag.success()
                return signal

        self.diag.reject("waiting_bos_retest_choch")
        return None

    def _signal_from_confirmation(self, direction, confirmation, bos, block,
                                  current_idx, close):
        cfg = self.cfg
        spread = cfg.BACKTEST.get("spread_points", 0) * self.point
        entry = close + spread if direction == Dir.BULL else close - spread
        liquidity_stop = self.warmup.stop_liquidity(direction, entry)
        buffer = self.cfg.WARMUP.get("liquidity_sl_buffer_points", 0) * self.point
        if direction == Dir.BULL:
            sl = min(block.bottom, liquidity_stop) - buffer if liquidity_stop is not None else block.bottom - buffer
        else:
            sl = max(block.top, liquidity_stop) + buffer if liquidity_stop is not None else block.top + buffer
        risk = abs(entry - sl)
        min_stop = max(cfg.RISK.get("min_stop_points", 0) * self.point,
                       cfg.RISK.get("stop_spread_mult", 0.0) * spread,
                       self.point)
        if not math.isfinite(risk) or risk < min_stop:
            self.diag.reject("bad_stop", margin=(risk / min_stop if min_stop else 0.0))
            return None

        tp = self.warmup.target_liquidity(direction, entry)
        if tp is None or (direction == Dir.BULL and tp <= entry) or (direction == Dir.BEAR and tp >= entry):
            self.diag.reject("no_opposing_liquidity_target")
            return None
        reward = abs(tp - entry)
        rr = reward / risk
        digits = cfg.INSTRUMENT["digits"]
        return Signal(
            direction=direction,
            entry=round(entry, digits), sl=round(sl, digits), tp=round(tp, digits),
            idx=current_idx, rr=round(rr, 2), lot=self._position_size(risk),
            reason="H1 trend + M15 warmup zone + BOS/retest/internal CHoCH",
            meta={"zone_idx": block.idx, "event": "BOS_RETEST_CHOCH",
                  "pivot_idx": bos["pivot"].idx, "break_idx": bos["break_idx"],
                  "confirmation_idx": confirmation["break_idx"],
                  "ob_top": block.top, "ob_bottom": block.bottom},
        )

    def _position_size(self, risk_distance: float) -> float:
        inst = self.cfg.INSTRUMENT
        risk_cash = self.cfg.RISK["account_balance"] * self.cfg.RISK["risk_per_trade"]
        lot = risk_cash / (risk_distance * inst.get("contract_size", 100))
        lot = min(lot, self.cfg.RISK["max_lot"])
        step = inst.get("lot_step", 0.01)
        lot = math.floor(lot / step) * step
        return round(max(lot, inst.get("min_lot", 0.01)), 2)
