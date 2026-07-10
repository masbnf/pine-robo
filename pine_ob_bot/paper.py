from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import uuid4

from .config import BotConfig
from .entry_pivot import PIVOT_HIGH, EntryPivot
from .models import Candle, OrderBlock, PaperPosition, PendingOrder, Tick, Trade
from .position_sizing import CALCULATED_VOLUME_BELOW_BROKER_MINIMUM, calculate_safe_position_size
from .trend_filter import (ORDER_CANCELLED_M5_TREND_CHANGED, ORDER_CANCELLED_M5_TREND_NEUTRAL,
                          TRADE_REJECTED_M5_TREND_NEUTRAL, TrendDirection,
                          is_trade_allowed_by_m5_trend, validate_trade_direction)

REJECTED_NO_ENTRY_PIVOT = "REJECTED_NO_ENTRY_PIVOT"
PORTFOLIO_RISK_CAP_EXCEEDED = "PORTFOLIO_RISK_CAP_EXCEEDED"


def _experimental_stats_defaults() -> dict:
    """Telemetry keys for the flag-gated trade-frequency experiments.

    Present (zero) even with every flag off so summaries/CSVs have stable
    columns; none of them ever alters execution. Grouped by feature:
    multi-bar sweep/reclaim extras, controlled revival, controlled re-entry,
    spread wait, and position/portfolio-risk capacity.
    """
    return {
        "sweep_reclaim_reset_by_deeper_sweep": 0,
        "sweep_reclaim_cancelled_trend": 0,
        "sweep_reclaim_cancelled_ob_invalid": 0,
        "revival_suspended": 0,
        "revival_trend_returned": 0,
        "revival_returned_within_3": 0,
        "revival_returned_within_6": 0,
        "revival_returned_within_12": 0,
        "revival_rejected_too_late": 0,
        "revival_rejected_ob_invalid": 0,
        "revival_rejected_no_fresh_sweep": 0,
        "revival_rejected_attempt_limit": 0,
        "revival_orders_created": 0,
        "revival_orders_filled": 0,
        "revival_orders_cancelled": 0,
        "revival_wins": 0,
        "revival_losses": 0,
        "revival_total_r": 0.0,
        "reentry_candidates": 0,
        "reentry_rejected_ob_invalid": 0,
        "reentry_rejected_trend": 0,
        "reentry_rejected_wait": 0,
        "reentry_rejected_no_fresh_sweep": 0,
        "reentry_rejected_attempt_limit": 0,
        "reentry_orders_created": 0,
        "reentry_orders_filled": 0,
        "reentry_wins": 0,
        "reentry_losses": 0,
        "reentry_total_r": 0.0,
        "unique_orders_blocked_by_spread": 0,
        "spread_wait_started": 0,
        "spread_wait_eventually_filled": 0,
        "spread_wait_expired": 0,
        "spread_wait_cancelled_trend": 0,
        "spread_wait_cancelled_ob_invalid": 0,
        "spread_wait_rejected_sizing": 0,
        "spread_wait_duration_ticks_sum": 0,
        "spread_wait_duration_seconds_sum": 0.0,
        "spread_wait_max_duration_seconds": 0.0,
        "spread_at_confirmation_sum": 0.0,
        "spread_at_fill_sum": 0.0,
        "rejected_max_positions": 0,
        "setups_blocked_max_positions": 0,
        "rejected_portfolio_risk_cap": 0,
        "setups_blocked_portfolio_risk": 0,
        "max_observed_open_risk_fraction": 0.0,
    }


@dataclass(slots=True)
class SymbolSpec:
    tick_size: float = 0.01
    tick_value: float = 1.0
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01


class PaperBroker:
    """In-process execution simulator. It contains no MT5 order-send method."""
    def __init__(self, cfg: BotConfig, equity: float, spec: SymbolSpec | None = None):
        self.cfg = cfg
        self.initial_equity = float(equity)
        self.equity = float(equity)
        self.peak_equity = float(equity)
        self.max_drawdown = 0.0
        self.spec = spec or SymbolSpec()
        self.pending: list[PendingOrder] = []
        self.positions: list[PaperPosition] = []
        self.trades: list[Trade] = []
        self.market_index: int | None = None
        self.market_context: dict = {}
        self.entry_age_values: list[int] = []
        self.recent_spreads = deque(maxlen=self.cfg.spread_median_window)
        # Drained by the caller (PaperApp.on_tick / on_closed_candle, or the
        # historical runner) right after each process_tick/process_candle call
        # so every rejected fill is logged and persisted exactly once.
        self.rejected_sizing: list[dict] = []
        # The single, live M5 trend the strategy is allowed to trade with --
        # set exclusively via update_m5_trend(), derived from
        # PineSwingOBEngine.trend (BOS/CHoCH), never from M15 or OB direction.
        self.current_m5_trend: TrendDirection = TrendDirection.NEUTRAL
        # Drained the same way as rejected_sizing: every order rejected at
        # creation/fill by the trend filter, and every pending order
        # cancelled by a trend change, is queued here for logging/DB.
        self.trend_events: list[dict] = []
        # The most recently confirmed classic Entry Pivot of each kind (see
        # entry_pivot.py). Updated exclusively via update_entry_pivot(),
        # sourced from ClassicEntryPivotDetector -- an independent, symmetric
        # window used only for local pullback/trigger timing. It never
        # changes current_m5_trend and is never derived from it.
        self.last_pivot_high: EntryPivot | None = None
        self.last_pivot_low: EntryPivot | None = None
        # Off by default so every pre-existing add_ob() call site/unit test
        # that predates this feature (and never drives a pivot detector)
        # keeps its exact prior behaviour. The real execution paths (PaperApp,
        # run_historical, run_tick_historical) call enable_entry_pivot_gate()
        # once right after constructing the broker, so live trading and both
        # backtest runners always enforce it identically.
        self.entry_pivot_gate_active: bool = False
        self.pivot_events: list[dict] = []
        # Drained like rejected_sizing/trend_events: one entry per position
        # whose stop was just moved to breakeven, so the caller can log,
        # persist and (in Demo mode) mirror the SL modification exactly once.
        self.breakeven_events: list[dict] = []
        # Shadow-audit state (cfg.revival_shadow_audit). Pure observation:
        # nothing in these two lists can ever create or touch a real order.
        self.revival_shadow: list[dict] = []
        self.shadow_positions: list[dict] = []
        # --- Experimental trade-frequency state (flag-gated; stays empty and
        # untouched while the corresponding cfg flags are off). ---
        # Controlled Revival ledger (cfg.controlled_revival): one record per
        # trend-cancelled setup eligible for a REAL revival. The old order is
        # never re-activated; a revival always creates a new PendingOrder.
        # Kept separate from revival_shadow so --revival-shadow-audit and
        # --controlled-revival can run together without double counting:
        # shadow counters stay hypothetical (reactivation_*), real revivals
        # count only in revival_* and in actual trades.
        self.revival_records: list[dict] = []
        # ob_id -> number of revival orders CREATED for it. Persisted and
        # never reset on restart, so revival_max_per_ob survives a crash.
        self.revival_attempts: dict[str, int] = {}
        # ob_id -> entry-attempt bookkeeping for controlled re-entry
        # (cfg.allow_ob_reentry). "attempts" counts ORDER CREATIONS -- the
        # original entry is attempt 1 -- so max_entries_per_ob bounds total
        # attempts even when a re-entry order is cancelled before filling.
        self.ob_entry_history: dict[str, dict] = {}
        # OB ids the engine invalidated: the broker-side "is this OB still
        # valid" source used by revival/re-entry. Populated only while one of
        # those features is on (unbounded growth is pointless otherwise).
        self.invalidated_obs: set[str] = set()
        self.stats = {"setups_seen": 0, "rejected_break_kind": 0,
                      "rejected_weak_choch": 0,
                      "early_touch_blocked": 0, "lifecycle_accepted": 0,
                      "lifecycle_armed": 0, "spread_entry_blocked": 0,
                      "demo_entry_reverted": 0,
                      "rejected_volume_below_minimum": 0,
                      "rejected_risk_sizing": 0,
                      "rejected_trend_mismatch": 0,
                      "rejected_trend_neutral": 0,
                      "cancelled_trend_change": 0,
                      "cancelled_trend_neutral": 0,
                      "rejected_no_entry_pivot": 0,
                      "rejected_stale_entry_pivot": 0,
                      "accepted_fresh_entry_pivot": 0,
                      "entry_pivot_age_samples": 0,
                      "entry_pivot_age_sum_bars": 0,
                      "entry_pivot_age_max_bars": 0,
                      "breakeven_armed": 0,
                      "sweep_reclaim_checked": 0,
                      "sweep_reclaim_entry_swept": 0,
                      "sweep_reclaim_confirmed": 0,
                      "sweep_reclaim_invalidated": 0,
                      "sweep_reclaim_filled": 0,
                      "sweep_reclaim_window_expired": 0,
                      "sweep_reclaim_lag0_confirms": 0,
                      "sweep_reclaim_lag1_confirms": 0,
                      "sweep_reclaim_lag2plus_confirms": 0,
                      "trend_suspended": 0,
                      "trend_returned_3": 0,
                      "trend_returned_6": 0,
                      "trend_returned_12": 0,
                      "reactivation_ob_still_valid": 0,
                      "reactivation_fresh_sweep": 0,
                      "reactivation_confirmed": 0,
                      "reactivation_would_fill": 0,
                      "reactivation_wins": 0,
                      "reactivation_losses": 0,
                      "reactivation_hypothetical_r": 0.0,
                      "pivot_highs_confirmed": 0,
                      "pivot_lows_confirmed": 0,
                      "buy_setups_created": 0,
                      "sell_setups_created": 0,
                      **_experimental_stats_defaults()}

    @property
    def position(self) -> PaperPosition | None:
        """Backward-compatible view of the oldest open position."""
        return self.positions[0] if self.positions else None

    def add_ob(self, ob: OrderBlock, extra_meta: dict | None = None) -> PendingOrder | None:
        self.stats["setups_seen"] += 1
        if ob.break_kind != "unknown" and ob.break_kind not in self.cfg.allowed_break_kinds:
            self.stats["rejected_break_kind"] += 1
            return None
        if (self.cfg.strong_choch_only and ob.break_kind == "CHoCH" and
                not (extra_meta or {}).get("bos_displacement_top_quartile")):
            self.stats["rejected_weak_choch"] += 1
            return None
        if ob.high <= ob.low or any(x.ob_id == ob.id for x in self.pending):
            return None
        trend_check = validate_trade_direction(ob.direction, self.current_m5_trend)
        if not trend_check.is_allowed:
            counter = ("rejected_trend_neutral"
                      if trend_check.rejection_reason == TRADE_REJECTED_M5_TREND_NEUTRAL
                      else "rejected_trend_mismatch")
            self.stats[counter] = self.stats.get(counter, 0) + 1
            self.trend_events.append({
                "event_type": "order_rejected_m5_trend", "order_id": None, "ob_id": ob.id,
                "side": ob.direction, "current_m5_trend": self.current_m5_trend.value,
                "trend_at_creation": self.current_m5_trend.value,
                "reason": trend_check.rejection_reason, "time": ob.formed_time,
                "signal_type": ob.break_kind,
            })
            return None
        # Classic Entry Pivot admission gate (5/5 by default): a matching-
        # direction pivot must already be confirmed before this Major
        # BOS/CHoCH-formed OB is allowed to become a tradeable setup. The
        # pivot only confirms/triggers this already-formed OB -- it never
        # creates a new, independent counter-trend Order Block on its own.
        # Inactive unless enable_entry_pivot_gate() has been called (see
        # __init__), which every real execution path does.
        required_pivot: EntryPivot | None = None
        pivot_confirmation_index: int | None = None
        entry_pivot_age_bars: int | None = None
        if self.entry_pivot_gate_active:
            required_pivot = (self.last_pivot_low if ob.direction == "bull"
                             else self.last_pivot_high)
            if required_pivot is None:
                self.stats["rejected_no_entry_pivot"] = self.stats.get(
                    "rejected_no_entry_pivot", 0) + 1
                self.trend_events.append({
                    "event_type": "order_rejected_entry_pivot", "order_id": None,
                    "ob_id": ob.id, "side": ob.direction,
                    "current_m5_trend": self.current_m5_trend.value,
                    "trend_at_creation": self.current_m5_trend.value,
                    "reason": REJECTED_NO_ENTRY_PIVOT, "time": ob.formed_time,
                    "signal_type": ob.break_kind,
                })
                return None
            # Freshness (max_entry_pivot_age_bars): age is measured from the
            # pivot's CONFIRMATION bar (candle_index + right_bars), never from
            # the pivot candle itself, so asymmetric left/right windows keep
            # their exact meaning: left only shapes WHICH bar can be a pivot,
            # right alone sets the causal confirmation delay.
            pivot_confirmation_index = (required_pivot.candle_index +
                                        required_pivot.right_bars)
            entry_pivot_age_bars = ob.formed_index - pivot_confirmation_index
            if entry_pivot_age_bars < 0:
                # Defensive lookahead guard: a pivot whose right-side window
                # has not fully closed by this setup's bar must behave exactly
                # like a missing pivot.
                self.stats["rejected_no_entry_pivot"] = self.stats.get(
                    "rejected_no_entry_pivot", 0) + 1
                return None
            max_age = self.cfg.max_entry_pivot_age_bars
            if max_age is not None and entry_pivot_age_bars > max_age:
                self.stats["rejected_stale_entry_pivot"] = self.stats.get(
                    "rejected_stale_entry_pivot", 0) + 1
                self.trend_events.append({
                    "event_type": "order_rejected_stale_entry_pivot",
                    "order_id": None, "ob_id": ob.id, "side": ob.direction,
                    "current_m5_trend": self.current_m5_trend.value,
                    "trend_at_creation": self.current_m5_trend.value,
                    "reason": "ORDER_REJECTED_STALE_ENTRY_PIVOT",
                    "time": ob.formed_time, "signal_type": ob.break_kind,
                    "entry_pivot_age_bars": entry_pivot_age_bars,
                    "max_entry_pivot_age_bars": max_age,
                })
                return None
            self.stats["accepted_fresh_entry_pivot"] = self.stats.get(
                "accepted_fresh_entry_pivot", 0) + 1
            self.stats["entry_pivot_age_samples"] = self.stats.get(
                "entry_pivot_age_samples", 0) + 1
            self.stats["entry_pivot_age_sum_bars"] = self.stats.get(
                "entry_pivot_age_sum_bars", 0) + entry_pivot_age_bars
            self.stats["entry_pivot_age_max_bars"] = max(
                self.stats.get("entry_pivot_age_max_bars", 0), entry_pivot_age_bars)
        risk = ob.high - ob.low
        target = ob.entry + self.cfg.rr * risk if ob.direction == "bull" else ob.entry - self.cfg.rr * risk
        lifecycle = "formed" if self.cfg.entry_lifecycle_enabled else "armed"
        order = PendingOrder(uuid4().hex, ob.id, ob.direction, ob.entry, ob.stop,
                             target, ob.formed_index, ob.formed_time, True,
                             {"break_kind": ob.break_kind,
                              "atr_at_formation": ob.atr_at_formation,
                              # Original OB edges, immutable references for the
                              # revival/re-entry experiments (order.stop gets
                              # ATR-buffered on sweep confirmation, so the raw
                              # zone boundary must be kept separately).
                              "ob_entry_price": ob.entry,
                              "ob_stop_price": ob.stop,
                              "ob_width": risk,
                              "ob_width_atr": risk / ob.atr_at_formation
                              if ob.atr_at_formation else None,
                              "source_index": ob.source_index,
                              "trend_at_creation": self.current_m5_trend.value,
                              "trend_swing_length": self.cfg.trend_swing_length,
                              "entry_pivot_left": self.cfg.entry_pivot_left,
                              "entry_pivot_right": self.cfg.entry_pivot_right,
                              "m5_trend_at_setup": self.current_m5_trend.value,
                              "entry_pivot_kind": required_pivot.kind if required_pivot else None,
                              "entry_pivot_price": required_pivot.price if required_pivot else None,
                              "entry_pivot_time": required_pivot.pivot_time if required_pivot else None,
                              "entry_pivot_confirmed_at": required_pivot.confirmed_at
                              if required_pivot else None,
                              "entry_pivot_type": (required_pivot.kind.replace("pivot_", "")
                                                   if required_pivot else None),
                              "entry_pivot_candle_index": (required_pivot.candle_index
                                                           if required_pivot else None),
                              "entry_pivot_confirmation_index": pivot_confirmation_index,
                              "entry_pivot_left_bars": (required_pivot.left_bars
                                                        if required_pivot else None),
                              "entry_pivot_right_bars": (required_pivot.right_bars
                                                         if required_pivot else None),
                              "entry_pivot_age_bars_at_setup": entry_pivot_age_bars,
                              "max_entry_pivot_age_bars": self.cfg.max_entry_pivot_age_bars,
                              **(extra_meta or {})}, lifecycle)
        self.pending.append(order)
        self.pending.sort(key=lambda x: (x.created_index, x.created_time))
        setup_counter = "buy_setups_created" if ob.direction == "bull" else "sell_setups_created"
        self.stats[setup_counter] = self.stats.get(setup_counter, 0) + 1
        if self.cfg.allow_ob_reentry:
            # The original entry is attempt 1 of max_entries_per_ob.
            history = self.ob_entry_history.setdefault(ob.id, {"attempts": 0})
            history["attempts"] = history.get("attempts", 0) + 1
            order.meta["ob_entry_attempt"] = history["attempts"]
            order.meta["is_reentry"] = False
        return order

    def enable_entry_pivot_gate(self) -> None:
        """Activate the mandatory classic Entry Pivot admission gate.

        Must be called once by any real execution path (PaperApp live,
        run_historical, run_tick_historical) immediately after constructing
        the broker, so Live, Historical and Tick-Historical always enforce
        the same rule. Left inactive by default so every pre-existing
        add_ob() unit test that predates this feature -- and never drives a
        pivot detector -- keeps its exact prior behaviour.
        """
        self.entry_pivot_gate_active = True

    def update_entry_pivot(self, pivot: EntryPivot) -> None:
        """Record the latest confirmed classic Entry Pivot of its kind.

        Must be called once per newly confirmed pivot, after update_m5_trend()
        for the same closed candle and before any add_ob() call for that
        candle's own newly formed OBs -- see PaperApp.on_closed_candle,
        run_historical and run_tick_historical for the shared ordering. This
        never touches current_m5_trend: a quick pivot can confirm/trigger a
        setup, it can never change the M5 Trend State by itself.
        """
        counter = "pivot_highs_confirmed" if pivot.kind == PIVOT_HIGH else "pivot_lows_confirmed"
        self.stats[counter] = self.stats.get(counter, 0) + 1
        if pivot.kind == PIVOT_HIGH:
            self.last_pivot_high = pivot
        else:
            self.last_pivot_low = pivot
        self.pivot_events.append({
            "event_type": "entry_pivot_confirmed", "kind": pivot.kind,
            "price": pivot.price, "candle_index": pivot.candle_index,
            "pivot_time": pivot.pivot_time, "confirmed_at": pivot.confirmed_at,
            "left_bars": pivot.left_bars, "right_bars": pivot.right_bars,
        })

    def cancel_ob(self, ob_id: str) -> None:
        for order in self.pending:
            if order.ob_id == ob_id:
                if order.active and self.cfg.entry_mode == "sweep_reclaim":
                    self.stats["sweep_reclaim_invalidated"] = self.stats.get(
                        "sweep_reclaim_invalidated", 0) + 1
                    if order.meta.get("sweep_reclaim_sweep_index") is not None:
                        self.stats["sweep_reclaim_cancelled_ob_invalid"] += 1
                if order.active:
                    self._note_experimental_cancel(order, "ob_invalid")
                order.active = False
                order.lifecycle_state = "invalidated"
        for rec in self.revival_shadow:
            if rec["ob_id"] == ob_id and rec["state"] in ("suspended", "awaiting"):
                rec["state"] = "dead_ob"
        if self.cfg.controlled_revival or self.cfg.allow_ob_reentry:
            self.invalidated_obs.add(ob_id)
        if self.cfg.controlled_revival:
            for rec in self.revival_records:
                if rec["ob_id"] == ob_id and rec["state"] == "suspended":
                    rec["state"] = "rejected_ob_invalid"
                    self.stats["revival_rejected_ob_invalid"] += 1
        if self.cfg.allow_ob_reentry:
            history = self.ob_entry_history.get(ob_id)
            if history and history.get("candidate_state") == "waiting":
                history["candidate_state"] = "rejected_ob_invalid"
                self.stats["reentry_rejected_ob_invalid"] += 1

    def update_m5_trend(self, new_trend: TrendDirection, when: str) -> int:
        """Apply the latest M5 trend (from PineSwingOBEngine.trend) and cancel
        every active pending order that is no longer aligned with it.

        Must be called once per closed M5 candle, right after the structure
        engine processes that candle and before any new order is created or
        any subsequent tick/candle fill is evaluated -- otherwise a stale
        pending order could still fill against the new trend.
        """
        previous = self.current_m5_trend
        self.current_m5_trend = new_trend
        if previous == new_trend:
            return 0
        cancelled = 0
        neutral = new_trend == TrendDirection.NEUTRAL
        reason = ORDER_CANCELLED_M5_TREND_NEUTRAL if neutral else ORDER_CANCELLED_M5_TREND_CHANGED
        counter = "cancelled_trend_neutral" if neutral else "cancelled_trend_change"
        for order in self._active_pending():
            if is_trade_allowed_by_m5_trend(order.direction, new_trend):
                continue
            if (self.cfg.entry_mode == "sweep_reclaim" and
                    order.meta.get("sweep_reclaim_sweep_index") is not None):
                self.stats["sweep_reclaim_cancelled_trend"] += 1
            self._note_experimental_cancel(order, "trend")
            order.active = False
            order.lifecycle_state = "cancelled_trend"
            order.meta.update({"cancellation_reason": reason,
                              "trend_at_cancellation": new_trend.value})
            self.stats[counter] = self.stats.get(counter, 0) + 1
            self.trend_events.append({
                "event_type": "order_cancelled_m5_trend_change", "order_id": order.id,
                "ob_id": order.ob_id, "side": order.direction,
                "current_m5_trend": new_trend.value, "previous_m5_trend": previous.value,
                "trend_at_creation": order.meta.get("trend_at_creation"),
                "reason": reason, "time": when,
                "signal_type": order.meta.get("break_kind"),
            })
            cancelled += 1
            # Shadow audit: the real order above is cancelled exactly as
            # before; the shadow record merely remembers it for observation.
            if (self.cfg.revival_shadow_audit and
                    self.cfg.entry_mode == "sweep_reclaim" and
                    self.market_index is not None):
                self.revival_shadow.append({
                    "order_id": order.id, "ob_id": order.ob_id,
                    "direction": order.direction, "entry": order.entry,
                    "stop": order.stop,
                    "atr": order.meta.get("atr_at_formation") or 0.0,
                    "cancel_index": self.market_index,
                    "state": "suspended", "swept": False,
                })
                self.stats["trend_suspended"] = self.stats.get("trend_suspended", 0) + 1
            # Controlled Revival (REAL, flag-gated): remember this setup as
            # suspended. The cancelled order above stays cancelled forever; a
            # revival, if it ever happens, builds a brand-new order. Weak
            # CHoCH is never eligible. Runs independently of the shadow audit
            # (separate ledgers, separate counters -- no double counting).
            if (self.cfg.controlled_revival and
                    self.cfg.entry_mode == "sweep_reclaim" and
                    self.market_index is not None and
                    self._revival_eligible_break(order.meta)):
                if self.revival_attempts.get(order.ob_id, 0) >= self.cfg.revival_max_per_ob:
                    self.stats["revival_rejected_attempt_limit"] += 1
                else:
                    self.revival_records.append({
                        "parent_order_id": order.id, "ob_id": order.ob_id,
                        "direction": order.direction,
                        "entry": order.meta.get("ob_entry_price", order.entry),
                        "base_stop": order.meta.get("ob_stop_price", order.stop),
                        "atr": order.meta.get("atr_at_formation") or 0.0,
                        "break_kind": order.meta.get("break_kind"),
                        "strong_choch": bool(order.meta.get("bos_displacement_top_quartile")),
                        "cancel_index": self.market_index,
                        "cancel_time": when,
                        "state": "suspended",
                        "parent_meta_keys": None,
                    })
                    self.stats["revival_suspended"] += 1
        return cancelled

    def _revival_eligible_break(self, meta: dict) -> bool:
        """Revival follows the original setup's break kind: BOS always, CHoCH
        only when it was a strong (top-quartile displacement) CHoCH. A weak
        CHoCH is never revived under any configuration."""
        kind = meta.get("break_kind")
        if kind == "BOS":
            return True
        return kind == "CHoCH" and bool(meta.get("bos_displacement_top_quartile"))

    def reconcile_entry_filters(self) -> int:
        """Cancel restored pending orders that the current strategy disallows."""
        cancelled = 0
        for order in self.pending:
            kind = order.meta.get("break_kind", "unknown")
            if order.active and kind not in self.cfg.allowed_break_kinds:
                order.active = False
                order.lifecycle_state = "invalidated"
                cancelled += 1
        return cancelled

    def reconcile_pending_targets(self) -> int:
        """Apply the configured RR to pending orders, never to an open trade."""
        changed = 0
        for order in self.pending:
            if not order.active:
                continue
            risk = abs(order.entry - order.stop)
            target = (order.entry + self.cfg.rr * risk if order.direction == "bull"
                      else order.entry - self.cfg.rr * risk)
            if abs(target - order.target) > 1e-12:
                order.target = target
                changed += 1
        return changed

    def set_market_context(self, context: dict) -> None:
        self.market_context = dict(context)

    def process_tick(self, tick: Tick) -> list[Trade]:
        closed: list[Trade] = []
        spread = max(0.0, tick.ask - tick.bid)
        if self.positions:
            for p in list(self.positions):
                px = tick.bid if p.direction == "bull" else tick.ask
                self._track_excursion(p, px, px)
                self._apply_breakeven(p, tick.time)
                stop = px <= p.stop if p.direction == "bull" else px >= p.stop
                target = px >= p.target if p.direction == "bull" else px <= p.target
                if stop:
                    closed.append(self._close(p, px, tick.time, "loss", execution_source="tick",
                                              execution_spread=spread))
                elif target:
                    closed.append(self._close(p, px, tick.time, "win", execution_source="tick",
                                              execution_spread=spread))
            self._observe_spread(spread)
            return closed
        slots = self.cfg.max_open_positions - len(self.positions)
        for order in self._active_pending():
            if slots <= 0:
                # Telemetry only: remaining fill-eligible orders stay active
                # and simply retry on a later tick, exactly as before.
                self._count_max_positions_block(order)
                continue
            if order.lifecycle_state != "armed":
                continue
            if (self.market_index is not None and
                    self.market_index - order.created_index < self.cfg.min_entry_wait_bars):
                continue
            px = tick.ask if order.direction == "bull" else tick.bid
            if (self.cfg.entry_mode == "sweep_reclaim" and
                    not order.meta.get("sweep_reclaim_confirmed")):
                continue
            invalid = px <= order.stop if order.direction == "bull" else px >= order.stop
            if invalid:
                order.active = False
                order.lifecycle_state = "invalidated"
                continue
            if self.cfg.entry_mode == "sweep_reclaim":
                # The closed candle confirms the setup, but the next live tick
                # supplies the only executable fill price.
                if not self._entry_spread_allowed(order, spread):
                    self.stats["spread_entry_blocked"] += 1
                    self._spread_wait_on_block(order, tick, spread)
                    continue
                distance = abs(px - order.stop)
                if distance <= 0:
                    order.active = False
                    order.lifecycle_state = "invalidated"
                    continue
                target = (px + self.cfg.rr * distance if order.direction == "bull"
                          else px - self.cfg.rr * distance)
                position = self._open(order, px, tick.time, None, "tick", spread,
                                      stop=order.stop, target=target)
                if position is not None:
                    self.stats["sweep_reclaim_filled"] = self.stats.get(
                        "sweep_reclaim_filled", 0) + 1
                    self._spread_wait_finalize(position, tick, spread)
                    slots -= 1
                else:
                    self._spread_wait_after_reject(order)
                continue
            fill = px <= order.entry if order.direction == "bull" else px >= order.entry
            if fill:
                if not self._entry_spread_allowed(order, spread):
                    self.stats["spread_entry_blocked"] += 1
                    self._spread_wait_on_block(order, tick, spread)
                    continue
                position = self._open(order, px, tick.time, None, "tick", spread)
                if position is not None:
                    self._spread_wait_finalize(position, tick, spread)
                    slots -= 1
                else:
                    self._spread_wait_after_reject(order)
        self._observe_spread(spread)
        return closed

    def process_signal_candle(self, candle: Candle, bar_index: int) -> None:
        """Advance candle-driven state without simulating any execution.

        Live execution is tick-only. Closed candles may advance the optional
        order-block lifecycle and the market index used by entry-age guards,
        but they must never fill pending orders or close open positions.
        Historical OHLC replay continues to use ``process_candle``.
        """
        self.market_index = bar_index
        self._advance_lifecycle(candle, bar_index)
        if self.cfg.entry_mode != "sweep_reclaim":
            return
        if self.cfg.revival_shadow_audit:
            self._revival_shadow_on_candle(candle, bar_index)
        self._experimental_on_candle(candle, bar_index)
        for order in self._active_pending():
            if order.lifecycle_state != "armed":
                continue
            if order.meta.get("sweep_reclaim_confirmed"):
                continue
            if bar_index - order.created_index < max(1, self.cfg.min_entry_wait_bars):
                continue
            self.stats["sweep_reclaim_checked"] = self.stats.get("sweep_reclaim_checked", 0) + 1
            reclaimed, lag = self._sweep_reclaim_window(order, candle, bar_index)
            if not reclaimed:
                continue
            atr = order.meta.get("atr_at_formation") or 0.0
            buffer = atr * self.cfg.sweep_reclaim_atr_buffer
            order.stop = (order.stop - buffer if order.direction == "bull"
                          else order.stop + buffer)
            order.meta.update({"entry_mode": "sweep_reclaim",
                               "sweep_reclaim_atr_buffer": self.cfg.sweep_reclaim_atr_buffer,
                               "sweep_reclaim_confirmed": True,
                               "sweep_reclaim_lag_bars": lag,
                               "reclaim_close": candle.close,
                               "reclaim_time": candle.time})
            self.stats["sweep_reclaim_confirmed"] = self.stats.get(
                "sweep_reclaim_confirmed", 0) + 1

    def process_candle(self, candle: Candle, bar_index: int, spread: float = 0.0,
                       execution_source: str = "candle_replay") -> list[Trade]:
        """Replay fallback. Existing positions are checked before pending fills.

        If both boundaries occur in one OHLC candle, SL wins. New orders are not
        eligible on their formation candle, preventing look-ahead.
        """
        self.market_index = bar_index
        # Same experimental per-bar driver as the tick path, so Controlled
        # Revival / Re-entry behave identically on OHLC replay. Runs before
        # exits/fills; orders it creates carry created_index=bar_index and
        # therefore cannot fill on this same bar (min-wait guard).
        self._experimental_on_candle(candle, bar_index)
        closed: list[Trade] = []
        if self.positions:
            for p in list(self.positions):
                # Never apply a delayed candle to a position opened later.
                if _is_after(p.opened_time, candle.time):
                    continue
                self._track_excursion(p, candle.low, candle.high)
                # Intrabar ordering is unknown on OHLC: if one candle both
                # reaches the trigger and returns to entry, this counts as a
                # breakeven exit (the conservative reading, never a held win).
                self._apply_breakeven(p, candle.time)
                hit_sl = candle.low <= p.stop if p.direction == "bull" else candle.high + spread >= p.stop
                hit_tp = candle.high >= p.target if p.direction == "bull" else candle.low + spread <= p.target
                if hit_sl:
                    closed.append(self._close(p, p.stop, candle.time, "loss", bar_index,
                                              execution_source, spread))
                elif hit_tp:
                    closed.append(self._close(p, p.target, candle.time, "win", bar_index,
                                              execution_source, spread))
            self._advance_lifecycle(candle, bar_index)
            return closed
        slots = self.cfg.max_open_positions
        for order in self._active_pending():
            if slots <= 0:
                # Telemetry only: the order stays active and retries later.
                self._count_max_positions_block(order)
                continue
            if order.lifecycle_state != "armed":
                continue
            age = bar_index - order.created_index
            if age < max(1, self.cfg.min_entry_wait_bars):
                touched_early = candle.low + spread <= order.entry if order.direction == "bull" else candle.high >= order.entry
                if touched_early and not order.meta.get("early_touch_blocked"):
                    order.meta["early_touch_blocked"] = True
                    self.stats["early_touch_blocked"] += 1
                continue
            if self.cfg.entry_mode == "sweep_reclaim":
                if self._try_sweep_reclaim(order, candle, bar_index, spread,
                                           execution_source):
                    slots -= 1
                continue
            # Buys fill on Ask and sells on Bid.
            touched = candle.low + spread <= order.entry if order.direction == "bull" else candle.high >= order.entry
            if not touched:
                continue
            p = self._open(order, order.entry, candle.time, bar_index, execution_source, spread)
            if p is None:
                continue
            slots -= 1
            self._track_excursion(p, candle.low, candle.high)
            self._apply_breakeven(p, candle.time)
            hit_sl = candle.low <= p.stop if p.direction == "bull" else candle.high + spread >= p.stop
            hit_tp = candle.high >= p.target if p.direction == "bull" else candle.low + spread <= p.target
            if hit_sl:
                closed.append(self._close(p, p.stop, candle.time, "loss", bar_index,
                                          execution_source, spread))
            elif hit_tp:
                closed.append(self._close(p, p.target, candle.time, "win", bar_index,
                                          execution_source, spread))
        self._advance_lifecycle(candle, bar_index)
        return closed

    def reject_open(self, position_id: str, reason: str) -> PaperPosition | None:
        """Rollback a paper fill when its required Demo mirror is rejected."""
        position = next((item for item in self.positions if item.id == position_id), None)
        if position is None:
            return None
        position.meta.update({"demo_order_error": reason, "paper_open_reverted": True})
        self.positions.remove(position)
        self.stats["demo_entry_reverted"] += 1
        return position

    def apply_demo_entry(self, position: PaperPosition, price: float, volume: float) -> None:
        """Synchronize the paper ledger to the confirmed broker execution."""
        old_entry, old_volume, old_risk = position.entry, position.volume, position.risk_money
        position.entry = float(price)
        position.volume = float(volume)
        distance = abs(position.entry - position.stop)
        position.risk_money = (distance / self.spec.tick_size * self.spec.tick_value
                               * position.volume)
        position.meta.update({"paper_entry_before_demo": old_entry,
                              "paper_volume_before_demo": old_volume,
                              "paper_risk_before_demo": old_risk,
                              "actual_entry": position.entry,
                              "actual_volume": position.volume,
                              "actual_risk_money": position.risk_money,
                              "planned_rr": (abs(position.target - position.entry) / distance
                                             if distance else 0.0),
                              "execution_state": "demo_confirmed_active"})

    def apply_demo_exit(self, trade: Trade, price: float,
                        volume: float | None = None) -> None:
        """Reprice a just-closed trade and equity from the confirmed broker exit."""
        old_pnl, old_exit = trade.pnl, trade.exit_price
        trade.exit_price = float(price)
        if volume is not None and float(volume) > 0:
            trade.volume = float(volume)
        move = (trade.exit_price - trade.entry if trade.direction == "bull"
                else trade.entry - trade.exit_price)
        trade.pnl = move / self.spec.tick_size * self.spec.tick_value * trade.volume
        trade.r_multiple = trade.pnl / trade.risk_money if trade.risk_money else 0.0
        trade.result = "win" if trade.pnl > 0 else "loss"
        self.equity += trade.pnl - old_pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        drawdown = (self.peak_equity - self.equity) / self.peak_equity if self.peak_equity else 0.0
        self.max_drawdown = max(self.max_drawdown, drawdown)
        trade.meta.update({"paper_exit_before_demo": old_exit,
                           "actual_exit": trade.exit_price,
                           "actual_exit_volume": trade.volume,
                           "actual_pnl": trade.pnl,
                           "actual_r_multiple": trade.r_multiple})

    def _entry_spread_allowed(self, order: PendingOrder, spread: float) -> bool:
        if not self.cfg.entry_spread_guard_enabled:
            return True
        limits: list[float] = []
        atr = order.meta.get("atr_at_formation")
        if atr and atr > 0:
            limits.append(float(atr) * self.cfg.max_entry_spread_atr_fraction)
        history = list(self.recent_spreads)
        if len(history) >= self.cfg.spread_median_min_samples:
            limits.append(statistics.median(history) * self.cfg.spread_median_multiplier)
        return not limits or spread <= min(limits)

    def _observe_spread(self, spread: float) -> None:
        if math.isfinite(spread) and spread >= 0:
            self.recent_spreads.append(spread)

    def _revival_shadow_on_candle(self, candle: Candle, bar_index: int) -> None:
        """Advance the revival shadow audit by one closed bar. Observation only.

        Rules (docs/ENTRY_FUNNEL_FINDINGS.md follow-up): a trend-cancelled
        setup is tracked while its OB stays valid; the trend must return
        within 12 closed bars (bucketed 3/6/12 for reporting); after the
        return a FRESH same-bar sweep+reclaim is required (a reclaim before
        the trend returned never counts); each OB is revived at most once;
        the hypothetical fill uses close +/- fallback_spread, the ATR-buffered
        stop and the fixed-RR target, then walks candles SL-first.

        Alignment uses current_m5_trend, i.e. the trend as of the PREVIOUS
        closed bar, because live ordering runs this before update_m5_trend --
        one bar of confirmation conservatism, identical to how real pending
        orders experience the trend gate.
        """
        for pos in list(self.shadow_positions):
            if pos["direction"] == "bull":
                hit_sl = candle.low <= pos["stop"]
                hit_tp = candle.high >= pos["target"]
            else:
                hit_sl = candle.high >= pos["stop"]
                hit_tp = candle.low <= pos["target"]
            if not (hit_sl or hit_tp):
                continue
            result_r = -1.0 if hit_sl else self.cfg.rr
            key = "reactivation_losses" if hit_sl else "reactivation_wins"
            self.stats[key] = self.stats.get(key, 0) + 1
            self.stats["reactivation_hypothetical_r"] = round(
                self.stats.get("reactivation_hypothetical_r", 0.0) + result_r, 6)
            self.shadow_positions.remove(pos)
        for rec in self.revival_shadow:
            if rec["state"] not in ("suspended", "awaiting"):
                continue
            want = "bullish" if rec["direction"] == "bull" else "bearish"
            aligned = self.current_m5_trend.value == want
            if rec["state"] == "suspended":
                lag = bar_index - rec["cancel_index"]
                if aligned:
                    if lag <= 3:
                        bucket = "trend_returned_3"
                    elif lag <= 6:
                        bucket = "trend_returned_6"
                    elif lag <= 12:
                        bucket = "trend_returned_12"
                    else:
                        rec["state"] = "return_too_late"
                        continue
                    self.stats[bucket] = self.stats.get(bucket, 0) + 1
                    self.stats["reactivation_ob_still_valid"] = self.stats.get(
                        "reactivation_ob_still_valid", 0) + 1
                    rec["state"] = "awaiting"
                    rec["revival_index"] = bar_index
                elif lag > 12:
                    rec["state"] = "expired_no_return"
                continue
            # awaiting: one revival window only -- if the trend leaves again,
            # the record is finished for good.
            if not aligned:
                rec["state"] = "trend_left_again"
                continue
            if rec["direction"] == "bull":
                swept = candle.low < rec["entry"]
                reclaimed = swept and candle.close > rec["entry"]
                fill = candle.close + self.cfg.fallback_spread
            else:
                swept = candle.high > rec["entry"]
                reclaimed = swept and candle.close < rec["entry"]
                fill = candle.close
            if swept and not rec["swept"]:
                rec["swept"] = True
                self.stats["reactivation_fresh_sweep"] = self.stats.get(
                    "reactivation_fresh_sweep", 0) + 1
            if not reclaimed:
                continue
            self.stats["reactivation_confirmed"] = self.stats.get(
                "reactivation_confirmed", 0) + 1
            buffer = rec["atr"] * self.cfg.sweep_reclaim_atr_buffer
            stop = (rec["stop"] - buffer if rec["direction"] == "bull"
                    else rec["stop"] + buffer)
            distance = abs(fill - stop)
            if distance <= 0:
                rec["state"] = "confirmed_unfillable"
                continue
            target = (fill + self.cfg.rr * distance if rec["direction"] == "bull"
                      else fill - self.cfg.rr * distance)
            self.stats["reactivation_would_fill"] = self.stats.get(
                "reactivation_would_fill", 0) + 1
            rec["state"] = "filled_shadow"
            self.shadow_positions.append({
                "direction": rec["direction"], "fill": fill, "stop": stop,
                "target": target, "ob_id": rec["ob_id"],
                "opened_index": bar_index, "opened_time": candle.time,
            })

    def _sweep_reclaim_window(self, order: PendingOrder, candle: Candle,
                              bar_index: int) -> tuple[bool, int]:
        """Shared sweep/reclaim state machine for both execution paths.

        The sweep is measured against the OB ENTRY edge, never the stop: a
        candle trading through the stop extreme is the exact condition under
        which the OB engine invalidates the block on this same candle
        (cancel_ob then deactivates the order), so a stop-based sweep could
        never survive to a fill.

        With sweep_reclaim_max_bars == 1 (the default) this is exactly the
        original same-bar rule: a bar must wick through the entry edge AND
        close back beyond it. With N > 1 (experimental, flag-gated), the sweep
        bar opens a window of N closed bars; any bar inside it that closes
        beyond the edge confirms, a deeper sweep restarts the window, and an
        expired window resets the order to the not-swept state. Returns
        (reclaim confirmed, closed bars between sweep and reclaim).
        """
        swept_now = (candle.low < order.entry if order.direction == "bull"
                     else candle.high > order.entry)
        sweep_index = order.meta.get("sweep_reclaim_sweep_index")
        window = max(1, self.cfg.sweep_reclaim_max_bars)
        if sweep_index is not None and bar_index - sweep_index >= window:
            order.meta.pop("sweep_reclaim_sweep_index", None)
            order.meta.pop("sweep_reclaim_sweep_price", None)
            self.stats["sweep_reclaim_window_expired"] = self.stats.get(
                "sweep_reclaim_window_expired", 0) + 1
            sweep_index = None
        if swept_now:
            # Count sweep EPISODES: any later bar that wicks back through the
            # entry edge while the window is open restarts the deadline (a
            # deeper sweep is the canonical case) without re-counting the
            # episode; sweep_reclaim_reset_by_deeper_sweep counts the restarts.
            extreme = candle.low if order.direction == "bull" else candle.high
            if sweep_index is None:
                self.stats["sweep_reclaim_entry_swept"] = self.stats.get(
                    "sweep_reclaim_entry_swept", 0) + 1
                order.meta["sweep_reclaim_sweep_price"] = extreme
            else:
                if bar_index > sweep_index:
                    self.stats["sweep_reclaim_reset_by_deeper_sweep"] += 1
                previous = order.meta.get("sweep_reclaim_sweep_price", extreme)
                order.meta["sweep_reclaim_sweep_price"] = (
                    min(previous, extreme) if order.direction == "bull"
                    else max(previous, extreme))
            sweep_index = bar_index
            order.meta["sweep_reclaim_sweep_index"] = bar_index
        if sweep_index is None:
            return False, 0
        reclaimed = (candle.close > order.entry if order.direction == "bull"
                     else candle.close < order.entry)
        if not reclaimed:
            return False, 0
        lag = bar_index - sweep_index
        key = ("sweep_reclaim_lag0_confirms" if lag == 0 else
               "sweep_reclaim_lag1_confirms" if lag == 1 else
               "sweep_reclaim_lag2plus_confirms")
        self.stats[key] = self.stats.get(key, 0) + 1
        order.meta.update({"sweep_index": sweep_index,
                           "reclaim_index": bar_index,
                           "sweep_price": order.meta.get("sweep_reclaim_sweep_price"),
                           "sweep_reclaim_max_bars": window})
        order.meta.pop("sweep_reclaim_sweep_index", None)
        order.meta.pop("sweep_reclaim_sweep_price", None)
        return True, lag

    def _try_sweep_reclaim(self, order: PendingOrder, candle: Candle,
                           bar_index: int, spread: float,
                           execution_source: str) -> bool:
        """Enter when a closed bar sweeps and then reclaims the OB entry edge.

        Same window rules as process_signal_candle (see
        _sweep_reclaim_window). OHLC is Bid data. A long therefore enters at
        close + spread while a short enters at close. The target remains
        fixed-RR so this experiment changes entry confirmation only.
        """
        if order.meta.get("sweep_reclaim_confirmed"):
            # Only experimental revival/re-entry orders created with
            # require_fresh_sweep=False arrive here pre-confirmed (the legacy
            # OHLC path never sets this meta); their stop was already
            # ATR-buffered at creation. Fill at this bar's close, like the
            # tick path fills at the next tick.
            fill = candle.close + spread if order.direction == "bull" else candle.close
            stop = order.stop
            distance = abs(fill - stop)
            if distance <= 0:
                return False
            target = (fill + self.cfg.rr * distance if order.direction == "bull"
                      else fill - self.cfg.rr * distance)
            opened = self._open(order, fill, candle.time, bar_index, execution_source,
                                spread, stop=stop, target=target) is not None
            if opened:
                self.stats["sweep_reclaim_filled"] = self.stats.get(
                    "sweep_reclaim_filled", 0) + 1
            return opened
        self.stats["sweep_reclaim_checked"] = self.stats.get("sweep_reclaim_checked", 0) + 1
        confirmed, lag = self._sweep_reclaim_window(order, candle, bar_index)
        if not confirmed:
            return False
        fill = candle.close + spread if order.direction == "bull" else candle.close
        self.stats["sweep_reclaim_confirmed"] = self.stats.get(
            "sweep_reclaim_confirmed", 0) + 1
        atr = order.meta.get("atr_at_formation") or 0.0
        buffer = atr * self.cfg.sweep_reclaim_atr_buffer
        stop = order.stop - buffer if order.direction == "bull" else order.stop + buffer
        distance = abs(fill - stop)
        if distance <= 0:
            return False
        target = (fill + self.cfg.rr * distance if order.direction == "bull"
                  else fill - self.cfg.rr * distance)
        order.meta.update({"entry_mode": "sweep_reclaim",
                           "sweep_reclaim_atr_buffer": self.cfg.sweep_reclaim_atr_buffer,
                           "sweep_reclaim_lag_bars": lag,
                           "reclaim_close": candle.close})
        opened = self._open(order, fill, candle.time, bar_index, execution_source,
                            spread, stop=stop, target=target) is not None
        if opened:
            self.stats["sweep_reclaim_filled"] = self.stats.get(
                "sweep_reclaim_filled", 0) + 1
        return opened

    def _active_pending(self) -> list[PendingOrder]:
        return [x for x in self.pending if x.active]

    def _has_active_pending_for_ob(self, ob_id: str) -> bool:
        return any(x.active and x.ob_id == ob_id for x in self.pending)

    def _count_max_positions_block(self, order: PendingOrder) -> None:
        """Telemetry when an otherwise fill-eligible order finds no slot.

        Never mutates execution state: the order stays active and retries on
        a later tick/candle exactly as it always did.
        """
        if order.lifecycle_state != "armed":
            return
        if (self.cfg.entry_mode == "sweep_reclaim" and
                not order.meta.get("sweep_reclaim_confirmed")):
            return
        self.stats["rejected_max_positions"] += 1
        if not order.meta.get("max_positions_blocked_counted"):
            order.meta["max_positions_blocked_counted"] = True
            self.stats["setups_blocked_max_positions"] += 1

    # ------------------------------------------------------------------
    # Experimental per-closed-bar driver (flag-gated).
    # ------------------------------------------------------------------
    def _experimental_on_candle(self, candle: Candle, bar_index: int) -> None:
        """Deterministic per-bar evaluation of the flag-gated experiments.

        Fixed, documented order: Controlled Revival first, then Controlled
        Re-entry. When one OB qualifies for both on the same bar only the
        revival order is created (Revival > Re-entry): revival better
        preserves the original setup's context, and the one-active-pending-
        per-OB guard plus the superseded_by_revival candidate state make the
        choice explicit and idempotent -- a record/candidate can only ever
        transition out of its trigger state once, so no bar or tick can
        create duplicate orders.

        Trend alignment here reads current_m5_trend as of the PREVIOUS closed
        bar (this hook runs before update_m5_trend for the current bar in all
        three execution paths), matching how real pending orders and the
        shadow audit experience the trend gate: one bar of confirmation
        conservatism, identical everywhere.
        """
        if self.cfg.entry_mode != "sweep_reclaim":
            return
        if self.cfg.controlled_revival:
            self._controlled_revival_on_candle(candle, bar_index)
        if self.cfg.allow_ob_reentry:
            self._reentry_on_candle(candle, bar_index)

    def _controlled_revival_on_candle(self, candle: Candle, bar_index: int) -> None:
        for rec in self.revival_records:
            if rec["state"] != "suspended":
                continue
            if rec["ob_id"] in self.invalidated_obs:
                rec["state"] = "rejected_ob_invalid"
                self.stats["revival_rejected_ob_invalid"] += 1
                continue
            lag = bar_index - rec["cancel_index"]
            want = "bullish" if rec["direction"] == "bull" else "bearish"
            if self.current_m5_trend.value != want:
                if lag > self.cfg.revival_max_return_bars:
                    rec["state"] = "expired_no_return"
                    self.stats["revival_rejected_too_late"] += 1
                continue
            if lag > self.cfg.revival_max_return_bars:
                rec["state"] = "rejected_too_late"
                self.stats["revival_rejected_too_late"] += 1
                continue
            self.stats["revival_trend_returned"] += 1
            for bound, key in ((3, "revival_returned_within_3"),
                               (6, "revival_returned_within_6"),
                               (12, "revival_returned_within_12")):
                if lag <= bound:
                    self.stats[key] += 1
            attempts = self.revival_attempts.get(rec["ob_id"], 0)
            if attempts >= self.cfg.revival_max_per_ob:
                rec["state"] = "rejected_attempt_limit"
                self.stats["revival_rejected_attempt_limit"] += 1
                continue
            if self._has_active_pending_for_ob(rec["ob_id"]):
                rec["state"] = "superseded_active_order"
                continue
            reason = ("trend_returned_fresh_sweep_required"
                      if self.cfg.revival_require_fresh_sweep
                      else "trend_returned_no_fresh_sweep_required")
            order = self._create_experimental_order(rec, candle, bar_index, "revival", {
                "is_revival": True,
                "revival_parent_order_id": rec["parent_order_id"],
                "revival_parent_ob_id": rec["ob_id"],
                "revival_attempt": attempts + 1,
                "revival_suspended_index": rec["cancel_index"],
                "revival_trend_return_index": bar_index,
                "revival_return_lag_bars": lag,
                "revival_reason": reason,
            })
            self.revival_attempts[rec["ob_id"]] = attempts + 1
            rec.update({"state": "order_created", "revival_order_id": order.id,
                        "return_index": bar_index})
            self.stats["revival_orders_created"] += 1

    def _reentry_on_candle(self, candle: Candle, bar_index: int) -> None:
        for ob_id, hist in self.ob_entry_history.items():
            if hist.get("candidate_state") != "waiting":
                continue
            if ob_id in self.invalidated_obs:
                hist["candidate_state"] = "rejected_ob_invalid"
                self.stats["reentry_rejected_ob_invalid"] += 1
                continue
            if self._has_active_pending_for_ob(ob_id):
                # Revival > Re-entry: a revival order just created on this OB
                # permanently supersedes the re-entry candidate.
                if any(rec.get("revival_order_id") and rec["ob_id"] == ob_id and
                       rec["state"] == "order_created" for rec in self.revival_records):
                    hist["candidate_state"] = "superseded_by_revival"
                continue
            if (self.cfg.controlled_revival and
                    any(rec["ob_id"] == ob_id and rec["state"] == "suspended"
                        for rec in self.revival_records)):
                # A pending revival decision exists -- defer, don't compete.
                continue
            wait = bar_index - hist["last_exit_index"]
            if wait < self.cfg.min_reentry_wait_bars:
                if not hist.get("wait_blocked_counted"):
                    hist["wait_blocked_counted"] = True
                    self.stats["reentry_rejected_wait"] += 1
                continue
            want = "bullish" if hist["direction"] == "bull" else "bearish"
            if self.current_m5_trend.value != want:
                hist["candidate_state"] = "rejected_trend"
                self.stats["reentry_rejected_trend"] += 1
                continue
            attempts = hist.get("attempts", 0)
            if attempts >= self.cfg.max_entries_per_ob:
                hist["candidate_state"] = "rejected_attempt_limit"
                self.stats["reentry_rejected_attempt_limit"] += 1
                continue
            order = self._create_experimental_order(hist, candle, bar_index, "reentry", {
                "is_reentry": True,
                "reentry_parent_trade_id": hist.get("last_trade_id"),
                "reentry_wait_bars": wait,
                "previous_entry_result_r": hist.get("last_result_r"),
            })
            hist.update({"candidate_state": "order_created",
                         "reentry_order_id": order.id})
            self.stats["reentry_orders_created"] += 1

    def _register_reentry_candidate(self, trade: Trade, bar_index: int | None) -> None:
        """Called from _close: books the exit and, when every static gate
        passes, opens a re-entry candidacy that _reentry_on_candle evaluates
        bar by bar. Weak CHoCH never becomes a candidate."""
        ob_id = trade.ob_id
        if not ob_id:
            return
        hist = self.ob_entry_history.setdefault(ob_id, {"attempts": 0})
        hist["attempts"] = max(hist.get("attempts", 0),
                               int(trade.meta.get("ob_entry_attempt") or 1))
        exit_index = bar_index if bar_index is not None else self.market_index
        hist.update({
            "direction": trade.direction,
            "entry": trade.meta.get("ob_entry_price"),
            "base_stop": trade.meta.get("ob_stop_price"),
            "atr": trade.meta.get("atr_at_formation") or 0.0,
            "break_kind": trade.meta.get("break_kind"),
            "strong_choch": bool(trade.meta.get("bos_displacement_top_quartile")),
            "ob_id": ob_id,
            "last_exit_index": exit_index,
            "last_exit_time": trade.closed_time,
            "last_result_r": trade.r_multiple,
            "last_trade_id": trade.id,
        })
        hist.pop("wait_blocked_counted", None)
        if exit_index is None or hist["entry"] is None or hist["base_stop"] is None:
            return  # restored pre-feature trade without OB edges: safely skip
        if ob_id in self.invalidated_obs:
            return
        if not self._revival_eligible_break(trade.meta):
            return
        if self.cfg.reentry_after_loss_only and trade.result == "win":
            return
        if hist["attempts"] >= self.cfg.max_entries_per_ob:
            self.stats["reentry_rejected_attempt_limit"] += 1
            return
        hist["candidate_state"] = "waiting"
        self.stats["reentry_candidates"] += 1

    def _create_experimental_order(self, base: dict, candle: Candle, bar_index: int,
                                   kind: str, extra_meta: dict) -> PendingOrder:
        """Build the new PendingOrder a revival/re-entry produces.

        Never restores the old order: new uuid, fresh created_index, and --
        unless require_fresh_sweep was explicitly disabled -- no sweep state,
        so the normal _sweep_reclaim_window machinery must confirm a
        completely fresh sweep+reclaim (which cannot happen before
        created_index + max(1, min_entry_wait_bars): the first entry's sweep
        is structurally unusable). Stop/fill are recomputed from that fresh
        structure by the standard confirm/fill code.
        """
        direction = base["direction"]
        entry = float(base["entry"])
        base_stop = float(base["base_stop"])
        atr = float(base.get("atr") or 0.0)
        width = abs(entry - base_stop)
        require_fresh = (self.cfg.revival_require_fresh_sweep if kind == "revival"
                         else self.cfg.reentry_require_fresh_sweep)
        stop = base_stop
        meta = {
            "break_kind": base.get("break_kind"),
            "atr_at_formation": atr or None,
            "ob_entry_price": entry,
            "ob_stop_price": base_stop,
            "ob_width": width,
            "ob_width_atr": width / atr if atr else None,
            "bos_displacement_top_quartile": base.get("strong_choch"),
            "trend_at_creation": self.current_m5_trend.value,
            "m5_trend_at_setup": self.current_m5_trend.value,
            "trend_swing_length": self.cfg.trend_swing_length,
            "entry_pivot_left": self.cfg.entry_pivot_left,
            "entry_pivot_right": self.cfg.entry_pivot_right,
            "setup_model": kind,
            "is_revival": False,
            "is_reentry": False,
            **extra_meta,
        }
        if not require_fresh:
            buffer = atr * self.cfg.sweep_reclaim_atr_buffer
            stop = base_stop - buffer if direction == "bull" else base_stop + buffer
            meta.update({"sweep_reclaim_confirmed": True,
                         "entry_mode": "sweep_reclaim",
                         "sweep_reclaim_atr_buffer": self.cfg.sweep_reclaim_atr_buffer,
                         "reclaim_close": candle.close,
                         f"{kind}_no_fresh_sweep": True})
        distance = abs(entry - stop)
        target = (entry + self.cfg.rr * distance if direction == "bull"
                  else entry - self.cfg.rr * distance)
        order = PendingOrder(uuid4().hex, base["ob_id"], direction, entry, stop,
                             target, bar_index, candle.time, True, meta, "armed")
        if self.cfg.allow_ob_reentry:
            hist = self.ob_entry_history.setdefault(base["ob_id"], {"attempts": 0})
            hist["attempts"] = hist.get("attempts", 0) + 1
            meta["ob_entry_attempt"] = hist["attempts"]
        self.pending.append(order)
        self.pending.sort(key=lambda x: (x.created_index, x.created_time))
        return order

    def _note_experimental_cancel(self, order: PendingOrder, cause: str) -> None:
        """Bookkeeping when an ACTIVE order dies from a trend change
        (cause='trend') or OB invalidation (cause='ob_invalid'). Pure
        telemetry/state-machine updates; the caller performs the actual
        cancellation exactly as before."""
        meta = order.meta
        if meta.get("spread_wait_active"):
            key = ("spread_wait_cancelled_trend" if cause == "trend"
                   else "spread_wait_cancelled_ob_invalid")
            self.stats[key] += 1
            meta["spread_wait_active"] = False
        if meta.get("is_revival"):
            self.stats["revival_orders_cancelled"] += 1
            if not meta.get("sweep_reclaim_confirmed"):
                self.stats["revival_rejected_no_fresh_sweep"] += 1
            for rec in self.revival_records:
                if rec.get("revival_order_id") == order.id and rec["state"] == "order_created":
                    rec["state"] = f"order_cancelled_{cause}"
        if meta.get("is_reentry"):
            if cause == "trend":
                self.stats["reentry_rejected_trend"] += 1
            if not meta.get("sweep_reclaim_confirmed"):
                self.stats["reentry_rejected_no_fresh_sweep"] += 1
            hist = self.ob_entry_history.get(order.ob_id)
            if hist is not None and hist.get("reentry_order_id") == order.id:
                hist["candidate_state"] = f"order_cancelled_{cause}"

    # ------------------------------------------------------------------
    # Spread Wait (flag-gated; tick execution paths only).
    # ------------------------------------------------------------------
    def _spread_wait_on_block(self, order: PendingOrder, tick: Tick, spread: float) -> None:
        """A confirmed order was spread-blocked. Legacy (flag off): implicit
        unbounded retry, untouched. Flag on: explicit waiting_spread state
        with deterministic expiry on any configured limit."""
        if not self.cfg.wait_for_spread_after_confirmation:
            return
        meta = order.meta
        if not meta.get("spread_wait_active"):
            meta.update({"spread_wait_active": True,
                         "lifecycle_note": "waiting_spread",
                         "spread_wait_started_time": tick.time,
                         "spread_wait_started_index": self.market_index,
                         "spread_wait_ticks": 0,
                         "spread_at_confirmation": spread})
            self.stats["spread_wait_started"] += 1
            self.stats["unique_orders_blocked_by_spread"] += 1
            self.stats["spread_at_confirmation_sum"] = round(
                self.stats.get("spread_at_confirmation_sum", 0.0) + spread, 10)
        meta["spread_wait_ticks"] = int(meta.get("spread_wait_ticks", 0)) + 1
        seconds = self._elapsed_seconds(meta.get("spread_wait_started_time"), tick.time)
        expired = (self.cfg.spread_wait_max_ticks is not None and
                   meta["spread_wait_ticks"] > self.cfg.spread_wait_max_ticks)
        if (not expired and self.cfg.spread_wait_max_seconds is not None and
                seconds is not None and seconds > self.cfg.spread_wait_max_seconds):
            expired = True
        if (not expired and self.cfg.spread_wait_max_bars is not None and
                self.market_index is not None and
                meta.get("spread_wait_started_index") is not None and
                self.market_index - meta["spread_wait_started_index"] >
                self.cfg.spread_wait_max_bars):
            expired = True
        if expired:
            order.active = False
            order.lifecycle_state = "spread_wait_expired"
            meta.update({"spread_wait_active": False, "spread_waited": True,
                         "spread_wait_seconds": seconds})
            self.stats["spread_wait_expired"] += 1
            self._spread_wait_record_duration(meta, seconds)

    def _spread_wait_finalize(self, position: PaperPosition, tick: Tick,
                              spread: float) -> None:
        """Stamp fill-side wait telemetry on the just-opened position."""
        if not self.cfg.wait_for_spread_after_confirmation:
            return
        meta = position.meta
        if meta.get("spread_wait_active"):
            seconds = self._elapsed_seconds(meta.get("spread_wait_started_time"), tick.time)
            meta.update({"spread_wait_active": False, "spread_waited": True,
                         "spread_wait_seconds": seconds, "spread_at_fill": spread})
            self.stats["spread_wait_eventually_filled"] += 1
            self.stats["spread_at_fill_sum"] = round(
                self.stats.get("spread_at_fill_sum", 0.0) + spread, 10)
            self._spread_wait_record_duration(meta, seconds)
        else:
            meta.setdefault("spread_waited", False)

    def _spread_wait_after_reject(self, order: PendingOrder) -> None:
        if (self.cfg.wait_for_spread_after_confirmation and
                order.meta.get("spread_wait_active") and
                order.lifecycle_state == "rejected_sizing"):
            self.stats["spread_wait_rejected_sizing"] += 1
            order.meta["spread_wait_active"] = False

    def _spread_wait_record_duration(self, meta: dict, seconds: float | None) -> None:
        ticks = int(meta.get("spread_wait_ticks", 0))
        self.stats["spread_wait_duration_ticks_sum"] += ticks
        if seconds is not None:
            self.stats["spread_wait_duration_seconds_sum"] = round(
                self.stats.get("spread_wait_duration_seconds_sum", 0.0) + seconds, 6)
            self.stats["spread_wait_max_duration_seconds"] = max(
                self.stats.get("spread_wait_max_duration_seconds", 0.0),
                round(seconds, 6))

    @staticmethod
    def _elapsed_seconds(start: str | None, end: str | None) -> float | None:
        if not start or not end:
            return None
        try:
            return max(0.0, (datetime.fromisoformat(end) -
                             datetime.fromisoformat(start)).total_seconds())
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Experimental persistence helpers.
    # ------------------------------------------------------------------
    def _experimental_fingerprint(self) -> dict:
        """The config surface the persisted experimental state depends on.
        Stored in every snapshot; a mismatch on restore triggers the safe
        cancellation path in _cancel_experimental_state."""
        cfg = self.cfg
        return {"entry_mode": cfg.entry_mode,
                "sweep_reclaim_max_bars": cfg.sweep_reclaim_max_bars,
                "controlled_revival": cfg.controlled_revival,
                "revival_max_return_bars": cfg.revival_max_return_bars,
                "revival_require_fresh_sweep": cfg.revival_require_fresh_sweep,
                "revival_max_per_ob": cfg.revival_max_per_ob,
                "allow_ob_reentry": cfg.allow_ob_reentry,
                "max_entries_per_ob": cfg.max_entries_per_ob,
                "min_reentry_wait_bars": cfg.min_reentry_wait_bars,
                "reentry_require_fresh_sweep": cfg.reentry_require_fresh_sweep,
                "reentry_after_loss_only": cfg.reentry_after_loss_only,
                "wait_for_spread_after_confirmation": cfg.wait_for_spread_after_confirmation,
                "spread_wait_max_seconds": cfg.spread_wait_max_seconds,
                "spread_wait_max_ticks": cfg.spread_wait_max_ticks,
                "spread_wait_max_bars": cfg.spread_wait_max_bars,
                "portfolio_risk_cap": cfg.portfolio_risk_cap}

    def _cancel_experimental_state(self, stored: dict, current: dict) -> None:
        """Safe behaviour on config/state mismatch across a restart: cancel
        every in-flight experimental artifact, KEEP attempt counters (so a
        restart can never grant extra revivals/re-entries), and queue one
        loggable event explaining why. Old orders are never re-validated or
        re-activated here."""
        cancelled = 0
        for order in self.pending:
            if order.active and (order.meta.get("is_revival") or
                                 order.meta.get("is_reentry")):
                order.active = False
                order.lifecycle_state = "cancelled_config_change"
                cancelled += 1
            order.meta.pop("spread_wait_active", None)
        for rec in self.revival_records:
            if rec["state"] == "suspended":
                rec["state"] = "cancelled_config_change"
        for hist in self.ob_entry_history.values():
            if hist.get("candidate_state") == "waiting":
                hist["candidate_state"] = "cancelled_config_change"
        self.trend_events.append({
            "event_type": "experimental_state_cancelled",
            "reason": "experimental config changed across restart; in-flight "
                      "experimental state cancelled (attempt counters kept)",
            "stored_config": stored, "current_config": current,
            "orders_cancelled": cancelled,
        })

    def _open(self, order: PendingOrder, fill: float, when: str,
              bar_index: int | None, execution_source: str,
              execution_spread: float, stop: float | None = None,
              target: float | None = None) -> PaperPosition | None:
        stop = order.stop if stop is None else stop
        target = order.target if target is None else target
        fill_context = self.market_context.get(order.direction, {})
        context_score = None
        effective_index = bar_index if bar_index is not None else self.market_index
        wait_bars = (effective_index - order.created_index
                     if effective_index is not None else None)
        age_history = self.entry_age_values[-self.cfg.entry_age_rank_window:]
        age_percentile = None
        if wait_bars is not None and len(age_history) >= self.cfg.entry_age_rank_min_samples:
            age_percentile = sum(value <= wait_bars for value in age_history) / len(age_history)
        age_top_quartile = age_percentile is not None and age_percentile >= .75
        if wait_bars is not None:
            self.entry_age_values.append(wait_bars)
            self.entry_age_values = self.entry_age_values[-self.cfg.entry_age_rank_window:]
        target_source = "fixed_rr"
        if self.cfg.target_mode == "m5_liquidity_min_rr":
            key = ("liq_m5_nearest_bsl_level" if order.direction == "bull"
                   else "liq_m5_nearest_ssl_level")
            candidate = fill_context.get(key)
            distance = abs(fill - stop)
            reward = ((candidate - fill) if order.direction == "bull" else (fill - candidate)) \
                if candidate is not None else 0.0
            if distance > 0 and reward / distance >= self.cfg.rr:
                target = candidate
                target_source = "m5_liquidity"
        risk_fraction = self.cfg.risk_fraction
        if self.cfg.liquidity_risk_sizing_enabled:
            risk_fraction = (self.cfg.liquidity_risk_fraction
                             if order.meta.get("setup_m5_opposite_sweep")
                             else self.cfg.base_risk_fraction)
        elif self.cfg.choch_risk_sizing_enabled:
            risk_fraction = (self.cfg.choch_opposite_risk_fraction
                             if fill_context.get("m5_last_choch_alignment") == "opposite"
                             else self.cfg.choch_base_risk_fraction)
        elif self.cfg.combined_context_risk_sizing_enabled:
            context_score = int(bool(order.meta.get("setup_m5_opposite_sweep")))
            context_score += int(fill_context.get("m5_last_choch_alignment") == "opposite")
            risk_fraction = (
                self.cfg.combined_two_signal_risk_fraction if context_score == 2 else
                self.cfg.combined_one_signal_risk_fraction if context_score == 1 else
                self.cfg.combined_zero_signal_risk_fraction
            )
        elif self.cfg.displacement_risk_sizing_enabled:
            rank = order.meta.get("bos_close_through_percentile")
            risk_fraction = (self.cfg.displacement_top_risk_fraction
                             if rank is not None and rank >= .75
                             else self.cfg.displacement_base_risk_fraction)
        elif self.cfg.choch_displacement_risk_sizing_enabled:
            context_score = int(fill_context.get("m5_last_choch_alignment") == "opposite")
            context_score += int(bool(order.meta.get("bos_displacement_top_quartile")))
            risk_fraction = (
                self.cfg.combined_two_signal_risk_fraction if context_score == 2 else
                self.cfg.combined_one_signal_risk_fraction if context_score == 1 else
                self.cfg.combined_zero_signal_risk_fraction
            )
        elif self.cfg.three_factor_risk_sizing_enabled:
            context_score = int(fill_context.get("m5_last_choch_alignment") == "opposite")
            context_score += int(bool(order.meta.get("bos_displacement_top_quartile")))
            context_score += int(age_top_quartile)
            risk_fraction = (.0075 if context_score == 0 else .009 if context_score == 1
                             else .011 if context_score == 2 else .0125)
        break_kind = order.meta.get("break_kind")
        if break_kind == "BOS" and self.cfg.bos_three_factor_risk_enabled:
            context_score = int(fill_context.get("m5_last_choch_alignment") == "opposite")
            context_score += int(bool(order.meta.get("bos_displacement_top_quartile")))
            context_score += int(age_top_quartile)
            risk_fraction = (.0075 if context_score == 0 else .009 if context_score == 1
                             else .011 if context_score == 2 else .0125)
        if break_kind == "CHoCH" and self.cfg.choch_split_risk_enabled:
            toxic = (age_top_quartile and
                     bool(order.meta.get("bos_displacement_top_quartile")))
            risk_fraction = (self.cfg.choch_toxic_risk_fraction if toxic else
                             self.cfg.choch_sweep_risk_fraction
                             if order.meta.get("setup_m5_opposite_sweep") else
                             self.cfg.choch_other_risk_fraction)
        if (break_kind == "BOS" and self.cfg.bos_m15_counter_bonus_fraction and
                fill_context.get("m15_alignment") == "counter"):
            risk_fraction = min(.0125, risk_fraction +
                                self.cfg.bos_m15_counter_bonus_fraction)
        if (order.meta.get("break_kind") == "CHoCH" and
                self.cfg.choch_risk_cap_fraction is not None):
            risk_fraction = min(risk_fraction, self.cfg.choch_risk_cap_fraction)
        distance = abs(fill - stop)
        sizing = calculate_safe_position_size(
            equity=self.equity,
            risk_percent=risk_fraction,
            entry_price=fill,
            stop_loss=stop,
            tick_size=self.spec.tick_size,
            tick_value=self.spec.tick_value,
            volume_min=self.spec.volume_min,
            volume_max=self.spec.volume_max,
            volume_step=self.spec.volume_step,
        )
        if not sizing.is_valid:
            # Never bump an undersized lot up to volume_min -- that would
            # silently multiply realized risk past what was configured.
            # Reject the setup instead: no paper fill, no pending order left
            # to retry, no Demo mirror will ever see it.
            order.active = False
            order.lifecycle_state = "rejected_sizing"
            order.meta.update({"rejection_reason": sizing.rejection_reason,
                              "allowed_risk": sizing.allowed_risk,
                              "attempted_volume": sizing.volume,
                              "attempted_actual_risk": sizing.actual_risk,
                              "applied_risk_fraction": risk_fraction})
            counter = ("rejected_volume_below_minimum"
                      if sizing.rejection_reason == CALCULATED_VOLUME_BELOW_BROKER_MINIMUM
                      else "rejected_risk_sizing")
            self.stats[counter] = self.stats.get(counter, 0) + 1
            self.rejected_sizing.append({
                "order_id": order.id, "ob_id": order.ob_id, "direction": order.direction,
                "entry_price": fill, "stop_loss": stop, "equity": self.equity,
                "risk_percent": risk_fraction, "allowed_risk": sizing.allowed_risk,
                "tick_size": self.spec.tick_size, "tick_value": self.spec.tick_value,
                "volume_min": self.spec.volume_min, "volume_max": self.spec.volume_max,
                "volume_step": self.spec.volume_step,
                "attempted_volume": sizing.volume, "attempted_actual_risk": sizing.actual_risk,
                "rejection_reason": sizing.rejection_reason, "time": when,
            })
            return None
        volume, actual_risk = sizing.volume, sizing.actual_risk

        # Portfolio risk cap (flag-gated). Runs right after sizing so the
        # projection uses the REAL sized risk of this fill, and open risk is
        # each open position's entry-to-stop risk_money booked at its own
        # entry -- floating PnL never enters the calculation. Reject-only in
        # v1: the volume is never silently reduced and a rejected order is
        # deactivated, not retried later.
        if self.cfg.portfolio_risk_cap is not None:
            open_risk_money = sum(item.risk_money for item in self.positions)
            equity = self.equity if self.equity > 0 else 0.0
            open_fraction = open_risk_money / equity if equity else float("inf")
            new_fraction = actual_risk / equity if equity else float("inf")
            projected = open_fraction + new_fraction
            order.meta.update({"open_risk_before_entry": open_fraction,
                               "new_trade_risk_fraction": new_fraction,
                               "projected_open_risk_fraction": projected,
                               "portfolio_risk_cap": self.cfg.portfolio_risk_cap})
            if projected > self.cfg.portfolio_risk_cap + 1e-12:
                order.active = False
                order.lifecycle_state = "rejected_portfolio_risk"
                order.meta["rejection_reason"] = PORTFOLIO_RISK_CAP_EXCEEDED
                self.stats["rejected_portfolio_risk_cap"] += 1
                self.stats["setups_blocked_portfolio_risk"] += 1
                self.rejected_sizing.append({
                    "order_id": order.id, "ob_id": order.ob_id,
                    "direction": order.direction, "entry_price": fill,
                    "stop_loss": stop, "equity": self.equity,
                    "risk_percent": risk_fraction,
                    "open_risk_before_entry": open_fraction,
                    "new_trade_risk_fraction": new_fraction,
                    "projected_open_risk_fraction": projected,
                    "portfolio_risk_cap": self.cfg.portfolio_risk_cap,
                    "rejection_reason": PORTFOLIO_RISK_CAP_EXCEEDED, "time": when,
                })
                return None
            self.stats["max_observed_open_risk_fraction"] = max(
                self.stats.get("max_observed_open_risk_fraction", 0.0), projected)

        # Final, LIVE M5-trend re-check -- a pending order can go stale
        # between creation and fill (formed under one trend, touched only
        # after the trend already flipped). This uses self.current_m5_trend
        # as of right now, never the trend captured at order creation, and
        # runs on every fill path since they all funnel through _open().
        order.meta["trend_at_fill_check"] = self.current_m5_trend.value
        order.meta["m5_trend_at_fill"] = self.current_m5_trend.value
        fill_trend_check = validate_trade_direction(order.direction, self.current_m5_trend)
        if not fill_trend_check.is_allowed:
            order.active = False
            order.lifecycle_state = "rejected_trend"
            order.meta.update({"rejection_reason": fill_trend_check.rejection_reason})
            counter = ("rejected_trend_neutral"
                      if fill_trend_check.rejection_reason == TRADE_REJECTED_M5_TREND_NEUTRAL
                      else "rejected_trend_mismatch")
            self.stats[counter] = self.stats.get(counter, 0) + 1
            self.trend_events.append({
                "event_type": "order_rejected_m5_trend", "order_id": order.id,
                "ob_id": order.ob_id, "side": order.direction,
                "current_m5_trend": self.current_m5_trend.value,
                "trend_at_creation": order.meta.get("trend_at_creation"),
                "reason": fill_trend_check.rejection_reason, "time": when,
                "signal_type": order.meta.get("break_kind"),
            })
            return None

        if order.meta.get("is_revival"):
            self.stats["revival_orders_filled"] += 1
            order.meta["revival_fresh_sweep_index"] = order.meta.get("sweep_index")
        elif order.meta.get("is_reentry"):
            self.stats["reentry_orders_filled"] += 1
            order.meta["reentry_fresh_sweep_index"] = order.meta.get("sweep_index")
        if self.cfg.controlled_revival or self.cfg.allow_ob_reentry:
            order.meta.setdefault(
                "setup_model",
                "revival" if order.meta.get("is_revival")
                else "reentry" if order.meta.get("is_reentry") else "normal")
        order.active = False
        order.lifecycle_state = "filled"
        position = PaperPosition(uuid4().hex, order.id, order.direction, fill,
                                 stop, target, volume, actual_risk, when,
                                 bar_index, 0.0, 0.0,
                                 {**order.meta, "ob_id": order.ob_id,
                                  "created_index": order.created_index,
                                  "wait_bars": wait_bars,
                                  "entry_age_percentile": age_percentile,
                                  "entry_age_top_quartile": age_top_quartile,
                                  "entry_execution_source": execution_source,
                                  "entry_execution_spread": execution_spread,
                                  "original_stop": stop,
                                  "execution_state": ("demo_confirmation_pending"
                                                      if self.cfg.demo_orders_enabled
                                                      else "paper_active"),
                                  "target_source": target_source,
                                  "planned_rr": abs(target - fill) / distance,
                                  "applied_risk_fraction": risk_fraction,
                                  "allowed_risk": sizing.allowed_risk,
                                  "choch_risk_cap_fraction": self.cfg.choch_risk_cap_fraction,
                                  "context_risk_score": context_score
                                  if (self.cfg.combined_context_risk_sizing_enabled or
                                      self.cfg.choch_displacement_risk_sizing_enabled or
                                      self.cfg.three_factor_risk_sizing_enabled) else None,
                                  **{f"fill_{k}": v for k, v in
                                     fill_context.items()}})
        self.positions.append(position)
        return position

    def _advance_lifecycle(self, candle: Candle, bar_index: int) -> None:
        if not self.cfg.entry_lifecycle_enabled:
            return
        for order in self._active_pending():
            meta = order.meta
            last_close = meta.get("lifecycle_last_close", meta.get("formation_close"))
            if order.lifecycle_state == "formed":
                initial = meta.get("formation_high") if order.direction == "bull" else meta.get("formation_low")
                if initial is None:
                    # Legacy order: do not infer readiness from incomplete state.
                    continue
                extended = candle.high > initial if order.direction == "bull" else candle.low < initial
                if extended:
                    order.lifecycle_state = "accepted"
                    order.accepted_index = bar_index
                    meta["post_bos_extreme"] = candle.high if order.direction == "bull" else candle.low
                    self.stats["lifecycle_accepted"] += 1
            elif order.lifecycle_state == "accepted":
                extreme = meta.get("post_bos_extreme")
                if order.direction == "bull" and (extreme is None or candle.high > extreme):
                    meta["post_bos_extreme"] = candle.high
                elif order.direction == "bear" and (extreme is None or candle.low < extreme):
                    meta["post_bos_extreme"] = candle.low
                retracing = (last_close is not None and
                             (candle.close < last_close if order.direction == "bull"
                              else candle.close > last_close))
                if retracing and order.accepted_index is not None and bar_index > order.accepted_index:
                    order.lifecycle_state = "armed"
                    order.armed_index = bar_index
                    meta["lifecycle_bars_to_accept"] = order.accepted_index - order.created_index
                    meta["lifecycle_bars_to_arm"] = bar_index - order.created_index
                    self.stats["lifecycle_armed"] += 1
            meta["lifecycle_last_close"] = candle.close

    def _apply_breakeven(self, p: PaperPosition, when: str) -> None:
        """Move the stop to entry exactly once when real MFE reaches the
        configured R trigger (fraction of the initial entry-to-stop distance).

        The stop never moves backward and no offset is applied. Living inside
        the shared broker -- called from every excursion-tracking site in
        process_tick and process_candle -- is what keeps Paper live, Demo
        mirroring and both backtest runners on one identical implementation.
        """
        trigger = self.cfg.breakeven_trigger_r
        if trigger is None or p.meta.get("breakeven_armed"):
            return
        original_stop = float(p.meta.get("original_stop", p.stop))
        distance = abs(p.entry - original_stop)
        if distance <= 0 or p.max_favorable < trigger * distance:
            return
        improves = p.entry > p.stop if p.direction == "bull" else p.entry < p.stop
        p.meta.update({"breakeven_armed": True,
                       "breakeven_armed_time": when,
                       "breakeven_trigger_r": trigger})
        if improves:
            p.stop = p.entry
        self.stats["breakeven_armed"] = self.stats.get("breakeven_armed", 0) + 1
        self.breakeven_events.append({
            "event_type": "breakeven_armed", "position_id": p.id,
            "order_id": p.order_id, "direction": p.direction,
            "entry": p.entry, "original_stop": original_stop,
            "new_stop": p.stop, "trigger_r": trigger,
            "max_favorable": p.max_favorable, "time": when,
        })

    def _track_excursion(self, p: PaperPosition, low: float, high: float) -> None:
        adverse = p.entry - low if p.direction == "bull" else high - p.entry
        favorable = high - p.entry if p.direction == "bull" else p.entry - low
        p.max_adverse = max(p.max_adverse, adverse)
        p.max_favorable = max(p.max_favorable, favorable)

    def _close(self, p: PaperPosition, price: float, when: str, result: str,
               bar_index: int | None = None, execution_source: str = "unknown",
               execution_spread: float = 0.0) -> Trade:
        move = price - p.entry if p.direction == "bull" else p.entry - price
        pnl = move / self.spec.tick_size * self.spec.tick_value * p.volume
        r = pnl / p.risk_money if p.risk_money else 0.0
        # MAE/MFE stay in units of the risk taken at entry -- after a
        # breakeven move, entry-to-current-stop would be zero.
        original_stop = float(p.meta.get("original_stop", p.stop))
        initial_distance = abs(p.entry - original_stop)
        breakeven_armed = bool(p.meta.get("breakeven_armed"))
        trade = Trade(p.id, p.direction, p.entry, p.stop, p.target, price, p.volume,
                      p.risk_money, pnl, r, p.opened_time, when,
                      "win" if pnl > 0 else "loss", p.order_id,
                      str(p.meta.get("ob_id", "")), p.opened_index, bar_index,
                      p.max_adverse / initial_distance if initial_distance else 0.0,
                      p.max_favorable / initial_distance if initial_distance else 0.0,
                      {**p.meta, "exit_execution_source": execution_source,
                       "exit_execution_spread": execution_spread,
                       "breakeven_armed": breakeven_armed,
                       "breakeven_armed_time": p.meta.get("breakeven_armed_time"),
                       "breakeven_exit": breakeven_armed and result == "loss",
                       "original_stop": original_stop,
                       "final_stop": p.stop,
                       "execution_state": ("demo_exit_confirmation_pending"
                                           if self.cfg.demo_orders_enabled
                                           else "paper_closed")})
        self.trades.append(trade)
        self.equity += pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        if self.peak_equity:
            self.max_drawdown = max(self.max_drawdown, (self.peak_equity - self.equity) / self.peak_equity)
        self.positions.remove(p)
        if trade.meta.get("is_revival"):
            self.stats["revival_wins" if trade.result == "win" else "revival_losses"] += 1
            self.stats["revival_total_r"] = round(
                self.stats.get("revival_total_r", 0.0) + trade.r_multiple, 6)
        elif trade.meta.get("is_reentry"):
            self.stats["reentry_wins" if trade.result == "win" else "reentry_losses"] += 1
            self.stats["reentry_total_r"] = round(
                self.stats.get("reentry_total_r", 0.0) + trade.r_multiple, 6)
        if self.cfg.allow_ob_reentry and self.cfg.entry_mode == "sweep_reclaim":
            self._register_reentry_candidate(trade, bar_index)
        return trade

    def dump_state(self, index_offset: int = 0) -> dict:
        pending = []
        for value in self.pending:
            if index_offset and not value.active:
                continue
            item = asdict(value)
            for key in ("created_index", "accepted_index", "armed_index"):
                if item.get(key) is not None:
                    item[key] -= index_offset
            if index_offset and item["meta"].get("spread_wait_started_index") is not None:
                # Keeps the bar-based spread-wait timeout correct after a
                # trimmed dump/load round trip.
                item["meta"]["spread_wait_started_index"] -= index_offset
            pending.append(item)
        positions = []
        for value in self.positions:
            item = asdict(value)
            if item.get("opened_index") is not None:
                item["opened_index"] -= index_offset
            positions.append(item)
        return {"initial_equity": self.initial_equity, "equity": self.equity,
                "peak_equity": self.peak_equity, "max_drawdown": self.max_drawdown,
                "spec": asdict(self.spec), "pending": pending, "positions": positions,
                "trades": [], "market_index": (self.market_index - index_offset
                                                  if self.market_index is not None else None),
                "market_context": self.market_context,
                "entry_age_values": self.entry_age_values,
                "recent_spreads": list(self.recent_spreads), "stats": self.stats,
                "current_m5_trend": self.current_m5_trend.value,
                "entry_pivot_gate_active": self.entry_pivot_gate_active,
                "last_pivot_high": _dump_entry_pivot(self.last_pivot_high, index_offset),
                "last_pivot_low": _dump_entry_pivot(self.last_pivot_low, index_offset),
                "revival_shadow": self.revival_shadow,
                "shadow_positions": self.shadow_positions,
                "revival_records": [_shift_indices(dict(rec), index_offset,
                                                   ("cancel_index", "return_index"))
                                    for rec in self.revival_records],
                "revival_attempts": dict(self.revival_attempts),
                "ob_entry_history": {ob_id: _shift_indices(dict(hist), index_offset,
                                                           ("last_exit_index",))
                                     for ob_id, hist in self.ob_entry_history.items()},
                "invalidated_obs": sorted(self.invalidated_obs),
                "experimental_config": self._experimental_fingerprint()}

    def load_state(self, data: dict) -> None:
        self.initial_equity = data["initial_equity"]
        self.equity, self.peak_equity = data["equity"], data["peak_equity"]
        self.max_drawdown = data.get("max_drawdown", 0.0)
        self.spec = SymbolSpec(**data["spec"])
        self.pending = [PendingOrder(**x) for x in data.get("pending", [])]
        raw_positions = data.get("positions")
        if raw_positions is None:
            raw_positions = [data["position"]] if data.get("position") else []
        self.positions = [PaperPosition(**x) for x in raw_positions]
        self.trades = [Trade(**x) for x in data.get("trades", [])]
        self.market_index = data.get("market_index")
        self.market_context = data.get("market_context", {})
        self.entry_age_values = [int(value) for value in data.get("entry_age_values", [])]
        self.recent_spreads = deque((float(value) for value in data.get("recent_spreads", [])),
                                    maxlen=self.cfg.spread_median_window)
        self.stats = data.get("stats", {"setups_seen": 0, "rejected_break_kind": 0,
                                        "rejected_weak_choch": 0,
                                        "early_touch_blocked": 0, "lifecycle_accepted": 0,
                                        "lifecycle_armed": 0})
        self.stats.setdefault("rejected_weak_choch", 0)
        self.stats.setdefault("spread_entry_blocked", 0)
        self.stats.setdefault("demo_entry_reverted", 0)
        self.stats.setdefault("rejected_volume_below_minimum", 0)
        self.stats.setdefault("rejected_risk_sizing", 0)
        self.stats.setdefault("rejected_trend_mismatch", 0)
        self.stats.setdefault("rejected_trend_neutral", 0)
        self.stats.setdefault("cancelled_trend_change", 0)
        self.stats.setdefault("cancelled_trend_neutral", 0)
        self.stats.setdefault("rejected_no_entry_pivot", 0)
        self.stats.setdefault("rejected_stale_entry_pivot", 0)
        self.stats.setdefault("accepted_fresh_entry_pivot", 0)
        self.stats.setdefault("entry_pivot_age_samples", 0)
        self.stats.setdefault("entry_pivot_age_sum_bars", 0)
        self.stats.setdefault("entry_pivot_age_max_bars", 0)
        self.stats.setdefault("breakeven_armed", 0)
        self.stats.setdefault("sweep_reclaim_checked", 0)
        self.stats.setdefault("sweep_reclaim_entry_swept", 0)
        self.stats.setdefault("sweep_reclaim_confirmed", 0)
        self.stats.setdefault("sweep_reclaim_invalidated", 0)
        self.stats.setdefault("sweep_reclaim_filled", 0)
        self.stats.setdefault("sweep_reclaim_window_expired", 0)
        self.stats.setdefault("sweep_reclaim_lag0_confirms", 0)
        self.stats.setdefault("sweep_reclaim_lag1_confirms", 0)
        self.stats.setdefault("sweep_reclaim_lag2plus_confirms", 0)
        self.stats.setdefault("trend_suspended", 0)
        self.stats.setdefault("trend_returned_3", 0)
        self.stats.setdefault("trend_returned_6", 0)
        self.stats.setdefault("trend_returned_12", 0)
        self.stats.setdefault("reactivation_ob_still_valid", 0)
        self.stats.setdefault("reactivation_fresh_sweep", 0)
        self.stats.setdefault("reactivation_confirmed", 0)
        self.stats.setdefault("reactivation_would_fill", 0)
        self.stats.setdefault("reactivation_wins", 0)
        self.stats.setdefault("reactivation_losses", 0)
        self.stats.setdefault("reactivation_hypothetical_r", 0.0)
        self.revival_shadow = [dict(x) for x in data.get("revival_shadow", [])]
        self.shadow_positions = [dict(x) for x in data.get("shadow_positions", [])]
        self.stats.setdefault("pivot_highs_confirmed", 0)
        self.stats.setdefault("pivot_lows_confirmed", 0)
        self.stats.setdefault("buy_setups_created", 0)
        self.stats.setdefault("sell_setups_created", 0)
        self.rejected_sizing = []
        self.trend_events = []
        self.pivot_events = []

        self.breakeven_events = []
        self.current_m5_trend = TrendDirection(data.get("current_m5_trend", TrendDirection.NEUTRAL.value))
        self.entry_pivot_gate_active = data.get("entry_pivot_gate_active", False)
        self.last_pivot_high = EntryPivot(**data["last_pivot_high"]) if data.get("last_pivot_high") else None
        self.last_pivot_low = EntryPivot(**data["last_pivot_low"]) if data.get("last_pivot_low") else None
        for key, default in _experimental_stats_defaults().items():
            self.stats.setdefault(key, default)
        self.revival_records = [dict(x) for x in data.get("revival_records", [])]
        self.revival_attempts = {str(key): int(value) for key, value
                                 in (data.get("revival_attempts") or {}).items()}
        self.ob_entry_history = {str(key): dict(value) for key, value
                                 in (data.get("ob_entry_history") or {}).items()}
        self.invalidated_obs = set(data.get("invalidated_obs", []))
        stored = data.get("experimental_config")
        current = self._experimental_fingerprint()
        if stored is not None and stored != current:
            self._cancel_experimental_state(stored, current)


def _shift_indices(item: dict, index_offset: int, keys: tuple[str, ...]) -> dict:
    """Rebase the given bar-index keys by index_offset (dump-time trim),
    mirroring what dump_state does for every other persisted index."""
    if index_offset:
        for key in keys:
            if item.get(key) is not None:
                item[key] -= index_offset
    return item


def _dump_entry_pivot(pivot: EntryPivot | None, index_offset: int) -> dict | None:
    """asdict() an EntryPivot with candle_index shifted by index_offset, so a
    trimmed dump/load round-trip keeps pivot ages causally correct (the same
    shift dump_state applies to every other persisted bar index)."""
    if pivot is None:
        return None
    item = asdict(pivot)
    item["candle_index"] -= index_offset
    return item


def _is_after(left: str, right: str) -> bool:
    try:
        return datetime.fromisoformat(left) > datetime.fromisoformat(right)
    except (TypeError, ValueError):
        return left > right
