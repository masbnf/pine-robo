#!/usr/bin/env python3
"""Compare risk models in one streaming pass over one or more tick months."""
from __future__ import annotations

import argparse
import copy
import csv
import math
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pine_ob_bot.config import BotConfig
from pine_ob_bot.liquidity_context import LiquidityTracker
from pine_ob_bot.mtf_context import M15Context
from pine_ob_bot.paper import PaperBroker, SymbolSpec
from pine_ob_bot.pine_engine import PineSwingOBEngine
from pine_ob_bot.reporting import export_breakdown, export_html, export_reports
from pine_ob_bot.structure_context import ChochContext, DisplacementContext, displacement_snapshot
from pine_ob_bot.tick_historical import M5Builder, iter_ticks


def profile_config(name: str) -> BotConfig:
    common = dict(swing_length=12, rr=1.5, allowed_break_kinds=("BOS", "CHoCH"),
                  choch_displacement_risk_sizing_enabled=True)
    if name == "01_baseline":
        return BotConfig(**common, choch_risk_cap_fraction=.005)
    if name == "02_choch_split":
        return BotConfig(**common, choch_risk_cap_fraction=None,
                         choch_split_risk_enabled=True)
    if name == "03_bos_three_factor":
        return BotConfig(**common, choch_risk_cap_fraction=.005,
                         bos_three_factor_risk_enabled=True)
    if name == "04_combined":
        return BotConfig(**common, choch_risk_cap_fraction=None,
                         choch_split_risk_enabled=True,
                         bos_three_factor_risk_enabled=True)
    if name == "05_combined_m15_bonus":
        return BotConfig(**common, choch_risk_cap_fraction=None,
                         choch_split_risk_enabled=True,
                         bos_three_factor_risk_enabled=True,
                         bos_m15_counter_bonus_fraction=.001)
    raise ValueError(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", nargs="+", required=True, metavar="YYYY-MM")
    parser.add_argument("--data-root", type=Path,
                        default=Path("pine_ob_bot_data/historical_ticks"))
    parser.add_argument("--out", type=Path,
                        default=Path("pine_ob_bot_data/tick_backtests/risk_comparison_2026_03_05"))
    parser.add_argument("--initial-equity", type=float, default=10_000)
    parser.add_argument("--warmup-bars", type=int, default=500)
    args = parser.parse_args()

    paths = []
    for month in args.months:
        month_paths = sorted((args.data_root / month).glob("ticks_*.csv*"))
        if not month_paths:
            parser.error(f"no tick files found for {month}")
        paths.extend(month_paths)

    names = ["01_baseline", "02_choch_split", "03_bos_three_factor",
             "04_combined", "05_combined_m15_bonus"]
    configs = {name: profile_config(name) for name in names}
    signal_cfg = configs["01_baseline"]
    broker = PaperBroker(signal_cfg, args.initial_equity, SymbolSpec())
    engine = PineSwingOBEngine(signal_cfg)
    m15 = M15Context(signal_cfg)
    liq_m5 = LiquidityTracker(signal_cfg.swing_length, signal_cfg.atr_period)
    liq_m15 = LiquidityTracker(signal_cfg.m15_swing_length, signal_cfg.m15_atr_period)
    choch = ChochContext()
    displacement = DisplacementContext(signal_cfg.displacement_rank_window,
                                       signal_cfg.displacement_rank_min_samples)
    choch_displacement = DisplacementContext(signal_cfg.displacement_rank_window,
                                             signal_cfg.displacement_rank_min_samples)
    builder = M5Builder()
    ticks = 0
    trading_started = False

    def close_bar(candle) -> None:
        nonlocal trading_started
        index = len(engine.candles)
        broker.process_signal_candle(candle, index)
        m15_bar = m15.process_m5(candle)
        liq_m5.process(candle)
        if m15_bar:
            liq_m15.process(m15_bar)
        breaks, formed, invalidated = engine.process(candle)
        choch.process(breaks)
        for ob in formed:
            displacement_data = displacement_snapshot(candle, ob, breaks)
            ranker = displacement if ob.break_kind == "BOS" else choch_displacement
            rank = ranker.observe(displacement_data.get("bos_close_through_atr"))
            if index < args.warmup_bars:
                continue
            pivot = engine.swing_low if ob.direction == "bull" else engine.swing_high
            meta = {**m15.snapshot(ob.direction, candle.close),
                    **liq_m5.snapshot(ob.direction, candle.close, "liq_m5"),
                    **liq_m15.snapshot(ob.direction, candle.close, "liq_m15"),
                    **liq_m5.setup_snapshot(ob.direction,
                                            pivot.bar_index if pivot else None,
                                            ob.formed_index),
                    **choch.snapshot(ob.direction, index),
                    **displacement_data, **rank,
                    "formation_open": candle.open, "formation_high": candle.high,
                    "formation_low": candle.low, "formation_close": candle.close}
            broker.add_ob(ob, meta)
        for ob_id in invalidated:
            broker.cancel_ob(ob_id)
        context = {direction: {**m15.snapshot(direction, candle.close),
                               **liq_m5.snapshot(direction, candle.close, "liq_m5"),
                               **liq_m15.snapshot(direction, candle.close, "liq_m15"),
                               **choch.snapshot(direction, index)}
                   for direction in ("bull", "bear")}
        broker.set_market_context(context)
        if not trading_started and len(engine.candles) >= args.warmup_bars:
            broker.pending.clear()
            trading_started = True

    for tick in iter_ticks(paths):
        closed = builder.push(tick)
        if closed:
            close_bar(closed)
        spread = max(0.0, tick.ask - tick.bid)
        if trading_started:
            broker.process_tick(tick)
        else:
            broker._observe_spread(spread)
        ticks += 1
        if ticks % 5_000_000 == 0:
            print(f"ticks={ticks:,} bars={len(engine.candles):,}", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)
    def replay_profile(name: str) -> PaperBroker:
        cfg = configs[name]
        result = PaperBroker(cfg, args.initial_equity, SymbolSpec())
        for source in broker.trades:
            trade = copy.deepcopy(source)
            meta = trade.meta
            score2 = int(meta.get("fill_m5_last_choch_alignment") == "opposite")
            score2 += int(bool(meta.get("bos_displacement_top_quartile")))
            fraction = (.0125 if score2 == 2 else .01 if score2 == 1 else .0075)
            if meta.get("break_kind") == "BOS" and cfg.bos_three_factor_risk_enabled:
                score3 = score2 + int(bool(meta.get("entry_age_top_quartile")))
                fraction = (.0075 if score3 == 0 else .009 if score3 == 1
                            else .011 if score3 == 2 else .0125)
            if meta.get("break_kind") == "CHoCH":
                if cfg.choch_split_risk_enabled:
                    toxic = (bool(meta.get("entry_age_top_quartile")) and
                             bool(meta.get("bos_displacement_top_quartile")))
                    fraction = (.0025 if toxic else .0055
                                if meta.get("setup_m5_opposite_sweep") else .0045)
                elif cfg.choch_risk_cap_fraction is not None:
                    fraction = min(fraction, cfg.choch_risk_cap_fraction)
            if (meta.get("break_kind") == "BOS" and
                    meta.get("fill_m15_alignment") == "counter"):
                fraction = min(.0125, fraction + cfg.bos_m15_counter_bonus_fraction)
            risk_money = result.equity * fraction
            distance = abs(trade.entry - trade.stop)
            loss_per_lot = distance / result.spec.tick_size * result.spec.tick_value
            raw_volume = risk_money / loss_per_lot if loss_per_lot else result.spec.volume_min
            steps = math.floor((raw_volume + 1e-12) / result.spec.volume_step)
            trade.volume = min(result.spec.volume_max,
                               max(result.spec.volume_min, steps * result.spec.volume_step))
            trade.risk_money = loss_per_lot * trade.volume
            move = (trade.exit_price - trade.entry if trade.direction == "bull"
                    else trade.entry - trade.exit_price)
            trade.pnl = move / result.spec.tick_size * result.spec.tick_value * trade.volume
            trade.r_multiple = trade.pnl / trade.risk_money if trade.risk_money else 0.0
            trade.result = "win" if trade.pnl > 0 else "loss"
            trade.meta.update({"applied_risk_fraction": fraction,
                               "risk_comparison_profile": name})
            result.equity += trade.pnl
            result.peak_equity = max(result.peak_equity, result.equity)
            dd = ((result.peak_equity - result.equity) / result.peak_equity
                  if result.peak_equity else 0.0)
            result.max_drawdown = max(result.max_drawdown, dd)
            result.trades.append(trade)
        result.stats.update(broker.stats)
        return result

    comparison = []
    label = "_".join(args.months)
    for name in names:
        profile_broker = replay_profile(name)
        summary = export_reports(profile_broker, args.out / f"{name}_trades.csv",
                                 args.out / f"{name}_summary.csv")
        export_breakdown(profile_broker, args.out / f"{name}_breakdown.csv")
        export_html(profile_broker, args.out / f"{name}_report.html",
                    f"Tick risk comparison - {name}",
                    {"months": label, "ticks": ticks, "bars": len(engine.candles),
                     "warmup bars": args.warmup_bars})
        comparison.append({"profile": name, **summary,
                           "setups_seen": profile_broker.stats["setups_seen"]})
    with (args.out / "comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(comparison[0]))
        writer.writeheader()
        writer.writerows(comparison)
    for row in comparison:
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
