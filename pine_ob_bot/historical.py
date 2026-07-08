from __future__ import annotations

from pathlib import Path

from data.loader import load_m5

from .config import BotConfig
from .entry_pivot import ClassicEntryPivotDetector
from .models import Candle
from .mtf_context import M15Context
from .liquidity_context import LiquidityTracker
from .paper import PaperBroker, SymbolSpec
from .pine_engine import PineSwingOBEngine
from .reporting import export_breakdown, export_html, export_reports
from .storage import StateStore
from .structure_context import ChochContext, DisplacementContext, displacement_snapshot
from .trend_filter import trend_from_engine_state


def run_historical(csv_path: Path, cfg: BotConfig, initial_equity: float = 10_000.0,
                   spread: float = 0.20, run_label: str = "") -> dict:
    """Replay prior M5 Bid OHLC without touching the live-forward-test state.

    Uses the exact same PineSwingOBEngine (Trend Swing / Major BOS-CHoCH) and
    ClassicEntryPivotDetector (Entry Pivot) classes as the live PaperApp path,
    driven in the same order (trend update -> entry pivot -> setups), so Live
    and Historical agree on Major Swing/BOS/CHoCH, Trend State, Entry Pivots
    and admitted/rejected setups for identical OHLC input. Only the fill
    source differs (real ticks live, OHLC replay here).
    """
    frame = load_m5(str(csv_path))
    # XAUUSD defaults match the existing project; volume math is $1/tick/lot.
    broker = PaperBroker(cfg, initial_equity, SymbolSpec(tick_size=.01, tick_value=1.0,
                                                         volume_min=.01, volume_step=.01))
    broker.enable_entry_pivot_gate()
    engine = PineSwingOBEngine(cfg)
    entry_pivot = ClassicEntryPivotDetector(cfg.entry_pivot_left, cfg.entry_pivot_right)
    m15 = M15Context(cfg)
    liq_m5 = LiquidityTracker(cfg.swing_length, cfg.atr_period)
    liq_m15 = LiquidityTracker(cfg.m15_swing_length, cfg.m15_atr_period)
    choch = ChochContext()
    displacement = DisplacementContext(cfg.displacement_rank_window,
                                       cfg.displacement_rank_min_samples)
    choch_displacement = DisplacementContext(cfg.displacement_rank_window,
                                             cfg.displacement_rank_min_samples)
    rejected_sizing_log: list[dict] = []
    trend_events_log: list[dict] = []
    major_bos_count = major_choch_count = 0
    major_swing_highs = major_swing_lows = 0
    last_swing_high_index: int | None = None
    last_swing_low_index: int | None = None
    for row in frame.itertuples(index=False):
        candle = Candle(row.time.isoformat(), float(row.open), float(row.high),
                        float(row.low), float(row.close))
        index = len(engine.candles)
        broker.process_candle(candle, index, spread=spread,
                              execution_source="historical_ohlc")
        for rejection in broker.rejected_sizing:
            rejected_sizing_log.append({**rejection, "time": rejection.get("time", candle.time)})
        broker.rejected_sizing.clear()
        for event in broker.trend_events:
            trend_events_log.append({**event, "time": event.get("time", candle.time)})
        broker.trend_events.clear()
        m15_bar = m15.process_m5(candle)
        liq_m5.process(candle)
        if m15_bar:
            liq_m15.process(m15_bar)
        breaks, formed, invalidated = engine.process(candle)
        choch.process(breaks)
        # Apply the fresh M5 trend -- cancelling any now-opposing pending
        # order -- before any of this candle's own newly formed OBs are
        # turned into an order. New OBs always form in lockstep with
        # engine.trend (see PineSwingOBEngine.process), so without this the
        # trend check inside add_ob would compare a brand-new OB against the
        # *previous* candle's trend and wrongly reject it.
        broker.update_m5_trend(trend_from_engine_state(engine.trend), candle.time)
        # Entry Pivot runs right after the trend update and before setups are
        # built, mirroring PaperApp.on_closed_candle exactly.
        for confirmed_pivot in entry_pivot.process(candle):
            broker.update_entry_pivot(confirmed_pivot)
            trend_events_log.append({"event_type": "entry_pivot_confirmed",
                                     "kind": confirmed_pivot.kind, "price": confirmed_pivot.price,
                                     "candle_index": confirmed_pivot.candle_index,
                                     "pivot_time": confirmed_pivot.pivot_time,
                                     "confirmed_at": confirmed_pivot.confirmed_at,
                                     "time": confirmed_pivot.confirmed_at})
        for item in breaks:
            if item.kind == "BOS":
                major_bos_count += 1
            else:
                major_choch_count += 1
        if engine.swing_high is not None and engine.swing_high.bar_index != last_swing_high_index:
            last_swing_high_index = engine.swing_high.bar_index
            major_swing_highs += 1
        if engine.swing_low is not None and engine.swing_low.bar_index != last_swing_low_index:
            last_swing_low_index = engine.swing_low.bar_index
            major_swing_lows += 1
        for ob in formed:
            displacement_data = displacement_snapshot(candle, ob, breaks)
            ranker = displacement if ob.break_kind == "BOS" else choch_displacement
            displacement_rank = ranker.observe(displacement_data.get("bos_close_through_atr"))
            pivot = engine.swing_low if ob.direction == "bull" else engine.swing_high
            broker.add_ob(ob, {**m15.snapshot(ob.direction, candle.close),
                               **liq_m5.snapshot(ob.direction, candle.close, "liq_m5"),
                               **liq_m15.snapshot(ob.direction, candle.close, "liq_m15"),
                               **liq_m5.setup_snapshot(ob.direction,
                                                       pivot.bar_index if pivot else None,
                                                       ob.formed_index),
                               **choch.snapshot(ob.direction, index),
                               **displacement_data, **displacement_rank,
                               "formation_open": candle.open, "formation_high": candle.high,
                               "formation_low": candle.low, "formation_close": candle.close,
                               "major_swing_high": engine.swing_high.level if engine.swing_high else None,
                               "major_swing_low": engine.swing_low.level if engine.swing_low else None})
        for ob_id in invalidated:
            broker.cancel_ob(ob_id)
        broker.set_market_context({direction: {**m15.snapshot(direction, candle.close),
                                               **liq_m5.snapshot(direction, candle.close, "liq_m5"),
                                               **liq_m15.snapshot(direction, candle.close, "liq_m15"),
                                               **choch.snapshot(direction, index)}
                                   for direction in ("bull", "bear")})

    root = cfg.db_path.parent
    raw_label = csv_path.stem + (f"_{run_label}" if run_label else "")
    label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in raw_label)
    store = StateStore(root / f"historical_{label}.sqlite3")
    store.save(engine.dump_state(), broker.dump_state(),
               {"m15": m15.dump_state(), "liquidity_m5": liq_m5.dump_state(),
                "liquidity_m15": liq_m15.dump_state(), "choch": choch.dump_state(),
                "displacement": displacement.dump_state(),
                "choch_displacement": choch_displacement.dump_state(),
                "entry_pivot": entry_pivot.dump_state()})
    for trade in broker.trades:
        store.record_trade(trade)
    for rejection in rejected_sizing_log:
        store.record_event(rejection["time"], "order_rejected_volume", rejection)
    for event in trend_events_log:
        store.record_event(event["time"], event.get("event_type", "order_rejected_m5_trend"), event)
    store.close()
    summary_path = root / f"historical_{label}_summary.csv"
    summary = export_reports(broker, root / f"historical_{label}_trades.csv", summary_path)
    export_breakdown(broker, root / f"historical_{label}_breakdown.csv")
    filled_ids = {x.order_id for x in broker.trades}
    filled_ids.update(x.order_id for x in broker.positions)
    average_r = summary["total_r"] / summary["trades"] if summary["trades"] else 0.0
    summary.update({"source": str(csv_path), "bars": len(frame), "spread": spread,
                    "swing_length": cfg.swing_length,
                    "trend_swing_length": cfg.trend_swing_length,
                    "entry_pivot_left": cfg.entry_pivot_left,
                    "entry_pivot_right": cfg.entry_pivot_right,
                    "orders_created": len(broker.pending),
                    "orders_filled": len(filled_ids),
                    "orders_active_unfilled": sum(x.active for x in broker.pending),
                    "allowed_break_kinds": "+".join(cfg.allowed_break_kinds),
                    "strong_choch_only": cfg.strong_choch_only,
                    "target_mode": cfg.target_mode,
                    "min_entry_wait_bars": cfg.min_entry_wait_bars,
                    "entry_mode": cfg.entry_mode,
                    "sweep_reclaim_atr_buffer": cfg.sweep_reclaim_atr_buffer,
                    "entry_lifecycle_enabled": cfg.entry_lifecycle_enabled,
                    "max_open_positions": cfg.max_open_positions,
                    "liquidity_risk_sizing_enabled": cfg.liquidity_risk_sizing_enabled,
                    "choch_risk_sizing_enabled": cfg.choch_risk_sizing_enabled,
                    "combined_context_risk_sizing_enabled": cfg.combined_context_risk_sizing_enabled,
                    "displacement_risk_sizing_enabled": cfg.displacement_risk_sizing_enabled,
                    "choch_displacement_risk_sizing_enabled": cfg.choch_displacement_risk_sizing_enabled,
                    "choch_risk_cap_fraction": cfg.choch_risk_cap_fraction,
                    "three_factor_risk_sizing_enabled": cfg.three_factor_risk_sizing_enabled,
                    # Swing/Pivot-specific summary metrics (spec item 22).
                    "major_swing_highs": major_swing_highs, "major_swing_lows": major_swing_lows,
                    "major_bos_count": major_bos_count, "major_choch_count": major_choch_count,
                    "entry_pivot_highs": broker.stats.get("pivot_highs_confirmed", 0),
                    "entry_pivot_lows": broker.stats.get("pivot_lows_confirmed", 0),
                    "buy_setups": broker.stats.get("buy_setups_created", 0),
                    "sell_setups": broker.stats.get("sell_setups_created", 0),
                    "setups_rejected_trend_mismatch": broker.stats.get("rejected_trend_mismatch", 0),
                    "setups_rejected_neutral_trend": broker.stats.get("rejected_trend_neutral", 0),
                    "setups_rejected_no_entry_pivot": broker.stats.get("rejected_no_entry_pivot", 0),
                    "pending_orders_created": len(broker.pending),
                    "pending_orders_cancelled_trend_change": broker.stats.get("cancelled_trend_change", 0),
                    "filled_orders": len(filled_ids), "closed_trades": len(broker.trades),
                    "average_r": average_r, "max_drawdown": broker.max_drawdown,
                    **broker.stats})
    age_samples = broker.stats.get("entry_pivot_age_samples", 0)
    summary["avg_entry_pivot_age_bars"] = (
        round(broker.stats.get("entry_pivot_age_sum_bars", 0) / age_samples, 2)
        if age_samples else None)
    summary["max_entry_pivot_age_seen"] = (
        broker.stats.get("entry_pivot_age_max_bars", 0) if age_samples else None)
    export_html(broker, root / f"historical_{label}_report.html",
                f"Pine Swing-OB Backtest — {csv_path.name}",
                {"source": csv_path, "bars": len(frame), "spread": spread,
                 "timeframe": cfg.timeframe,
                 "trend swing": cfg.trend_swing_length,
                 "entry pivot": f"{cfg.entry_pivot_left}/{cfg.entry_pivot_right}",
                 "RR": cfg.rr, "risk": f"{cfg.risk_fraction * 100:.1f}%",
                 "breaks": "+".join(cfg.allowed_break_kinds),
                 "target mode": cfg.target_mode,
                 "minimum entry wait": f"{cfg.min_entry_wait_bars} bars",
                 "entry mode": cfg.entry_mode,
                 "sweep ATR buffer": cfg.sweep_reclaim_atr_buffer,
                 "lifecycle": "event-driven" if cfg.entry_lifecycle_enabled else "off",
                 "max positions": cfg.max_open_positions})
    import csv
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary)); writer.writeheader(); writer.writerow(summary)
    return summary
