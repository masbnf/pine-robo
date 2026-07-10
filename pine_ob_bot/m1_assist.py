"""M1 Entry Assist engine (experimental, flag-gated, tick paths only).

Role separation (hard contract): M5 remains the ONLY source of trend,
major swing, BOS/Strong-CHoCH, entry pivot, order blocks, setups and trade
direction. This engine may only refine ENTRY TIMING on an active, valid M5
setup. It never creates counter-trend state, independent order blocks,
weak-CHoCH entries, or setups of its own.

Causality: every registered setup stores first_eligible_m1_index -- the
index of the FIRST M1 bar that closes after the setup existed. The runner
closes the boundary M1 before the boundary M5 (see tick_historical), so an
M1 bar that closed inside the M5 candle which created the setup is
processed before the setup exists and can never be consumed by it; the
explicit index guard additionally rejects (and counts) any violation.

Modes:
  shadow   -- observe-only: hypothetical entries walked on M1 closes;
              zero mutation of real orders/positions/counters-of-record.
  sequence -- only disambiguates same-M5-bar sweep/reclaim ordering; can
              VETO a false M5 confirmation (reclaim seen before sweep) and
              creates nothing.
  entry    -- a valid CLOSED-M1 sweep+reclaim confirms the active M5 setup
              early by setting the standard sweep_reclaim_confirmed meta;
              the fill then flows through the untouched normal tick
              pipeline (spread, spread-wait, position limit, portfolio
              cap, sizing). If the confirmation expires unfilled, the
              order's stop and meta are restored and the M5 fallback path
              continues exactly as before. After a successful M1 fill the
              order is 'filled' -- the M5 path can never duplicate it.

State machine per setup (deterministic, idempotent):
  idle -> swept -> reclaimed -> confirmed_waiting_fill -> filled
  with terminal/side states: expired, cancelled_trend, cancelled_ob_invalid,
  cancelled_duplicate, cancelled_position_limit, cancelled_spread,
  cancelled_risk (fill-path rejections are reported by the normal pipeline;
  the engine mirrors them into the state for diagnostics).
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from .m1 import M1Candle
from .models import PendingOrder, Trade
from .trend_filter import TRADE_REJECTED_M5_TREND_NEUTRAL, validate_trade_direction

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime cycle
    from .paper import PaperBroker

M1_STATES = ("idle", "swept", "reclaimed", "confirmed_waiting_fill", "filled",
             "expired", "cancelled_trend", "cancelled_ob_invalid",
             "cancelled_duplicate", "cancelled_position_limit",
             "cancelled_spread", "cancelled_risk")

# Meta keys the engine sets at confirmation and removes again on expiry so
# the M5 fallback continues on a completely clean order.
_CONFIRM_META_KEYS = (
    "sweep_reclaim_confirmed", "entry_timeframe", "entry_trigger",
    "m1_event_id", "m1_sweep_time", "m1_sweep_index", "m1_sweep_price",
    "m1_reclaim_time", "m1_reclaim_index", "m1_reclaim_close",
    "m1_reclaim_lag_bars", "m1_confirmation_time", "m1_confirmation_bid",
    "m1_confirmation_ask", "m1_spread_at_confirmation", "m1_stop_refined",
    "reclaim_close",
)


def m1_stats_defaults() -> dict:
    """Telemetry keys for M1 Entry Assist (always present, zero when off)."""
    keys = [
        "m1_bars_created", "m1_empty_minutes", "m1_tick_gaps",
        "m1_out_of_order_ticks", "m1_duplicate_ticks", "m1_partial_bars",
        "m1_max_ticks_per_bar", "m1_min_ticks_per_bar",
        "m1_setups_observed", "m1_setups_eligible",
        "m1_sweeps_detected", "m1_reclaims_detected",
        "m1_confirmations_total", "m1_confirmations_filled",
        "m1_confirmations_expired", "m1_confirmations_shadow",
        "m1_rejected_pre_setup_lookahead",
        "m1_rejected_trend_mismatch", "m1_rejected_trend_neutral",
        "m1_rejected_ob_invalid", "m1_rejected_break_kind",
        "m1_rejected_no_fresh_sweep", "m1_rejected_reclaim_before_sweep",
        "m1_rejected_reclaim_too_late", "m1_rejected_duplicate",
        "m1_rejected_confirmation_cap",
        "m1_rejected_position_limit", "m1_rejected_portfolio_risk",
        "m1_rejected_spread", "m1_rejected_sizing",
        "m1_cancelled_trend_change", "m1_cancelled_ob_invalid",
        "m1_sequence_valid", "m1_sequence_invalid",
        "m1_sequence_ambiguous", "m1_sequence_missing_data",
        "m1_duplicate_events_blocked", "m1_duplicate_fills_blocked",
        "m1_m5_duplicate_entries_prevented",
        "m1_entries_bos", "m1_entries_strong_choch",
        "m1_wins", "m1_losses",
        "m1_confirmations_early_vs_m5", "m1_confirmations_unique_vs_m5",
        "m1_confirmations_later_confirmed_by_m5",
        "m1_confirmations_never_confirmed_by_m5",
        "m1_shadow_wins", "m1_shadow_losses",
    ]
    out: dict = {key: 0 for key in keys}
    out.update({"m1_total_r": 0.0, "m1_shadow_total_r": 0.0,
                "m1_gross_win_r": 0.0, "m1_gross_loss_r": 0.0,
                "m1_lead_minutes_sum": 0.0, "m1_max_lead_minutes": 0.0,
                "m1_reclaim_lag_sum_bars": 0})
    return out


class M1AssistEngine:
    """Owns all M1-assist state; lives on PaperBroker when the flag is on."""

    def __init__(self, broker: "PaperBroker"):
        self.broker = broker
        self.cfg = broker.cfg
        self.m1_index = 0                    # index the NEXT closed M1 bar gets
        self.tick_count = 0                  # ticks seen (fill-delay metric)
        self.last_tick_time: str | None = None
        self.setups: dict[str, dict] = {}    # order_id -> state record
        self.ob_confirmations: dict[str, int] = {}
        self.used_event_ids: set[str] = set()
        # Shadow keeps its OWN consumption books so observe-only runs can
        # never block (or be blocked by) real event/cap consumption.
        self.shadow_used_event_ids: set[str] = set()
        self.shadow_ob_confirmations: dict[str, int] = {}
        self.shadow_positions: list[dict] = []
        self.shadow_events: list[dict] = []
        self.counterfactuals: list[dict] = []
        self.decision_events: list[dict] = []
        self.window_had_gap = False          # empty minutes inside current M5
        self.debug = False                   # --debug-m1: log every sweep/reclaim

    # ------------------------------------------------------------------
    # Registration / causality
    # ------------------------------------------------------------------
    def register_setup(self, order: PendingOrder) -> None:
        """Called by the broker the moment any pending setup is created.
        The first eligible M1 bar is the NEXT one to close -- bars closed on
        or before the creating M5 close are structurally ineligible."""
        self.broker.stats["m1_setups_observed"] += 1
        record = {
            "order_id": order.id, "ob_id": order.ob_id,
            "direction": order.direction,
            "entry_edge": float(order.meta.get("ob_entry_price", order.entry)),
            "base_stop": float(order.meta.get("ob_stop_price", order.stop)),
            "atr": float(order.meta.get("atr_at_formation") or 0.0),
            "break_kind": order.meta.get("break_kind"),
            "strong_choch": bool(order.meta.get("bos_displacement_top_quartile")),
            "state": "idle",
            "first_eligible_m1_index": self.m1_index,
            "m5_setup_confirmed_time": order.created_time,
            "m5_setup_confirmed_index": order.created_index,
            "sweep_index": None, "sweep_price": None, "sweep_time": None,
            "reclaim_index": None, "reclaim_close": None, "reclaim_time": None,
            "confirmed_at_m1_index": None, "pre_confirm_stop": None,
            "confirm_tick_count": None, "confirm_time": None,
            "window_events": [],
        }
        self.setups[order.id] = record
        order.meta.setdefault("m5_setup_id", order.id)
        order.meta["first_eligible_m1_index"] = self.m1_index
        order.meta["first_eligible_m1_time"] = self.last_tick_time

    def on_tick(self, tick) -> None:
        self.tick_count += 1
        self.last_tick_time = tick.time

    # ------------------------------------------------------------------
    # Closed M1 bar processing (the heart of the assist).
    # ------------------------------------------------------------------
    def on_m1_close(self, bar: M1Candle) -> None:
        index = self.m1_index
        self.m1_index += 1
        self._walk_shadow_positions(bar)
        for order in self.broker.pending:
            record = self.setups.get(order.id)
            if record is None or not order.active:
                continue
            if record["state"] in ("filled", "expired") or \
                    record["state"].startswith("cancelled"):
                continue
            if index < record["first_eligible_m1_index"]:
                # Structurally unreachable thanks to the M1-before-M5 close
                # ordering; counted as a hard guard against regressions.
                self.broker.stats["m1_rejected_pre_setup_lookahead"] += 1
                continue
            self._advance_setup(order, record, bar, index)
        self._expire_confirmations(index)

    def _advance_setup(self, order: PendingOrder, record: dict,
                       bar: M1Candle, index: int) -> None:
        bull = record["direction"] == "bull"
        edge = record["entry_edge"]
        swept_now = bar.low < edge if bull else bar.high > edge
        reclaimed_now = bar.close > edge if bull else bar.close < edge
        state = record["state"]
        if state == "confirmed_waiting_fill":
            return  # waiting on the tick pipeline; expiry handled separately
        if record["state"] == "idle" and record["sweep_index"] is None and \
                reclaimed_now and not swept_now:
            # Reclaim-shaped close without any sweep: no M1 signal, but it is
            # sequence evidence (a reclaim BEFORE any sweep in this window).
            record["window_events"].append(("reclaim", index))
            return
        if swept_now:
            extreme = bar.low if bull else bar.high
            if record["sweep_index"] is None:
                self.broker.stats["m1_sweeps_detected"] += 1
                record.update({"sweep_index": index, "sweep_price": extreme,
                               "sweep_time": bar.time, "state": "swept"})
                if self.debug:
                    self._log("m1_sweep_detected", record, index,
                              decision="observed", reason="wick_through_entry_edge")
            else:
                # Deeper/renewed sweep restarts the window (same documented
                # rule as the M5 window machine).
                record["sweep_price"] = (min(record["sweep_price"], extreme) if bull
                                         else max(record["sweep_price"], extreme))
                record["sweep_index"] = index
                record["sweep_time"] = bar.time
            record["window_events"].append(("sweep", index))
        elif record["state"] == "swept" and record["sweep_index"] is not None and \
                index - record["sweep_index"] > self.cfg.m1_reclaim_max_bars:
            # Sweep too old without reclaim: reset to idle (window expired).
            record.update({"state": "idle", "sweep_index": None,
                           "sweep_price": None, "sweep_time": None})
        if not reclaimed_now or record["sweep_index"] is None:
            return
        lag = index - record["sweep_index"]
        record["window_events"].append(("reclaim", index))
        if lag > self.cfg.m1_reclaim_max_bars:
            self.broker.stats["m1_rejected_reclaim_too_late"] += 1
            record.update({"state": "idle", "sweep_index": None,
                           "sweep_price": None, "sweep_time": None})
            return
        self.broker.stats["m1_reclaims_detected"] += 1
        record.update({"reclaim_index": index, "reclaim_close": bar.close,
                       "reclaim_time": bar.time, "state": "reclaimed"})
        self._try_confirm(order, record, bar, index, lag)

    # ------------------------------------------------------------------
    # Confirmation gates (entry + shadow modes).
    # ------------------------------------------------------------------
    def _try_confirm(self, order: PendingOrder, record: dict,
                     bar: M1Candle, index: int, lag: int) -> None:
        stats = self.broker.stats
        if self.cfg.m1_assist_mode == "sequence":
            record["state"] = "idle"      # sequence mode only observes order
            return
        reason = None
        trend_check = validate_trade_direction(record["direction"],
                                               self.broker.current_m5_trend)
        if order.meta.get("sweep_reclaim_confirmed"):
            reason = "m5_already_confirmed"
        elif order.lifecycle_state != "armed":
            reason = "not_armed"
        elif not trend_check.is_allowed:
            key = ("m1_rejected_trend_neutral"
                   if trend_check.rejection_reason == TRADE_REJECTED_M5_TREND_NEUTRAL
                   else "m1_rejected_trend_mismatch")
            stats[key] += 1
            reason = "trend"
        elif record["ob_id"] in self.broker.invalidated_obs:
            stats["m1_rejected_ob_invalid"] += 1
            reason = "ob_invalid"
        elif not self._break_kind_eligible(record):
            stats["m1_rejected_break_kind"] += 1
            reason = "break_kind"
        shadow = self.cfg.m1_assist_mode == "shadow"
        ids = self.shadow_used_event_ids if shadow else self.used_event_ids
        caps = self.shadow_ob_confirmations if shadow else self.ob_confirmations
        event_id = self._event_id(record)
        if reason is None and event_id in ids:
            stats["m1_rejected_duplicate"] += 1
            stats["m1_duplicate_events_blocked"] += 1
            record["state"] = "cancelled_duplicate"
            reason = "duplicate_event"
        if reason is None and \
                caps.get(record["ob_id"], 0) >= self.cfg.m1_max_confirmations_per_ob:
            stats["m1_rejected_confirmation_cap"] += 1
            reason = "confirmation_cap"
        if reason is not None:
            self._log("m1_confirmation_rejected", record, index,
                      decision="rejected", reason=reason)
            if record["state"] == "reclaimed":
                record["state"] = "idle"
            return
        ids.add(event_id)
        caps[record["ob_id"]] = caps.get(record["ob_id"], 0) + 1
        stats["m1_confirmations_total"] += 1
        stats["m1_reclaim_lag_sum_bars"] += lag
        stats["m1_setups_eligible"] += 1
        if self.cfg.m1_assist_mode == "shadow":
            self._open_shadow(record, bar, event_id, lag)
            record["state"] = "idle"     # real setup stays untouched/unconsumed
            self._log("m1_shadow_confirmation", record, index,
                      decision="shadow", reason="fresh_reclaim_within_window")
            return
        # --- entry mode: confirm the REAL order through the standard meta ---
        record["pre_confirm_stop"] = order.stop
        stop, refined = self._confirmed_stop(record)
        order.stop = stop
        order.meta.update({
            "sweep_reclaim_confirmed": True,
            "entry_timeframe": "M1",
            "entry_trigger": "m1_sweep_reclaim",
            "m1_assist_enabled": True,
            "m1_assist_mode": self.cfg.m1_assist_mode,
            "m1_event_id": event_id,
            "m1_sweep_time": record["sweep_time"],
            "m1_sweep_index": record["sweep_index"],
            "m1_sweep_price": record["sweep_price"],
            "m1_reclaim_time": record["reclaim_time"],
            "m1_reclaim_index": record["reclaim_index"],
            "m1_reclaim_close": record["reclaim_close"],
            "m1_reclaim_lag_bars": lag,
            "m1_confirmation_time": bar.time,
            "m1_confirmation_bid": bar.bid_close,
            "m1_confirmation_ask": bar.ask_close,
            "m1_spread_at_confirmation": bar.spread_close,
            "m1_stop_refined": refined,
            "reclaim_close": bar.close,
            "sweep_reclaim_atr_buffer": self.cfg.sweep_reclaim_atr_buffer,
            "entry_mode": "sweep_reclaim",
        })
        record.update({"state": "confirmed_waiting_fill",
                       "confirmed_at_m1_index": index,
                       "confirm_tick_count": self.tick_count,
                       "confirm_time": bar.time})
        self._start_counterfactual(order, record, bar)
        self._log("m1_reclaim_confirmed", record, index,
                  decision="accepted", reason="fresh_reclaim_within_window")

    def _confirmed_stop(self, record: dict) -> tuple[float, bool]:
        """Stop for a confirmed M1 entry. Default: the untouched M5 rule
        (OB stop widened by the M5 sweep ATR buffer). Only with the separate
        --m1-refine-stop flag: behind the REAL M1 sweep extreme plus its own
        buffer; falls back to the M5 rule when the refined stop is invalid."""
        bull = record["direction"] == "bull"
        atr = record["atr"]
        m5_stop = (record["base_stop"] - atr * self.cfg.sweep_reclaim_atr_buffer
                   if bull else
                   record["base_stop"] + atr * self.cfg.sweep_reclaim_atr_buffer)
        if not self.cfg.m1_refine_stop or record["sweep_price"] is None:
            return m5_stop, False
        refined = (record["sweep_price"] - atr * self.cfg.m1_stop_atr_buffer
                   if bull else
                   record["sweep_price"] + atr * self.cfg.m1_stop_atr_buffer)
        edge = record["entry_edge"]
        if (bull and refined >= edge) or (not bull and refined <= edge):
            return m5_stop, False
        return refined, True

    def _break_kind_eligible(self, record: dict) -> bool:
        if record["break_kind"] == "BOS":
            return True
        return record["break_kind"] == "CHoCH" and record["strong_choch"]

    def _event_id(self, record: dict) -> str:
        return (f"{self.cfg.symbol}:{record['order_id']}:{record['ob_id']}:"
                f"{record['direction']}:{record['sweep_index']}:"
                f"{record['reclaim_index']}")

    # ------------------------------------------------------------------
    # Expiry: unfilled confirmation reverts to the clean M5 fallback.
    # ------------------------------------------------------------------
    def _expire_confirmations(self, index: int) -> None:
        for order in self.broker.pending:
            record = self.setups.get(order.id)
            if record is None or record["state"] != "confirmed_waiting_fill":
                continue
            if not order.active:
                continue
            if index - record["confirmed_at_m1_index"] < self.cfg.m1_entry_expiry_bars:
                continue
            if order.meta.get("entry_trigger") != "m1_sweep_reclaim":
                continue  # defensive: never undo an M5-made confirmation
            order.stop = record["pre_confirm_stop"]
            for key in _CONFIRM_META_KEYS:
                order.meta.pop(key, None)
            # The confirmation budget (m1_max_confirmations_per_ob) stays
            # consumed; the machine returns to idle so a LATER fresh
            # sweep+reclaim may try again if budget remains.
            record.update({"state": "idle", "sweep_index": None,
                           "sweep_price": None, "reclaim_index": None,
                           "expired_confirmations":
                               record.get("expired_confirmations", 0) + 1})
            self.broker.stats["m1_confirmations_expired"] += 1
            self._log("m1_confirmation_expired", record, index,
                      decision="expired", reason="no_fill_within_expiry")

    # ------------------------------------------------------------------
    # M5-boundary hooks.
    # ------------------------------------------------------------------
    def m5_confirmation_allowed(self, order: PendingOrder) -> bool:
        """Sequence validation of an M5 same-bar confirmation. Classifies the
        M1 ordering evidence and -- in sequence/entry modes only -- vetoes a
        confirmation whose reclaim was seen before any sweep."""
        record = self.setups.get(order.id)
        stats = self.broker.stats
        if record is None or not record["window_events"]:
            stats["m1_sequence_missing_data"] += 1
            order.meta["m1_sequence_status"] = "missing_m1_data"
            return True
        first_sweep = next((i for kind, i in record["window_events"]
                            if kind == "sweep"), None)
        first_reclaim = next((i for kind, i in record["window_events"]
                              if kind == "reclaim"), None)
        if self.window_had_gap:
            status = "ambiguous_sequence"
            stats["m1_sequence_ambiguous"] += 1
        elif first_sweep is not None and (first_reclaim is None or
                                          first_reclaim >= first_sweep):
            status = "valid_sequence"
            stats["m1_sequence_valid"] += 1
        elif first_reclaim is not None and (first_sweep is None or
                                            first_reclaim < first_sweep):
            status = "invalid_sequence"
            stats["m1_sequence_invalid"] += 1
        else:
            status = "ambiguous_sequence"
            stats["m1_sequence_ambiguous"] += 1
        order.meta["m1_sequence_status"] = status
        veto = (status == "invalid_sequence" and
                self.cfg.m1_use_sequence_validation and
                self.cfg.m1_assist_mode in ("sequence", "entry"))
        if veto:
            self._log("m5_confirmation_vetoed", record, self.m1_index - 1,
                      decision="vetoed", reason="reclaim_before_sweep_on_m1")
        return not veto

    def on_m5_close(self, candle, bar_index: int) -> None:
        """Reset per-setup sequence windows and advance the retrospective
        counterfactual trackers (never used for live decisions)."""
        for record in self.setups.values():
            record["window_events"] = []
        self.window_had_gap = False
        for tracker in self.counterfactuals:
            if tracker["status"] != "pending":
                continue
            bull = tracker["direction"] == "bull"
            edge = tracker["edge"]
            swept = candle.low < edge if bull else candle.high > edge
            if swept:
                tracker["m5_sweep_index"] = bar_index
            window = max(1, self.cfg.sweep_reclaim_max_bars)
            if (tracker["m5_sweep_index"] is not None and
                    bar_index - tracker["m5_sweep_index"] >= window):
                tracker["m5_sweep_index"] = bar_index if swept else None
            reclaimed = (candle.close > edge if bull else candle.close < edge)
            if tracker["m5_sweep_index"] is not None and reclaimed:
                tracker["status"] = "later_confirmed_by_m5"
                self.broker.stats["m1_confirmations_later_confirmed_by_m5"] += 1
                self.broker.stats["m1_confirmations_early_vs_m5"] += 1
                lead = _minutes_between(tracker["m1_confirm_time"], candle.time)
                if lead is not None:
                    self.broker.stats["m1_lead_minutes_sum"] = round(
                        self.broker.stats["m1_lead_minutes_sum"] + lead, 3)
                    self.broker.stats["m1_max_lead_minutes"] = max(
                        self.broker.stats["m1_max_lead_minutes"], lead)
                    tracker["m1_lead_minutes"] = lead
                self._backfill_counterfactual(tracker, unique=False)
            elif bar_index - tracker["created_index"] > 200:
                tracker["status"] = "never_confirmed_by_m5"
                self.broker.stats["m1_confirmations_never_confirmed_by_m5"] += 1
                self.broker.stats["m1_confirmations_unique_vs_m5"] += 1
                self._backfill_counterfactual(tracker, unique=True)

    def note_window_gap(self) -> None:
        self.window_had_gap = True

    def _backfill_counterfactual(self, tracker: dict, unique: bool) -> None:
        """Retrospective classification (early vs unique) stamped onto the
        already-closed trade's meta for breakdown reporting. Never a live
        decision input -- it only annotates history."""
        for trade in self.broker.trades:
            if trade.order_id == tracker["order_id"]:
                trade.meta["m1_unique_vs_m5"] = unique
                if tracker.get("m1_lead_minutes") is not None:
                    trade.meta["m1_lead_minutes"] = tracker["m1_lead_minutes"]
                break
        for position in self.broker.positions:
            if position.order_id == tracker["order_id"]:
                position.meta["m1_unique_vs_m5"] = unique
                if tracker.get("m1_lead_minutes") is not None:
                    position.meta["m1_lead_minutes"] = tracker["m1_lead_minutes"]
                break

    # ------------------------------------------------------------------
    # Fill / close / cancel mirrors from the normal pipeline.
    # ------------------------------------------------------------------
    def on_fill(self, order: PendingOrder, position, when: str,
                spread: float) -> None:
        if order.meta.get("entry_trigger") != "m1_sweep_reclaim":
            return
        record = self.setups.get(order.id)
        stats = self.broker.stats
        stats["m1_confirmations_filled"] += 1
        if order.meta.get("break_kind") == "BOS":
            stats["m1_entries_bos"] += 1
        else:
            stats["m1_entries_strong_choch"] += 1
        position.meta["m1_fill_time"] = when
        position.meta["m1_spread_at_fill"] = spread
        if record is not None:
            record["state"] = "filled"
            if record.get("confirm_tick_count") is not None:
                position.meta["m1_fill_delay_ticks"] = (
                    self.tick_count - record["confirm_tick_count"])
            delay_ms = _minutes_between(record.get("confirm_time"), when)
            position.meta["m1_fill_delay_ms"] = (round(delay_ms * 60_000, 1)
                                                 if delay_ms is not None else None)
            self._log("m1_entry_filled", record, self.m1_index - 1,
                      decision="filled", reason="first_eligible_tick")
            for tracker in self.counterfactuals:
                if tracker["order_id"] == order.id:
                    tracker["filled"] = True

    def on_trade_closed(self, trade: Trade) -> None:
        if trade.meta.get("entry_trigger") != "m1_sweep_reclaim":
            return
        stats = self.broker.stats
        stats["m1_wins" if trade.result == "win" else "m1_losses"] += 1
        stats["m1_total_r"] = round(stats["m1_total_r"] + trade.r_multiple, 6)
        if trade.r_multiple > 0:
            stats["m1_gross_win_r"] = round(stats["m1_gross_win_r"] + trade.r_multiple, 6)
        else:
            stats["m1_gross_loss_r"] = round(stats["m1_gross_loss_r"] - trade.r_multiple, 6)

    def on_order_cancelled(self, order: PendingOrder, cause: str) -> None:
        record = self.setups.get(order.id)
        if record is None or record["state"] in ("filled", "expired"):
            return
        if record["state"].startswith("cancelled"):
            return
        if cause == "trend":
            record["state"] = "cancelled_trend"
            self.broker.stats["m1_cancelled_trend_change"] += 1
        else:
            record["state"] = "cancelled_ob_invalid"
            self.broker.stats["m1_cancelled_ob_invalid"] += 1

    def on_fill_rejected(self, order: PendingOrder, kind: str) -> None:
        """Mirror fill-path rejections of an M1-confirmed order into the M1
        state machine and telemetry (the actual rejection handling stays in
        the normal pipeline)."""
        if order.meta.get("entry_trigger") != "m1_sweep_reclaim":
            return
        record = self.setups.get(order.id)
        counters = {"position_limit": "m1_rejected_position_limit",
                    "portfolio_risk": "m1_rejected_portfolio_risk",
                    "spread": "m1_rejected_spread",
                    "sizing": "m1_rejected_sizing"}
        if kind in counters:
            self.broker.stats[counters[kind]] += 1
        if record is not None and kind in ("portfolio_risk", "sizing"):
            record["state"] = ("cancelled_risk" if kind != "position_limit"
                               else "cancelled_position_limit")

    # ------------------------------------------------------------------
    # Shadow ledger (observe-only).
    # ------------------------------------------------------------------
    def _open_shadow(self, record: dict, bar: M1Candle, event_id: str,
                     lag: int) -> None:
        bull = record["direction"] == "bull"
        stop, refined = self._confirmed_stop(record)
        fill = bar.ask_close if bull else bar.bid_close
        distance = abs(fill - stop)
        if distance <= 0:
            return
        target = fill + self.cfg.rr * distance if bull else fill - self.cfg.rr * distance
        self.broker.stats["m1_confirmations_shadow"] += 1
        self.shadow_positions.append({
            "event_id": event_id, "direction": record["direction"],
            "entry": fill, "stop": stop, "target": target,
            "fill_time": bar.time, "ob_id": record["ob_id"],
            "m5_setup_id": record["order_id"],
            "break_kind": record["break_kind"],
            "sweep_index": record["sweep_index"],
            "reclaim_index": record["reclaim_index"],
            "lag_bars": lag, "mfe": 0.0, "mae": 0.0,
            "stop_refined": refined,
        })

    def _walk_shadow_positions(self, bar: M1Candle) -> None:
        if not self.shadow_positions:
            return
        stats = self.broker.stats
        for pos in list(self.shadow_positions):
            bull = pos["direction"] == "bull"
            distance = abs(pos["entry"] - pos["stop"])
            favorable = (bar.high - pos["entry"]) if bull else (pos["entry"] - bar.low)
            adverse = (pos["entry"] - bar.low) if bull else (bar.high - pos["entry"])
            pos["mfe"] = max(pos["mfe"], favorable / distance if distance else 0.0)
            pos["mae"] = max(pos["mae"], adverse / distance if distance else 0.0)
            hit_sl = bar.low <= pos["stop"] if bull else bar.high >= pos["stop"]
            hit_tp = bar.high >= pos["target"] if bull else bar.low <= pos["target"]
            if not (hit_sl or hit_tp):
                continue
            result_r = -1.0 if hit_sl else self.cfg.rr   # SL-first, conservative
            key = "m1_shadow_losses" if hit_sl else "m1_shadow_wins"
            stats[key] += 1
            stats["m1_shadow_total_r"] = round(
                stats["m1_shadow_total_r"] + result_r, 6)
            self.shadow_events.append({
                "shadow_entry": pos["entry"], "shadow_stop": pos["stop"],
                "shadow_target": pos["target"], "shadow_fill_time": pos["fill_time"],
                "shadow_exit_time": bar.time, "shadow_result_r": result_r,
                "shadow_max_favorable_excursion_r": round(pos["mfe"], 4),
                "shadow_max_adverse_excursion_r": round(pos["mae"], 4),
                "shadow_break_kind": pos["break_kind"],
                "shadow_m5_setup_id": pos["m5_setup_id"],
                "shadow_m1_sweep_index": pos["sweep_index"],
                "shadow_m1_reclaim_index": pos["reclaim_index"],
                "direction": pos["direction"], "lag_bars": pos["lag_bars"],
            })
            self.shadow_positions.remove(pos)

    # ------------------------------------------------------------------
    # Counterfactual "would M5 have confirmed later?" (retrospective only).
    # ------------------------------------------------------------------
    def _start_counterfactual(self, order: PendingOrder, record: dict,
                              bar: M1Candle) -> None:
        self.counterfactuals.append({
            "order_id": order.id, "direction": record["direction"],
            "edge": record["entry_edge"],
            "m5_sweep_index": order.meta.get("sweep_reclaim_sweep_index"),
            "created_index": self.broker.market_index or 0,
            "m1_confirm_time": bar.time, "status": "pending", "filled": False,
        })

    # ------------------------------------------------------------------
    # Structured decision log.
    # ------------------------------------------------------------------
    def _log(self, event: str, record: dict, m1_index: int, decision: str,
             reason: str) -> None:
        self.decision_events.append({
            "time": record.get("confirm_time") or record.get("reclaim_time")
            or record.get("sweep_time") or self.last_tick_time,
            "event": event, "feature": "m1_entry_assist", "level": "DECISION",
            "symbol": self.cfg.symbol, "m5_setup_id": record["order_id"],
            "ob_id": record["ob_id"], "direction": record["direction"],
            "m1_sweep_index": record.get("sweep_index"),
            "m1_reclaim_index": record.get("reclaim_index"),
            "m1_index": m1_index, "decision": decision, "reason": reason,
        })

    # ------------------------------------------------------------------
    # Persistence.
    # ------------------------------------------------------------------
    def dump_state(self) -> dict:
        return {"m1_index": self.m1_index, "tick_count": self.tick_count,
                "last_tick_time": self.last_tick_time,
                "setups": {k: dict(v) for k, v in self.setups.items()},
                "ob_confirmations": dict(self.ob_confirmations),
                "used_event_ids": sorted(self.used_event_ids),
                "shadow_used_event_ids": sorted(self.shadow_used_event_ids),
                "shadow_ob_confirmations": dict(self.shadow_ob_confirmations),
                "shadow_positions": [dict(x) for x in self.shadow_positions],
                "shadow_events": [dict(x) for x in self.shadow_events],
                "counterfactuals": [dict(x) for x in self.counterfactuals]}

    def load_state(self, data: dict) -> None:
        self.m1_index = int(data.get("m1_index", 0))
        self.tick_count = int(data.get("tick_count", 0))
        self.last_tick_time = data.get("last_tick_time")
        self.setups = {str(k): dict(v) for k, v in (data.get("setups") or {}).items()}
        for record in self.setups.values():
            record["window_events"] = [tuple(x) for x in record.get("window_events", [])]
        self.ob_confirmations = {str(k): int(v) for k, v
                                 in (data.get("ob_confirmations") or {}).items()}
        self.used_event_ids = set(data.get("used_event_ids", []))
        self.shadow_used_event_ids = set(data.get("shadow_used_event_ids", []))
        self.shadow_ob_confirmations = {str(k): int(v) for k, v in
                                        (data.get("shadow_ob_confirmations") or {}).items()}
        self.shadow_positions = [dict(x) for x in data.get("shadow_positions", [])]
        self.shadow_events = [dict(x) for x in data.get("shadow_events", [])]
        self.counterfactuals = [dict(x) for x in data.get("counterfactuals", [])]


def _minutes_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
        return round(delta.total_seconds() / 60.0, 3)
    except ValueError:
        return None
