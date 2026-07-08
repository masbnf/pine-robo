"""Streaming Bid/Ask tick backtest with M5 bar-close strategy signals."""
from __future__ import annotations

import gzip
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .config import BotConfig
from .entry_pivot import ClassicEntryPivotDetector
from .liquidity_context import LiquidityTracker
from .models import Candle, Tick
from .mtf_context import M15Context
from .paper import PaperBroker, SymbolSpec
from .pine_engine import PineSwingOBEngine
from .reporting import export_breakdown, export_html, export_reports
from .structure_context import ChochContext, DisplacementContext, displacement_snapshot
from .trend_filter import trend_from_engine_state


def iter_ticks(paths: Iterable[Path]):
    previous = None
    for path in sorted(paths):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", newline="", encoding="utf-8") as handle:
            header = handle.readline().rstrip("\r\n").split(",")
            try:
                time_col, bid_col, ask_col = (header.index("time"), header.index("bid"),
                                              header.index("ask"))
            except ValueError:
                continue
            for line in handle:
                try:
                    row = line.rstrip("\r\n").split(",")
                    tick = Tick(row[time_col], float(row[bid_col]), float(row[ask_col]))
                except (IndexError, TypeError, ValueError):
                    continue
                key = (tick.time, tick.bid, tick.ask)
                if key == previous or tick.bid <= 0 or tick.ask <= 0:
                    continue
                previous = key
                yield tick


@dataclass
class M5Builder:
    bucket: str | None = None
    time: str | None = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0

    def push(self, tick: Tick) -> Candle | None:
        # MT5 capture uses sortable ISO timestamps. Building the UTC M5 key
        # from its date/hour/minute avoids parsing a datetime for every tick.
        try:
            minute = int(tick.time[14:16])
            bucket_time = f"{tick.time[:14]}{minute - minute % 5:02d}:00+00:00"
        except (ValueError, IndexError):
            stamp = datetime.fromisoformat(tick.time)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            epoch = int(stamp.timestamp())
            bucket_time = datetime.fromtimestamp(epoch - epoch % 300, timezone.utc).isoformat()
        bucket = bucket_time
        if self.bucket == bucket:
            self.high = max(self.high, tick.bid)
            self.low = min(self.low, tick.bid)
            self.close = tick.bid
            return None
        closed = None
        if self.bucket is not None:
            closed = Candle(self.time, self.open, self.high, self.low, self.close)
        self.bucket = bucket
        self.time = bucket_time
        self.open = self.high = self.low = self.close = tick.bid
        return closed


def run_tick_historical(paths: list[Path], cfg: BotConfig, initial_equity: float,
                        output_root: Path, label: str, warmup_bars: int = 500) -> dict:
    broker = PaperBroker(cfg, initial_equity, SymbolSpec())
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
    builder = M5Builder()
    ticks = 0
    trading_started = False

    def close_bar(candle: Candle) -> None:
        nonlocal trading_started
        # Same candle-driven state advance as PaperApp.on_closed_candle: sets
        # market_index, advances the optional lifecycle AND confirms pending
        # sweep_reclaim orders. Running it before this candle's own freshly
        # formed OBs are added means it can only confirm pre-existing orders,
        # so no look-ahead is introduced -- identical ordering to Live.
        index = len(engine.candles)
        broker.process_signal_candle(candle, index)
        m15_bar = m15.process_m5(candle)
        liq_m5.process(candle)
        if m15_bar:
            liq_m15.process(m15_bar)
        breaks, formed, invalidated = engine.process(candle)
        choch.process(breaks)
        # Same reasoning as the other two backtest runners: the trend must be
        # current (and any now-opposing pending order cancelled) before this
        # candle's own freshly formed OBs are turned into orders, and before
        # any subsequent tick can fill one.
        broker.update_m5_trend(trend_from_engine_state(engine.trend), candle.time)
        # Entry Pivot runs right after the trend update and before setups are
        # built, mirroring PaperApp.on_closed_candle and run_historical.
        for confirmed_pivot in entry_pivot.process(candle):
            broker.update_entry_pivot(confirmed_pivot)
        for ob in formed:
            displacement_data = displacement_snapshot(candle, ob, breaks)
            ranker = displacement if ob.break_kind == "BOS" else choch_displacement
            rank = ranker.observe(displacement_data.get("bos_close_through_atr"))
            if index < warmup_bars:
                continue
            pivot = engine.swing_low if ob.direction == "bull" else engine.swing_high
            broker.add_ob(ob, {**m15.snapshot(ob.direction, candle.close),
                               **liq_m5.snapshot(ob.direction, candle.close, "liq_m5"),
                               **liq_m15.snapshot(ob.direction, candle.close, "liq_m15"),
                               **liq_m5.setup_snapshot(ob.direction,
                                                       pivot.bar_index if pivot else None,
                                                       ob.formed_index),
                               **choch.snapshot(ob.direction, index),
                               **displacement_data, **rank,
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
        if not trading_started and len(engine.candles) >= warmup_bars:
            broker.pending.clear()
            trading_started = True

    for tick in iter_ticks(paths):
        closed = builder.push(tick)
        if closed:
            close_bar(closed)
        if trading_started:
            broker.process_tick(tick)
        else:
            broker._observe_spread(max(0.0, tick.ask - tick.bid))
        ticks += 1

    output_root.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)
    summary = export_reports(broker, output_root / f"tick_{safe}_trades.csv",
                             output_root / f"tick_{safe}_summary.csv")
    export_breakdown(broker, output_root / f"tick_{safe}_breakdown.csv")
    export_html(broker, output_root / f"tick_{safe}_report.html",
                f"Pine Swing-OB Tick Backtest — {label}",
                {"source files": len(paths), "ticks": ticks, "closed M5": len(engine.candles),
                 "warmup bars": warmup_bars, "trend swing": cfg.trend_swing_length,
                 "entry pivot": f"{cfg.entry_pivot_left}/{cfg.entry_pivot_right}", "RR": cfg.rr,
                 "breaks": "+".join(cfg.allowed_break_kinds)})
    summary.update({"ticks": ticks, "bars": len(engine.candles), "files": len(paths),
                    "warmup_bars": warmup_bars,
                    "trend_swing_length": cfg.trend_swing_length,
                    "entry_pivot_left": cfg.entry_pivot_left,
                    "entry_pivot_right": cfg.entry_pivot_right,
                    "buy_setups": broker.stats.get("buy_setups_created", 0),
                    "sell_setups": broker.stats.get("sell_setups_created", 0),
                    **broker.stats})
    age_samples = broker.stats.get("entry_pivot_age_samples", 0)
    summary["avg_entry_pivot_age_bars"] = (
        round(broker.stats.get("entry_pivot_age_sum_bars", 0) / age_samples, 2)
        if age_samples else None)
    summary["max_entry_pivot_age_seen"] = (
        broker.stats.get("entry_pivot_age_max_bars", 0) if age_samples else None)
    return summary
