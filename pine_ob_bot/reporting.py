from __future__ import annotations

import csv
import html
import math
import copy
from dataclasses import asdict
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from .paper import PaperBroker


def export_daily_html(broker: PaperBroker, day: date, timezone_name: str,
                      path: Path, context: dict | None = None,
                      refresh_seconds: int | None = None) -> None:
    """Render the standard dashboard for trades closed on one local day."""
    import pandas as pd
    zone = ZoneInfo(timezone_name)
    before = []
    selected = []
    for trade in broker.trades:
        stamp = pd.Timestamp(trade.closed_time)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        local_day = stamp.tz_convert(zone).date()
        if local_day < day:
            before.append(trade)
        elif local_day == day:
            selected.append(trade)
    view = copy.copy(broker)
    view.trades = selected
    view.initial_equity = broker.initial_equity + sum(item.pnl for item in before)
    view.equity = view.initial_equity + sum(item.pnl for item in selected)
    peak = view.initial_equity
    equity = view.initial_equity
    max_dd = 0.0
    for trade in selected:
        equity += trade.pnl
        peak = max(peak, equity)
        if peak:
            max_dd = max(max_dd, (peak - equity) / peak)
    view.peak_equity = peak
    view.max_drawdown = max_dd
    details = {"report day": day.isoformat(), "timezone": timezone_name,
               **(context or {})}
    export_html(view, path, f"Pine Swing-OB Daily Trading Report — {day.isoformat()}",
                details, refresh_seconds=refresh_seconds)


def export_daily_metrics(broker: PaperBroker, day: date, timezone_name: str,
                         path: Path, extra: dict | None = None) -> dict:
    """Upsert one machine-readable row per local day for forward-test analysis."""
    import pandas as pd
    zone = ZoneInfo(timezone_name)
    trades = []
    for trade in broker.trades:
        stamp = pd.Timestamp(trade.closed_time)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        if stamp.tz_convert(zone).date() == day:
            trades.append(trade)
    wins = [item for item in trades if item.pnl > 0]
    losses = [item for item in trades if item.pnl <= 0]
    gross_win, gross_loss = sum(item.pnl for item in wins), abs(sum(item.pnl for item in losses))
    risks = [float(item.meta["applied_risk_fraction"]) for item in trades
             if item.meta.get("applied_risk_fraction") is not None]
    spreads = [float(item.meta["entry_execution_spread"]) for item in trades
               if item.meta.get("entry_execution_spread") is not None]
    slippage = [float(item.meta["demo_entry_slippage"]) for item in trades
                if item.meta.get("demo_entry_slippage") is not None]
    row = {
        "date": day.isoformat(), "timezone": timezone_name, "trades": len(trades),
        "wins": len(wins), "win_rate": len(wins) / len(trades) if trades else 0.0,
        "net_pnl": sum(item.pnl for item in trades),
        "total_r": sum(item.r_multiple for item in trades),
        "profit_factor": gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0),
        "bos_trades": sum(item.meta.get("break_kind") == "BOS" for item in trades),
        "choch_trades": sum(item.meta.get("break_kind") == "CHoCH" for item in trades),
        **{f"score_{score}": sum(item.meta.get("context_risk_score") == score for item in trades)
           for score in range(4)},
        "opposite_choch_fill": sum(item.meta.get("fill_m5_last_choch_alignment") == "opposite"
                                   for item in trades),
        "strong_displacement": sum(bool(item.meta.get("bos_displacement_top_quartile"))
                                   for item in trades),
        "setup_liquidity_sweep": sum(bool(item.meta.get("setup_m5_opposite_sweep"))
                                     for item in trades),
        "m15_aligned": sum(item.meta.get("m15_alignment") == "aligned" for item in trades),
        "m15_counter": sum(item.meta.get("m15_alignment") == "counter" for item in trades),
        "avg_risk_pct": sum(risks) / len(risks) * 100 if risks else 0.0,
        "min_risk_pct": min(risks) * 100 if risks else 0.0,
        "max_risk_pct": max(risks) * 100 if risks else 0.0,
        "tick_entries": sum(item.meta.get("entry_execution_source") == "tick" for item in trades),
        "replay_entries": sum(item.meta.get("entry_execution_source") != "tick" for item in trades),
        "avg_entry_spread": sum(spreads) / len(spreads) if spreads else 0.0,
        "demo_orders": sum(bool(item.meta.get("demo_order_sent")) for item in trades),
        "avg_demo_entry_slippage": sum(slippage) / len(slippage) if slippage else 0.0,
        **(extra or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if path.exists() and path.stat().st_size:
        with path.open(newline="", encoding="utf-8") as handle:
            existing = [item for item in csv.DictReader(handle) if item.get("date") != row["date"]]
    existing.append(row)
    fields = list(dict.fromkeys(key for item in existing for key in item))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(sorted(existing, key=lambda item: item["date"]))
    return row


def export_reports(broker: PaperBroker, trades_path: Path, summary_path: Path) -> dict:
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for trade in broker.trades:
        row = asdict(trade)
        row.update(row.pop("meta", {}))
        rows.append(row)
    fields = list(dict.fromkeys(key for row in rows for key in row)) if rows else [
        "id", "direction", "entry", "stop", "target", "exit_price", "volume",
        "risk_money", "pnl", "r_multiple", "opened_time", "closed_time", "result"]
    with trades_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); writer.writeheader()
        writer.writerows(rows)
    wins = [x for x in broker.trades if x.pnl > 0]
    losses = [x for x in broker.trades if x.pnl <= 0]
    gross_win, gross_loss = sum(x.pnl for x in wins), abs(sum(x.pnl for x in losses))
    summary = {"trades": len(broker.trades), "wins": len(wins),
               "win_rate": len(wins) / len(broker.trades) if broker.trades else 0.0,
               "net_pnl": sum(x.pnl for x in broker.trades),
               "total_r": sum(x.r_multiple for x in broker.trades),
               "profit_factor": gross_win / gross_loss if gross_loss else (float("inf") if gross_win else 0.0),
               "max_drawdown_pct": broker.max_drawdown * 100, "equity": broker.equity}
    with summary_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(summary)); writer.writeheader(); writer.writerow(summary)
    return summary


def experimental_summary(broker: PaperBroker) -> dict:
    """Config echo + derived averages for the flag-gated experiments.

    Merged into both backtest runners' summaries so every run records the
    full experimental configuration (spec: the report must contain it) and
    the spread-wait averages. All values are well-defined with the flags
    off (config echoes read False/None, averages read None).
    """
    cfg = broker.cfg
    stats = broker.stats
    started = stats.get("spread_wait_started", 0)
    filled = stats.get("spread_wait_eventually_filled", 0)
    open_risk = sum(item.risk_money for item in broker.positions)
    return {
        "controlled_revival": cfg.controlled_revival,
        "revival_max_return_bars": cfg.revival_max_return_bars if cfg.controlled_revival else None,
        "revival_require_fresh_sweep": cfg.revival_require_fresh_sweep if cfg.controlled_revival else None,
        "revival_max_per_ob": cfg.revival_max_per_ob if cfg.controlled_revival else None,
        "allow_ob_reentry": cfg.allow_ob_reentry,
        "max_entries_per_ob": cfg.max_entries_per_ob if cfg.allow_ob_reentry else None,
        "min_reentry_wait_bars": cfg.min_reentry_wait_bars if cfg.allow_ob_reentry else None,
        "reentry_require_fresh_sweep": cfg.reentry_require_fresh_sweep if cfg.allow_ob_reentry else None,
        "reentry_after_loss_only": cfg.reentry_after_loss_only if cfg.allow_ob_reentry else None,
        "wait_for_spread_after_confirmation": cfg.wait_for_spread_after_confirmation,
        "spread_wait_max_seconds": cfg.spread_wait_max_seconds,
        "spread_wait_max_ticks": cfg.spread_wait_max_ticks,
        "spread_wait_max_bars": cfg.spread_wait_max_bars,
        "portfolio_risk_cap": cfg.portfolio_risk_cap,
        "sweep_reclaim_max_bars": cfg.sweep_reclaim_max_bars,
        "avg_spread_wait_seconds": (
            round(stats.get("spread_wait_duration_seconds_sum", 0.0) / filled, 3)
            if filled else None),
        "avg_spread_at_confirmation": (
            round(stats.get("spread_at_confirmation_sum", 0.0) / started, 5)
            if started else None),
        "avg_spread_at_fill": (
            round(stats.get("spread_at_fill_sum", 0.0) / filled, 5)
            if filled else None),
        "current_open_risk_fraction": (
            round(open_risk / broker.equity, 6) if broker.equity else None),
        **_m1_summary(cfg, stats),
    }


def _m1_summary(cfg, stats: dict) -> dict:
    """Computed M1 Entry Assist summaries (None-safe with the flag off)."""
    m1_trades = stats.get("m1_wins", 0) + stats.get("m1_losses", 0)
    confirmations = stats.get("m1_confirmations_total", 0)
    early = stats.get("m1_confirmations_later_confirmed_by_m5", 0)
    unique = stats.get("m1_confirmations_unique_vs_m5", 0)
    classified = early + unique
    gross_loss = stats.get("m1_gross_loss_r", 0.0)
    return {
        "m1_entry_assist": cfg.m1_entry_assist,
        "m1_assist_mode": cfg.m1_assist_mode if cfg.m1_entry_assist else None,
        "m1_win_rate": (round(stats.get("m1_wins", 0) / m1_trades, 4)
                        if m1_trades else None),
        "m1_profit_factor": (round(stats.get("m1_gross_win_r", 0.0) / gross_loss, 3)
                             if gross_loss else None),
        "m1_average_r": (round(stats.get("m1_total_r", 0.0) / m1_trades, 4)
                         if m1_trades else None),
        "m1_avg_reclaim_lag_bars": (
            round(stats.get("m1_reclaim_lag_sum_bars", 0) / confirmations, 3)
            if confirmations else None),
        "m1_avg_lead_minutes": (
            round(stats.get("m1_lead_minutes_sum", 0.0) / early, 3)
            if early else None),
        "m1_unique_confirmation_rate": (round(unique / classified, 4)
                                        if classified else None),
        "m1_fill_conversion_rate": (
            round(stats.get("m1_confirmations_filled", 0) / confirmations, 4)
            if confirmations else None),
        "m1_avg_setup_to_confirm_minutes": (
            round(stats.get("m1_setup_to_confirm_minutes_sum", 0.0) / confirmations, 3)
            if confirmations else None),
        "m1_max_setup_to_confirm_minutes": stats.get("m1_max_setup_to_confirm_minutes", 0.0),
        "m1_rejected_lead_too_long": stats.get("m1_rejected_lead_too_long", 0),
    }


def export_breakdown(broker: PaperBroker, path: Path) -> None:
    """Write long-form diagnostic groups, convenient for Excel/pandas filters."""
    import pandas as pd
    rows = []
    for trade in broker.trades:
        item = asdict(trade)
        item.update(trade.meta)
        ts = pd.Timestamp(trade.opened_time)
        hour = ts.hour
        item.update({"entry_hour": f"{hour:02d}:00", "weekday": ts.day_name(),
                     "month": ts.strftime("%Y-%m"),
                     "session_utc": "Asia" if hour < 8 else "London" if hour < 13
                     else "NewYork" if hour < 21 else "OffHours",
                     "ob_width_atr_bucket": _bucket(item.get("ob_width_atr"),
                                                     [(0.5, "<0.5"), (1.0, "0.5-1"),
                                                      (2.0, "1-2"), (math.inf, "2+")]),
                     "wait_bars_bucket": _bucket(item.get("wait_bars"),
                                                  [(3, "0-2"), (7, "3-6"),
                                                   (13, "7-12"), (math.inf, "13+")]),
                     "sweep_lag_bucket": _bucket(item.get("sweep_reclaim_lag_bars"),
                                                  [(1, "0"), (2, "1"),
                                                   (math.inf, "2+")]),
                     "entry_attempt": item.get("ob_entry_attempt"),
                     "m1_lag_bucket": _bucket(item.get("m1_reclaim_lag_bars"),
                                              [(1, "0"), (2, "1"),
                                               (math.inf, "2+")]),
                     "m1_lead_bucket": _bucket(item.get("m1_lead_minutes"),
                                               [(1, "<1m"), (3, "1-3m"),
                                                (5, "3-5m"), (math.inf, "5m+")]),
                     "m1_setup_to_confirm_bucket": _bucket(
                         item.get("m1_setup_to_confirm_minutes"),
                         [(15, "<15m"), (30, "15-30m"),
                          (60, "30-60m"), (math.inf, "60m+")]),
                     "entry_spread_bucket": _bucket(item.get("entry_execution_spread"),
                                                     [(0.1, "<0.10"), (0.2, "0.10-0.20"),
                                                      (0.4, "0.20-0.40"),
                                                      (math.inf, "0.40+")])})
        rows.append(item)
    fields = ["direction", "break_kind", "session_utc", "entry_hour", "weekday",
              "month", "ob_width_atr_bucket", "wait_bars_bucket", "m15_trend",
              "m15_alignment", "m15_last_break_kind", "m15_last_break_direction",
              "m15_range_zone", "m15_active_ob_direction",
              "liq_m5_opposite_last_event", "liq_m5_opposite_sweep_seen",
              "liq_m15_opposite_last_event", "liq_m15_opposite_sweep_seen",
              "fill_liq_m5_opposite_last_event", "fill_liq_m15_opposite_last_event"]
    fields.extend(["setup_m5_opposite_sweep", "m5_last_choch_direction",
                   "m5_last_choch_alignment", "fill_m5_last_choch_direction",
                   "fill_m5_last_choch_alignment", "context_risk_score"])
    fields.extend(["target_source", "bos_directional_body",
                   "bos_displacement_top_quartile", "entry_age_top_quartile",
                   "breakeven_armed", "breakeven_exit"])
    # Experimental trade-frequency dimensions: normal vs revival vs re-entry,
    # first entry vs re-entry attempt, sweep lag 0/1/2+, spread waited vs
    # immediate. Groups only materialize when the columns exist in the data.
    fields.extend(["setup_model", "is_revival", "is_reentry", "entry_attempt",
                   "sweep_lag_bucket", "spread_waited"])
    # M1 Entry Assist dimensions: M5 vs M1 entries, unique-vs-early M1,
    # M1 reclaim lag, M1 lead time, sequence status and spread buckets.
    fields.extend(["entry_timeframe", "entry_trigger", "m1_unique_vs_m5",
                   "m1_lag_bucket", "m1_lead_bucket", "m1_setup_to_confirm_bucket",
                   "m1_sequence_status", "entry_spread_bucket"])
    output = []
    df = pd.DataFrame(rows)
    if not df.empty:
        for source in ("liq_m5_opposite_event_age_bars", "liq_m5_opposite_sweep_depth_atr",
                       "liq_m15_opposite_event_age_bars", "liq_m15_opposite_sweep_depth_atr",
                       "m5_last_choch_age_bars", "m5_same_choch_age_bars",
                       "m5_opposite_choch_age_bars", "fill_m5_last_choch_age_bars"):
            if source in df and df[source].notna().sum() >= 4:
                target = source + "_quantile"
                df[target] = pd.qcut(df[source], q=4, duplicates="drop").astype(str)
                fields.append(target)
        for source in ("bos_body_atr", "bos_range_atr", "bos_body_to_range",
                       "bos_close_through_atr", "bos_close_location"):
            if source in df and df[source].notna().sum() >= 4:
                target = source + "_quantile"
                df[target] = pd.qcut(df[source], q=4, duplicates="drop").astype(str)
                fields.append(target)
        for dimension in fields:
            if dimension not in df:
                continue
            for group, part in df.groupby(dimension, dropna=False):
                wins = part[part["pnl"] > 0]
                losses = part[part["pnl"] <= 0]
                gross_win = float(wins["pnl"].sum())
                gross_loss = abs(float(losses["pnl"].sum()))
                output.append({"dimension": dimension, "group": group,
                               "trades": len(part), "wins": len(wins),
                               "win_rate": len(wins) / len(part),
                               "net_pnl": float(part["pnl"].sum()),
                               "total_r": float(part["r_multiple"].sum()),
                               "avg_r": float(part["r_multiple"].mean()),
                               "profit_factor": gross_win / gross_loss if gross_loss else float("inf"),
                               "avg_mae_r": float(part["max_adverse_r"].mean()),
                               "avg_mfe_r": float(part["max_favorable_r"].mean())})
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["dimension", "group", "trades", "wins", "win_rate", "net_pnl",
               "total_r", "avg_r", "profit_factor", "avg_mae_r", "avg_mfe_r"]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns); writer.writeheader(); writer.writerows(output)


def export_html(broker: PaperBroker, path: Path, title: str,
                context: dict | None = None, refresh_seconds: int | None = None) -> None:
    """Create a dependency-free, self-contained analysis dashboard."""
    import pandas as pd
    context = context or {}
    trades = pd.DataFrame([asdict(x) for x in broker.trades])
    if not trades.empty:
        meta = pd.json_normalize(trades.pop("meta"))
        trades = pd.concat([trades, meta], axis=1)
        trades["equity"] = broker.initial_equity + trades["pnl"].cumsum()
    wins = int((trades["pnl"] > 0).sum()) if not trades.empty else 0
    gross_win = float(trades.loc[trades.pnl > 0, "pnl"].sum()) if not trades.empty else 0
    gross_loss = abs(float(trades.loc[trades.pnl <= 0, "pnl"].sum())) if not trades.empty else 0
    kpis = {
        "Trades": len(trades), "Win rate": f"{wins / len(trades) * 100:.2f}%" if len(trades) else "0%",
        "Net PnL": f"${broker.equity - broker.initial_equity:,.2f}",
        "Total R": f"{trades.r_multiple.sum():.2f}" if not trades.empty else "0",
        "Profit factor": f"{gross_win / gross_loss:.2f}" if gross_loss else "—",
        "Max drawdown": f"{broker.max_drawdown * 100:.2f}%", "Equity": f"${broker.equity:,.2f}",
    }
    cards = "".join(f'<div class="card"><span>{html.escape(k)}</span><b>{html.escape(str(v))}</b></div>'
                    for k, v in kpis.items())
    context_html = " · ".join(f"<b>{html.escape(str(k))}</b>: {html.escape(str(v))}"
                               for k, v in context.items())
    chart = _equity_svg(trades["equity"].tolist() if not trades.empty else [broker.initial_equity])
    sections = []
    if not trades.empty:
        # Tick-sourced open times mix fractional and whole seconds; inferring
        # the format from the first row makes pandas reject the other kind.
        ts = pd.to_datetime(trades["opened_time"], utc=True, format="ISO8601")
        trades["session"] = ts.dt.hour.map(lambda h: "Asia" if h < 8 else "London" if h < 13 else "NewYork" if h < 21 else "OffHours")
        trades["weekday"] = ts.dt.day_name()
        trades["month"] = ts.dt.strftime("%Y-%m")
        trades["hour"] = ts.dt.strftime("%H:00")
        trades["width/ATR"] = trades.get("ob_width_atr", pd.Series([None] * len(trades)))
        trades["wait bars"] = trades.get("wait_bars", pd.Series([None] * len(trades)))
        trades["risk tier"] = trades.get(
            "applied_risk_fraction", pd.Series([None] * len(trades))).map(
            lambda value: "unknown" if pd.isna(value) else f"{float(value) * 100:.2f}%")
        for source in ("liq_m5_opposite_event_age_bars", "liq_m5_opposite_sweep_depth_atr",
                       "liq_m15_opposite_event_age_bars", "liq_m15_opposite_sweep_depth_atr",
                       "m5_last_choch_age_bars", "fill_m5_last_choch_age_bars"):
            if source in trades and trades[source].notna().sum() >= 4:
                trades[source + "_quantile"] = pd.qcut(trades[source], q=4, duplicates="drop").astype(str)
        for source in ("bos_body_atr", "bos_range_atr", "bos_body_to_range",
                       "bos_close_through_atr", "bos_close_location"):
            if source in trades and trades[source].notna().sum() >= 4:
                trades[source + "_quantile"] = pd.qcut(
                    trades[source], q=4, duplicates="drop").astype(str)
        for label, column in (("Direction", "direction"), ("BOS vs CHoCH", "break_kind"),
                              ("Sessions (UTC)", "session"), ("Weekdays", "weekday"),
                              ("Months", "month"), ("Entry hours (UTC)", "hour"),
                              ("M15 trend", "m15_trend"), ("M5/M15 alignment", "m15_alignment"),
                              ("M15 last break", "m15_last_break_kind"),
                              ("M15 range zone", "m15_range_zone"),
                              ("M5 opposite liquidity event", "liq_m5_opposite_last_event"),
                              ("M5 opposite sweep", "liq_m5_opposite_sweep_seen"),
                              ("M15 opposite liquidity event", "liq_m15_opposite_last_event"),
                              ("M15 opposite sweep", "liq_m15_opposite_sweep_seen"),
                              ("Setup-related M5 sweep", "setup_m5_opposite_sweep"),
                              ("M5 last CHoCH direction", "m5_last_choch_direction"),
                              ("M5 CHoCH alignment at setup", "m5_last_choch_alignment"),
                              ("M5 CHoCH alignment at fill", "fill_m5_last_choch_alignment"),
                              ("Combined context risk score", "context_risk_score"),
                              ("Applied risk", "risk tier"),
                              ("Entry execution source", "entry_execution_source"),
                              ("Exit execution source", "exit_execution_source"),
                              ("Demo order mirrored", "demo_order_sent"),
                              ("Target source", "target_source"),
                              ("Breakeven armed", "breakeven_armed"),
                              ("Breakeven exit", "breakeven_exit"),
                              ("Setup model", "setup_model"),
                              ("OB entry attempt", "ob_entry_attempt"),
                              ("Sweep reclaim lag (bars)", "sweep_reclaim_lag_bars"),
                              ("Spread waited", "spread_waited"),
                              ("BOS directional body", "bos_directional_body"),
                              ("Adaptive BOS displacement", "bos_displacement_top_quartile"),
                              ("Adaptive OB age", "entry_age_top_quartile"),
                              ("BOS body / ATR", "bos_body_atr_quantile"),
                              ("BOS range / ATR", "bos_range_atr_quantile"),
                              ("BOS body / range", "bos_body_to_range_quantile"),
                              ("BOS close-through / ATR", "bos_close_through_atr_quantile"),
                              ("BOS close location", "bos_close_location_quantile"),
                              ("M5 CHoCH age at setup", "m5_last_choch_age_bars_quantile"),
                              ("M5 CHoCH age at fill", "fill_m5_last_choch_age_bars_quantile"),
                              ("M5 sweep age quantiles", "liq_m5_opposite_event_age_bars_quantile"),
                              ("M5 sweep depth quantiles", "liq_m5_opposite_sweep_depth_atr_quantile")):
            if column not in trades:
                continue
            sections.append(f'<section><h2>{label}</h2><div class="table-scroll">{_html_group(trades, column)}</div></section>')
        cols = [x for x in ["opened_time", "direction", "break_kind", "entry", "stop", "target",
                            "result", "pnl", "r_multiple", "max_adverse_r", "max_favorable_r",
                            "width/ATR", "wait bars", "m15_trend", "m15_alignment",
                            "m15_last_break_kind", "m15_break_age_bars", "m15_range_zone",
                            "context_risk_score", "risk tier", "bos_displacement_top_quartile",
                            "fill_m5_last_choch_alignment", "setup_m5_opposite_sweep",
                            "breakeven_armed", "breakeven_exit",
                            "original_stop", "final_stop",
                            "setup_model", "is_revival", "revival_attempt",
                            "is_reentry", "ob_entry_attempt",
                            "sweep_reclaim_lag_bars", "spread_waited",
                            "spread_wait_seconds", "open_risk_before_entry",
                            "projected_open_risk_fraction",
                            "entry_timeframe", "entry_trigger",
                            "m1_reclaim_lag_bars", "m1_lead_minutes",
                            "m1_setup_to_confirm_minutes", "m1_risk_multiplier",
                            "m1_unique_vs_m5", "m1_sequence_status",
                            "entry_execution_source", "entry_execution_spread",
                            "demo_order_sent", "demo_position_ticket", "demo_open_price",
                            "demo_entry_slippage", "demo_close_price", "demo_exit_slippage"] if x in trades]
        recent = trades[cols].tail(200).iloc[::-1]
        sections.append('<section class="wide"><h2>Latest trades (max 200)</h2><div class="table-scroll">' +
                        recent.to_html(index=False, classes="data", border=0, float_format=lambda x: f"{x:.3f}") +
                        "</div></section>")
    css = """
    :root{color-scheme:dark;font-family:Inter,Segoe UI,Arial;background:#0b1020;color:#e7ebf5}
    body{max-width:1500px;margin:auto;padding:28px}.muted{color:#93a0ba}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:22px 0}
    .card,section{background:#121a2e;border:1px solid #23304d;border-radius:12px;padding:16px}.card span{display:block;color:#93a0ba;font-size:13px}.card b{font-size:22px}
    .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.wide{grid-column:1/-1}h1,h2{margin-top:0}h2{font-size:17px}
    .table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
    table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px;border-bottom:1px solid #26324c;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}th{color:#9fb0ce;position:sticky;top:0;background:#121a2e}
    tbody tr:nth-child(even){background:#0f1729}tbody tr:hover{background:#16233d}
    svg{width:100%;height:260px}.line{fill:none;stroke:#49d39d;stroke-width:2}.area{fill:#49d39d22}.axis{stroke:#394766;stroke-width:1}.chart-label{fill:#93a0ba;font-size:11px;font-family:Inter,Segoe UI,Arial}
    @media(max-width:850px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}body{padding:12px}.cards{grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}}
    """
    refresh = (f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
               if refresh_seconds else "")
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{refresh}<title>{html.escape(title)}</title><style>{css}</style></head>
    <body><h1>{html.escape(title)}</h1><div class="muted">{context_html}</div><div class="cards">{cards}</div>
    <section><h2>Equity curve</h2>{chart}</section><div class="grid">{''.join(sections)}</div></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")


def _html_group(df, column: str) -> str:
    import pandas as pd
    rows = []
    for key, part in df.groupby(column, dropna=False):
        win = part[part.pnl > 0]; loss = part[part.pnl <= 0]
        gp, gl = float(win.pnl.sum()), abs(float(loss.pnl.sum()))
        rows.append({"Group": key, "Trades": len(part), "Win %": len(win) / len(part) * 100,
                     "Net PnL": float(part.pnl.sum()), "Total R": float(part.r_multiple.sum()),
                     "Avg R": float(part.r_multiple.mean()), "PF": gp / gl if gl else float("inf")})
    return pd.DataFrame(rows).sort_values("Avg R", ascending=False).to_html(
        index=False, classes="data", border=0, float_format=lambda x: f"{x:.2f}")


def _equity_svg(values: list[float]) -> str:
    width, height, pad = 1200, 260, 16
    lo, hi = min(values), max(values)
    span = hi - lo or 1
    points = []
    for i, value in enumerate(values):
        x = pad + i / max(len(values) - 1, 1) * (width - 2 * pad)
        y = pad + (hi - value) / span * (height - 2 * pad)
        points.append(f"{x:.1f},{y:.1f}")
    line = " ".join(points)
    area = f"{pad},{height-pad} {line} {width-pad},{height-pad}"
    hi_label = f'<text class="chart-label" x="{pad}" y="{pad + 10}">${hi:,.2f}</text>'
    lo_label = f'<text class="chart-label" x="{pad}" y="{height - pad - 4}">${lo:,.2f}</text>'
    last_label = (f'<text class="chart-label" x="{width - pad}" y="{pad + 10}" text-anchor="end">'
                  f'${values[-1]:,.2f}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Equity curve">'
           f'<line class="axis" x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}"/>'
           f'<polygon class="area" points="{area}"/><polyline class="line" points="{line}"/>'
           f'{hi_label}{lo_label}{last_label}</svg>')


def _bucket(value, limits):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "unknown"
    for limit, label in limits:
        if float(value) < limit:
            return label
    return "unknown"
