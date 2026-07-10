from __future__ import annotations

import logging
import time
import copy
import shutil
from dataclasses import asdict
from datetime import datetime, time as datetime_time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import BotConfig
from .dashboard_server import DashboardServer
from .demo_executor import DemoExecutor
from .entry_pivot import ClassicEntryPivotDetector
from .models import Candle
from .mt5_feed import MT5ReadOnlyFeed
from .mtf_context import M15Context
from .liquidity_context import LiquidityTracker
from .market_recorder import MarketRecorder
from .paper import PaperBroker
from .pine_engine import PineSwingOBEngine
from .reporting import (export_breakdown, export_daily_html, export_daily_metrics,
                        export_html, export_reports)
from .report_worker import ReportWorker
from .storage import StateStore
from .structure_context import ChochContext, DisplacementContext, displacement_snapshot
from .trend_filter import trend_from_engine_state


class PaperApp:
    def __init__(self, cfg: BotConfig, feed: MT5ReadOnlyFeed):
        cfg.validate()
        self.cfg, self.feed = cfg, feed
        cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s | %(levelname)s | %(message)s",
                            handlers=[logging.StreamHandler(), logging.FileHandler(cfg.log_path, encoding="utf-8")])
        self.log = logging.getLogger("pine-ob-paper")
        self.store = StateStore(cfg.db_path)
        equity, spec = feed.connect()
        account = feed.mt5.account_info()
        account_snapshot = {
            "login": int(account.login), "server": account.server,
            "trade_mode": int(account.trade_mode), "currency": account.currency,
            "leverage": int(account.leverage), "balance": float(account.balance),
            "equity": float(account.equity), "symbol": feed.symbol,
            "tick_size": spec.tick_size, "tick_value": spec.tick_value,
            "volume_min": spec.volume_min, "volume_max": spec.volume_max,
            "volume_step": spec.volume_step,
        }
        now = datetime.now(timezone.utc).isoformat()
        self.store.record_event(now, "mt5_account_connected", account_snapshot)
        self.log.info("MT5 account=%s server=%s mode=%s equity=%.2f symbol=%s",
                      account.login, account.server,
                      "DEMO" if account.trade_mode == feed.mt5.ACCOUNT_TRADE_MODE_DEMO else account.trade_mode,
                      account.equity, feed.symbol)
        self.engine = PineSwingOBEngine(cfg)
        self.m15 = M15Context(cfg)
        self.liq_m5 = LiquidityTracker(cfg.swing_length, cfg.atr_period)
        self.liq_m15 = LiquidityTracker(cfg.m15_swing_length, cfg.m15_atr_period)
        self.choch = ChochContext()
        self.displacement = DisplacementContext(cfg.displacement_rank_window,
                                                cfg.displacement_rank_min_samples)
        self.choch_displacement = DisplacementContext(cfg.displacement_rank_window,
                                                      cfg.displacement_rank_min_samples)
        # Independent classic Entry Pivot detector (default 5/5), used only
        # for local pullback/trigger timing -- never for the M5 Trend State,
        # which stays exclusively owned by self.engine.trend above.
        self.entry_pivot = ClassicEntryPivotDetector(cfg.entry_pivot_left, cfg.entry_pivot_right)
        self.broker = PaperBroker(cfg, equity, spec)
        # Live paper trading always enforces the Entry Pivot admission gate.
        self.broker.enable_entry_pivot_gate()
        self.m1_builder = None
        if cfg.m1_entry_assist:
            from .m1 import M1Builder
            self.m1_builder = M1Builder()
            self.log.warning(
                "M1 Entry Assist is EXPERIMENTAL and live M1 bars are built "
                "from polled ticks (~1/s): bar quality is approximate "
                "(tick_count/partial flags record it); the in-progress M1 "
                "bar is not persisted across restarts.")
        self.recorder = MarketRecorder(cfg.market_capture_dir, self.feed.symbol)
        self.demo = (DemoExecutor(feed.mt5, self.feed.symbol, cfg.demo_magic,
                                  cfg.demo_deviation_points,
                                  cfg.demo_max_free_margin_fraction)
                     if cfg.demo_orders_enabled else None)
        self.last_processed_tick: tuple[str, float, float] | None = None
        self.report_zone = ZoneInfo(cfg.report_timezone)
        self.report_day = datetime.now(self.report_zone).date()
        self.last_report_refresh = 0.0
        self.last_breakdown_refresh = 0.0
        self.report_worker = ReportWorker(self.log)
        self.dashboard = None
        if cfg.dashboard_enabled:
            try:
                self.dashboard = DashboardServer(cfg.daily_reports_dir,
                                                 cfg.dashboard_host, cfg.dashboard_port)
                self.dashboard.start()
                self.log.info("LIVE DASHBOARD %s", self.dashboard.url)
            except OSError as exc:
                self.log.error("dashboard unavailable: %s", exc)

    def restore_or_bootstrap(self) -> None:
        snapshot = self.store.load()
        candles = self.feed.closed_candles(self.cfg.history_bars)
        if snapshot:
            self.engine.load_state(snapshot["engine"])
            self.broker.load_state(snapshot["broker"])
            # Sync the broker's trend to the just-restored engine state before
            # anything else runs. Without this, a snapshot with no missed
            # candles (or one saved before this filter existed) would leave
            # the broker thinking Neutral while the engine already knows the
            # real M5 trend, wrongly blocking/cancelling every pending order.
            self.broker.update_m5_trend(trend_from_engine_state(self.engine.trend),
                                        self.engine.last_time or datetime.now(timezone.utc).isoformat())
            self._drain_trend_events(self.engine.last_time or datetime.now(timezone.utc).isoformat())
            cancelled = self.broker.reconcile_entry_filters()
            retargeted = self.broker.reconcile_pending_targets()
            context = snapshot.get("context")
            if context and "m15" in context:
                self.m15.load_state(context["m15"])
                self.liq_m5.load_state(context["liquidity_m5"])
                self.liq_m15.load_state(context["liquidity_m15"])
                if context.get("choch"):
                    self.choch.load_state(context["choch"])
                if context.get("displacement"):
                    self.displacement.load_state(context["displacement"])
                if context.get("choch_displacement"):
                    self.choch_displacement.load_state(context["choch_displacement"])
                if context.get("entry_pivot"):
                    self.entry_pivot.load_state(context["entry_pivot"])
            elif context:
                self.m15.load_state(context)
                for candle in candles:
                    if self.engine.last_time and candle.time <= self.engine.last_time:
                        self._process_observation_context(candle)
            else:
                for candle in candles:
                    if self.engine.last_time and candle.time <= self.engine.last_time:
                        self._process_observation_context(candle)
            missed = [x for x in candles if not self.engine.last_time or x.time > self.engine.last_time]
            for candle in missed:
                self.on_closed_candle(candle, replay=True)
            self.broker.market_index = len(self.engine.candles) - 1
            self._reconcile_demo_positions()
            self._save()
            self.log.info("restored state; replayed %d missed; cancelled %d disallowed; retargeted %d pending",
                          len(missed), cancelled, retargeted)
            return
        # Warm detector without inventing historical PnL. Keep currently active OBs.
        for candle in candles:
            self._process_observation_context(candle)
            breaks, formed, invalid = self.engine.process(candle)
            self.choch.process(breaks)
            # Trend Swing 12 (Major BOS/CHoCH/Trend State) updates first, then
            # the independent classic Entry Pivot detector, then setups are
            # built -- so no pivot or setup for this candle is ever evaluated
            # against a stale trend.
            self.broker.update_m5_trend(trend_from_engine_state(self.engine.trend), candle.time)
            for pivot in self.entry_pivot.process(candle):
                self.broker.update_entry_pivot(pivot)
            for ob in formed:
                displacement_data = displacement_snapshot(candle, ob, breaks)
                ranker = self.displacement if ob.break_kind == "BOS" else self.choch_displacement
                self.broker.add_ob(ob, {**self._observation_snapshot(ob.direction, candle.close),
                                        **self._setup_liquidity_snapshot(ob.direction, ob.formed_index),
                                        **displacement_data,
                                        **ranker.observe(displacement_data.get("bos_close_through_atr")),
                                        "formation_open": candle.open, "formation_high": candle.high,
                                        "formation_low": candle.low, "formation_close": candle.close,
                                        "major_swing_high": self.engine.swing_high.level
                                        if self.engine.swing_high else None,
                                        "major_swing_low": self.engine.swing_low.level
                                        if self.engine.swing_low else None})
            for ob_id in invalid:
                self.broker.cancel_ob(ob_id)
            self.broker.set_market_context(self._fill_context(candle.close))
        self._drain_trend_events(self.engine.last_time or datetime.now(timezone.utc).isoformat())
        self.broker.market_index = len(self.engine.candles) - 1
        self._reconcile_demo_positions()
        self._save()
        self.log.info("bootstrapped %d closed candles; paper equity %.2f", len(candles), self.broker.equity)

    def on_closed_candle(self, candle: Candle, replay: bool = False) -> None:
        if self.cfg.market_capture_enabled:
            self.recorder.record_candle(candle)
        index = len(self.engine.candles)
        # Live and restart catch-up are deliberately tick-only for execution.
        # A closed M5 candle updates signal/lifecycle state, but cannot create
        # a fill or close a position from ambiguous OHLC ordering.
        self.broker.process_signal_candle(candle, index)
        self._drain_rejected_sizing(candle.time)
        self._process_observation_context(candle)
        breaks, formed, invalid = self.engine.process(candle)
        self.choch.process(breaks)
        # Trend must update -- and cancel any now-opposing pending orders --
        # before any new same-candle signal is turned into an order, and
        # strictly before the next on_tick() fill evaluation.
        self.broker.update_m5_trend(trend_from_engine_state(self.engine.trend), candle.time)
        for pivot in self.entry_pivot.process(candle):
            self.broker.update_entry_pivot(pivot)
            self.store.record_event(candle.time, "entry_pivot_confirmed", asdict(pivot))
        for item in breaks:
            self.store.record_event(candle.time, item.kind.lower(), asdict(item))
        for ob in formed:
            displacement_data = displacement_snapshot(candle, ob, breaks)
            ranker = self.displacement if ob.break_kind == "BOS" else self.choch_displacement
            order = self.broker.add_ob(ob, {**self._observation_snapshot(ob.direction, candle.close),
                                            **self._setup_liquidity_snapshot(ob.direction, ob.formed_index),
                                            **displacement_data,
                                            **ranker.observe(displacement_data.get("bos_close_through_atr")),
                                            "formation_open": candle.open, "formation_high": candle.high,
                                            "formation_low": candle.low, "formation_close": candle.close,
                                            "major_swing_high": self.engine.swing_high.level
                                            if self.engine.swing_high else None,
                                            "major_swing_low": self.engine.swing_low.level
                                            if self.engine.swing_low else None})
            self.store.record_event(candle.time, "ob_formed", asdict(ob))
            if order:
                self.log.info("%s OB limit=%s sl=%s", ob.direction.upper(), order.entry, order.stop)
            else:
                self.store.record_event(candle.time, "ob_rejected",
                                        {"ob_id": ob.id, "break_kind": ob.break_kind})
        for ob_id in invalid:
            self.broker.cancel_ob(ob_id)
            self.store.record_event(candle.time, "ob_invalidated", {"ob_id": ob_id})
        self.broker.set_market_context(self._fill_context(candle.close))
        self._drain_trend_events(candle.time)
        self._save()

    def on_tick(self) -> None:
        tick = self.feed.tick()
        if tick is None:
            return
        identity = (tick.time, tick.bid, tick.ask)
        if identity == self.last_processed_tick:
            return
        self.last_processed_tick = identity
        if self.cfg.market_capture_enabled:
            self.recorder.record_tick(tick)
        tick_time = datetime.fromisoformat(tick.time)
        if tick_time.tzinfo is None:
            tick_time = tick_time.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - tick_time.astimezone(timezone.utc)).total_seconds()
        if age > self.cfg.max_live_tick_age_seconds:
            self.log.debug("ignoring stale tick age=%.1fs", age)
            return
        if self.m1_builder is not None:
            # Live M1 is built from POLLED ticks (~1/s), so bars are
            # approximate; tick_count/partial flags record the quality. The
            # closed M1 is processed before this tick executes, and the
            # engine's first_eligible_m1_index guard keeps causality even
            # though live M5 closes arrive on their own polling schedule.
            closed_m1 = self.m1_builder.push(tick)
            if closed_m1 is not None:
                self.broker.process_m1_candle(closed_m1)
        before = {p.id for p in self.broker.positions}
        before_positions = {p.id: p for p in self.broker.positions}
        trades = self.broker.process_tick(tick)
        self._drain_rejected_sizing(tick.time)
        self._drain_trend_events(tick.time)
        self._drain_breakeven_events(tick.time)
        after = {p.id for p in self.broker.positions}
        if before != after:
            for trade in trades:
                original = before_positions.get(trade.id)
                if self.demo and original and original.meta.get("demo_order_sent"):
                    try:
                        result = self.demo.close(trade.id, trade.direction,
                                                 original.meta.get("demo_position_ticket"),
                                                 original.volume)
                        close_price = result.get("price")
                        paper_close_price = trade.exit_price
                        if close_price is not None:
                            self.broker.apply_demo_exit(trade, float(close_price),
                                                        result.get("volume"))
                        trade.meta.update({
                            "execution_state": "demo_exit_confirmed",
                            "demo_close_status": result.get("status"),
                            "demo_close_price": close_price,
                            "demo_close_volume": result.get("volume"),
                            "demo_exit_slippage": (
                                paper_close_price - close_price if close_price is not None
                                and trade.direction == "bull" else
                                close_price - paper_close_price if close_price is not None else None),
                        })
                        self.store.record_event(tick.time, "demo_position_closed", result)
                        self.log.info("DEMO CLOSE ticket=%s status=%s",
                                      result.get("ticket"), result.get("status"))
                    except Exception as exc:
                        trade.meta["execution_state"] = "reconciliation_required_demo_exit"
                        trade.meta["demo_close_error"] = str(exc)
                        self.store.record_event(tick.time, "demo_close_failed", {"error": str(exc)})
                        self.log.error("DEMO CLOSE FAILED: %s", exc)
                self.store.record_trade(trade)
                self.store.record_event(tick.time, "trade_closed", asdict(trade))
                self.log.info("CLOSE %s pnl=%.2f R=%.2f equity=%.2f",
                              trade.result.upper(), trade.pnl, trade.r_multiple, self.broker.equity)
            for position in list(self.broker.positions):
                if position.id not in before:
                    mirrored = False
                    if self.demo and position.meta.get("entry_execution_source") == "tick":
                        try:
                            result = self.demo.open(position)
                            self.broker.apply_demo_entry(position, result["price"],
                                                         result["volume"])
                            mirrored = True
                            self.store.record_event(tick.time, "demo_position_opened", result)
                            self.log.info("DEMO OPEN ticket=%s volume=%s",
                                          result.get("ticket"), result.get("volume"))
                        except Exception as exc:
                            rejected = self.broker.reject_open(position.id, str(exc))
                            self.store.record_event(tick.time, "demo_open_failed", {"error": str(exc)})
                            self.store.record_event(tick.time, "paper_open_reverted",
                                                    {"position_id": position.id,
                                                     "order_id": position.order_id,
                                                     "error": str(exc)})
                            self.log.error("DEMO OPEN FAILED: %s", exc)
                            if rejected:
                                self.log.warning("PAPER OPEN REVERTED id=%s", position.id)
                    if not self.demo or mirrored:
                        self.store.record_event(tick.time, "position_opened", asdict(position))
                        self.log.info("OPEN %s @ %s", position.direction.upper(), position.entry)
            self._save()

    def _drain_rejected_sizing(self, when: str) -> None:
        """Log and persist every trade rejected by the sizing guard.

        A rejection here means realized risk could never be kept within the
        configured percentage without violating the broker's minimum lot, or
        without breaching the risk cap after volume_max/volume_step
        normalization. No order (pending or market) is ever sent for these --
        this only records why.
        """
        if not self.broker.rejected_sizing:
            return
        for rejection in self.broker.rejected_sizing:
            self.store.record_event(when, "order_rejected_volume", rejection)
            self.log.warning(
                "ORDER REJECTED reason=%s ob_id=%s dir=%s entry=%s stop=%s "
                "equity=%.2f risk_pct=%.4f allowed_risk=%.2f "
                "attempted_volume=%.4f attempted_actual_risk=%.2f",
                rejection["rejection_reason"], rejection["ob_id"], rejection["direction"],
                rejection["entry_price"], rejection["stop_loss"], rejection["equity"],
                rejection["risk_percent"], rejection["allowed_risk"],
                rejection["attempted_volume"], rejection["attempted_actual_risk"])
        self.broker.rejected_sizing.clear()

    def _drain_trend_events(self, when: str) -> None:
        """Log and persist every order rejected or cancelled by the M5 trend
        filter (side/current_m5_trend mismatch, or Neutral trend)."""
        if not self.broker.trend_events:
            return
        for event in self.broker.trend_events:
            kind = event.get("event_type", "order_rejected_m5_trend")
            self.store.record_event(event.get("time", when), kind, event)
            self.log.warning(
                "TREND FILTER %s reason=%s ob_id=%s order_id=%s side=%s "
                "current_trend=%s trend_at_creation=%s",
                kind, event.get("reason"), event.get("ob_id"), event.get("order_id"),
                event.get("side"), event.get("current_m5_trend"), event.get("trend_at_creation"))
        self.broker.trend_events.clear()

    def _drain_breakeven_events(self, when: str) -> None:
        """Log and persist every stop just moved to breakeven; in Demo mode
        also mirror the SL modification to MT5 exactly once per position."""
        if not self.broker.breakeven_events:
            return
        for event in self.broker.breakeven_events:
            self.store.record_event(event.get("time", when), "breakeven_armed", event)
            self.log.info("BREAKEVEN ARMED id=%s side=%s entry=%s sl %s -> %s trigger=%sR",
                          event.get("position_id"), event.get("direction"),
                          event.get("entry"), event.get("original_stop"),
                          event.get("new_stop"), event.get("trigger_r"))
            position = next((item for item in self.broker.positions
                             if item.id == event.get("position_id")), None)
            if not (self.demo and position is not None and
                    position.meta.get("demo_order_sent")):
                continue
            try:
                result = self.demo.modify_stop(position, float(event["new_stop"]))
                position.meta["demo_breakeven_status"] = result.get("status")
                self.store.record_event(when, "demo_stop_modified", result)
                self.log.info("DEMO SL MODIFY ticket=%s status=%s sl=%s",
                              result.get("ticket"), result.get("status"), result.get("sl"))
            except Exception as exc:
                position.meta.update({"demo_breakeven_status": "error",
                                      "demo_breakeven_error": str(exc)})
                self.store.record_event(when, "demo_stop_modify_failed",
                                        {"position_id": position.id, "error": str(exc)})
                self.log.error("DEMO SL MODIFY FAILED: %s", exc)
        self.broker.breakeven_events.clear()

    def _reconcile_demo_positions(self) -> None:
        if not self.demo:
            return
        result = self.demo.reconcile(self.broker.positions)
        now = datetime.now(timezone.utc).isoformat()
        self.store.record_event(now, "demo_reconciliation", result)
        for item in result["missing"]:
            self.store.record_event(now, "demo_position_missing", item)
        for item in result["orphans"]:
            self.store.record_event(now, "demo_position_orphan", item)
        self.log.info("DEMO RECONCILE matched=%d missing=%d orphan=%d",
                      len(result["matched"]), len(result["missing"]),
                      len(result["orphans"]))

    def run(self, once: bool = False) -> None:
        self.restore_or_bootstrap()
        last = self.engine.last_time
        last_candle_poll = 0.0
        try:
            while True:
                now = time.monotonic()
                if now - last_candle_poll >= self.cfg.candle_poll_seconds:
                    candles = self.feed.closed_candles(3)
                    for candle in candles:
                        if last is None or candle.time > last:
                            self.on_closed_candle(candle)
                            last = candle.time
                    last_candle_poll = now
                # Closed-candle catch-up must precede current-tick fills.
                self.on_tick()
                self._refresh_daily_report()
                if once:
                    break
                time.sleep(self.cfg.poll_seconds)
        finally:
            self._refresh_daily_report(force=True, block=True)
            self.report_worker.close()
            export_reports(self.broker, self.cfg.trades_csv, self.cfg.summary_csv)
            export_html(self.broker, self.cfg.db_path.parent / "paper_report.html",
                        "Pine Swing-OB Live Paper Report",
                        {"symbol": self.feed.symbol, "timeframe": self.cfg.timeframe,
                         "RR": self.cfg.rr, "risk": f"{self.cfg.risk_fraction * 100:.1f}%"})
            self._save()
            self.store.close()
            self.feed.shutdown()
            if self.dashboard:
                self.dashboard.close()

    def _refresh_daily_report(self, force: bool = False, block: bool = False) -> None:
        today = datetime.now(self.report_zone).date()
        if today != self.report_day:
            self._export_daily_report(self.report_day, block=block)
            self.report_day = today
            force = True
        now = time.monotonic()
        if force or now - self.last_report_refresh >= self.cfg.report_refresh_seconds:
            self._export_daily_report(today, block=block)
            self.last_report_refresh = now

    def _export_daily_report(self, day, block: bool = False) -> None:
        start_local = datetime.combine(day, datetime_time.min, self.report_zone)
        end_local = start_local + timedelta(days=1)
        counts = self.store.event_counts(start_local.astimezone(timezone.utc).isoformat(),
                                         end_local.astimezone(timezone.utc).isoformat())
        context = {"symbol": self.feed.symbol, "timeframe": self.cfg.timeframe,
                   "trend swing": self.cfg.trend_swing_length,
                   "entry pivot": f"{self.cfg.entry_pivot_left}/{self.cfg.entry_pivot_right}",
                   "RR": self.cfg.rr,
                   "breaks": "+".join(self.cfg.allowed_break_kinds),
                   "risk model": "CHoCH+Displacement" if self.cfg.choch_displacement_risk_sizing_enabled else "other",
                   "CHoCH risk cap": self.cfg.choch_risk_cap_fraction,
                   "target mode": self.cfg.target_mode,
                   "entry mode": self.cfg.entry_mode,
                   "demo orders": self.cfg.demo_orders_enabled,
                   "report updated": datetime.now(self.report_zone).isoformat(timespec="seconds"),
                   "last tick": self.last_processed_tick[0] if self.last_processed_tick else "none",
                   "last closed candle": self.engine.last_time or "none",
                   "paper equity": f"{self.broker.equity:.2f}",
                   "open paper positions": len(self.broker.positions),
                   "active pending": sum(item.active for item in self.broker.pending),
                   **{f"events {key}": value for key, value in counts.items()}}
        # Closed Trade objects are immutable after on_tick completes. Copy the
        # container and scalar broker state only; deepcopying full history can
        # pause tick processing as the forward-test grows.
        broker_snapshot = copy.copy(self.broker)
        broker_snapshot.trades = list(self.broker.trades)
        broker_snapshot.positions = list(self.broker.positions)
        broker_snapshot.pending = list(self.broker.pending)
        now = time.monotonic()
        include_breakdown = now - self.last_breakdown_refresh >= self.cfg.breakdown_refresh_seconds
        if include_breakdown:
            self.last_breakdown_refresh = now
        self.report_worker.submit(self._write_daily_reports, broker_snapshot, day, context,
                                  counts, include_breakdown, block=block)

    def _write_daily_reports(self, broker, day, context, counts, include_breakdown) -> None:
        root = self.cfg.daily_reports_dir
        dated = root / f"paper_report_{day.isoformat()}.html"
        export_daily_html(broker, day, self.cfg.report_timezone,
                          root / "paper_report_latest.html", context, refresh_seconds=30)
        shutil.copyfile(root / "paper_report_latest.html", dated)
        export_daily_metrics(broker, day, self.cfg.report_timezone,
                             root / "forward_test_daily_metrics.csv",
                             {"symbol": self.feed.symbol, "timeframe": self.cfg.timeframe,
                              "trend_swing_length": self.cfg.trend_swing_length,
                              "entry_pivot_left": self.cfg.entry_pivot_left,
                              "entry_pivot_right": self.cfg.entry_pivot_right, "rr": self.cfg.rr,
                              "allowed_breaks": "+".join(self.cfg.allowed_break_kinds),
                              "choch_displacement_sizing": self.cfg.choch_displacement_risk_sizing_enabled,
                              "choch_risk_cap": self.cfg.choch_risk_cap_fraction,
                              "target_mode": self.cfg.target_mode, "entry_mode": self.cfg.entry_mode,
                              "min_entry_wait": self.cfg.min_entry_wait_bars,
                              "max_positions": self.cfg.max_open_positions,
                              "m15_swing_length": self.cfg.m15_swing_length,
                              "fallback_spread": self.cfg.fallback_spread,
                              **{f"events_{key}": value for key, value in counts.items()}})
        export_reports(broker, root / "forward_test_trades.csv",
                       root / "forward_test_summary.csv")
        if include_breakdown:
            export_html(broker, root / "forward_test_cumulative.html",
                        "Pine Swing-OB Forward Test — Cumulative", context,
                        refresh_seconds=30)
            export_breakdown(broker, root / "forward_test_breakdown.csv")

    def _save(self) -> None:
        engine_state = self.engine.dump_state(self.cfg.state_history_bars)
        offset = int(engine_state.get("index_offset", 0))
        m15_history = max(500, self.cfg.state_history_bars // 3)
        m15_state = self.m15.dump_state(m15_history)
        m15_offset = int(m15_state["engine"].get("index_offset", 0))
        context = {"m15": m15_state,
                   "liquidity_m5": self.liq_m5.dump_state(index_offset=offset),
                   "liquidity_m15": self.liq_m15.dump_state(index_offset=m15_offset),
                   "choch": self.choch.dump_state(offset)}
        context["displacement"] = self.displacement.dump_state()
        context["choch_displacement"] = self.choch_displacement.dump_state()
        context["entry_pivot"] = self.entry_pivot.dump_state(self.cfg.state_history_bars)
        self.store.save(engine_state, self.broker.dump_state(offset), context)

    def _process_observation_context(self, candle: Candle) -> None:
        m15_bar = self.m15.process_m5(candle)
        self.liq_m5.process(candle)
        if m15_bar:
            self.liq_m15.process(m15_bar)

    def _observation_snapshot(self, direction: str, price: float) -> dict:
        return {**self.m15.snapshot(direction, price),
                **self.liq_m5.snapshot(direction, price, "liq_m5"),
                **self.liq_m15.snapshot(direction, price, "liq_m15"),
                **self.choch.snapshot(direction, max(len(self.engine.candles) - 1, 0))}

    def _fill_context(self, price: float) -> dict:
        return {direction: self._observation_snapshot(direction, price)
                for direction in ("bull", "bear")}

    def _setup_liquidity_snapshot(self, direction: str, bos_index: int) -> dict:
        pivot = self.engine.swing_low if direction == "bull" else self.engine.swing_high
        start = pivot.bar_index if pivot else None
        return self.liq_m5.setup_snapshot(direction, start, bos_index)
