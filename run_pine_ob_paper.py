#!/usr/bin/env python3
"""Run the standalone, read-only MT5 Pine Swing-OB paper trader.

Two independent M5 swing systems feed every path here (live, Paper, and
Historical replay via --backtest):

  Trend Swing (--trend-swing-length, default 12): the existing state-based
  swing/BOS/CHoCH engine (PineSwingOBEngine). Owns Major Swing High/Low,
  Major BOS/CHoCH and the single M5 Trend State (Bullish/Bearish/Neutral)
  that gates trade direction. ~60 minutes of confirmation delay at M5.

  Entry Pivot (--entry-pivot-left/--entry-pivot-right, default 5/5): an
  independent classic, symmetric pivot (see pine_ob_bot/entry_pivot.py) used
  only for local pullback/trigger timing -- confirming/triggering an already
  Major-BOS/CHoCH-formed setup. It never changes the Trend State by itself.
  ~25 minutes of confirmation delay at M5 (right side only; the left side
  needs no future bars).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from pine_ob_bot.app import PaperApp
from pine_ob_bot.cli_risk import resolve_choch_risk_cap, validate_fixed_risk_args
from pine_ob_bot.config import BotConfig
from pine_ob_bot.mt5_feed import MT5ReadOnlyFeed
from pine_ob_bot.historical import run_historical


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pine Swing OB live paper backtest (never sends MT5 orders)")
    parser.add_argument("--once", action="store_true", help="connect, catch up, export, and exit")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument(
        "--trend-swing-length",
        type=int,
        default=None,
        help="M5 major trend swing confirmation length (default: 12)",
    )
    parser.add_argument(
        "--entry-pivot-left",
        type=int,
        default=5,
        help="closed M5 bars to the left of a classic entry pivot (default: 5)",
    )
    parser.add_argument(
        "--entry-pivot-right",
        type=int,
        default=5,
        help="closed M5 bars required to confirm the right side of an entry pivot (default: 5)",
    )
    parser.add_argument("--max-entry-pivot-age", type=int, default=None, metavar="N",
                        help="reject setups whose latest confirmed entry pivot is more "
                             "than N closed M5 bars past its confirmation bar "
                             "(inclusive boundary; default: no age limit)")
    parser.add_argument(
        "--swing-length",
        type=int,
        default=None,
        help=(
            "deprecated alias for --trend-swing-length; "
            "kept for backward compatibility"
        ),
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite output path; generated from swing settings when omitted",
    )
    parser.add_argument("--backtest", type=Path, metavar="CSV",
                        help="replay prior M5 OHLC into separate historical outputs")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--spread", type=float, default=0.20,
                        help="historical XAUUSD spread in price units (default: 0.20)")
    parser.add_argument("--rr", type=float, default=1.5, help="reward:risk target (default: 1.5)")
    parser.add_argument("--target-mode", choices=("fixed_rr", "m5_liquidity_min_rr"),
                        default="fixed_rr",
                        help="fixed RR or nearest active M5 liquidity with RR as minimum")
    parser.add_argument("--breakeven-at-r", type=float, default=None, metavar="R",
                        help="move SL to entry once, without offset, when the trade's "
                             "real MFE reaches this many R of initial risk; the stop "
                             "never moves backward (default: disabled)")
    parser.add_argument("--max-positions", type=int, default=1,
                        help="maximum simultaneous paper positions (default: 1)")
    parser.add_argument("--liquidity-risk-sizing", action="store_true",
                        help="risk 1.25%% with setup sweep, otherwise 0.75%%")
    parser.add_argument("--choch-risk-sizing", action="store_true",
                        help="risk 1.25%% with opposite M5 CHoCH at fill, otherwise 0.75%%")
    parser.add_argument("--combined-context-risk-sizing", action="store_true",
                        help="risk 0.75/1.0/1.25%% from liquidity sweep + opposite CHoCH score")
    parser.add_argument("--displacement-risk-sizing", action="store_true",
                        help="risk 1.25%% for causal top-quartile BOS displacement, else 0.75%%")
    parser.add_argument("--choch-displacement-risk-sizing", action="store_true",
                        help="risk 0.75/1.0/1.25%% from opposite CHoCH + adaptive displacement")
    parser.add_argument("--choch-risk-cap", type=float, default=None, metavar="FRACTION",
                        help="maximum risk fraction for CHoCH setups (default: 0.005; "
                             "cannot be combined with --fixed-risk)")
    parser.add_argument("--three-factor-risk-sizing", action="store_true",
                        help="score opposite CHoCH, displacement and adaptive OB age")
    parser.add_argument("--fixed-risk", action="store_true",
                        help="use BotConfig.risk_fraction uniformly for BOS and CHoCH: "
                             "disables every adaptive risk-sizing model and the CHoCH "
                             "risk cap (choch_risk_cap_fraction=None). Cannot be combined "
                             "with any adaptive risk-sizing flag or an explicit "
                             "--choch-risk-cap.")
    parser.add_argument("--no-market-capture", action="store_true",
                        help="disable live Bid/Ask and closed-candle CSV capture")
    parser.add_argument("--demo-orders", action="store_true",
                        help="mirror tick-opened paper trades to MT5; refuses non-demo accounts")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="disable the local live HTML dashboard")
    parser.add_argument("--dashboard-host", default="127.0.0.1")
    parser.add_argument("--dashboard-port", type=int, default=8765)
    parser.add_argument("--bos-only", action="store_true", help="accept M5 BOS order blocks only")
    parser.add_argument("--include-choch", action="store_true",
                        help="experimental: include CHoCH setups (default is BOS only)")
    parser.add_argument("--strong-choch-only", action="store_true",
                        help="add only causal top-quartile displacement CHoCH setups")
    parser.add_argument("--min-entry-wait", type=int, default=0,
                        help="minimum closed M5 bars between OB formation and fill")
    parser.add_argument("--entry-mode", choices=("limit", "sweep_reclaim"), default="limit",
                        help="limit at OB edge, or close after same-bar sweep and reclaim")
    parser.add_argument("--sweep-atr-buffer", type=float, default=0.20,
                        help="SL buffer in ATR units for sweep_reclaim entries")
    parser.add_argument("--sweep-reclaim-max-bars", type=int, default=1, metavar="N",
                        help="experimental: closed M5 bars the reclaim may lag the "
                             "sweep (default 1 = same-bar sweep+reclaim, the "
                             "original logic; a deeper sweep restarts the window)")
    parser.add_argument("--revival-shadow-audit", action="store_true",
                        help="observe-only audit of trend-cancelled setups: counts "
                             "trend returns within 3/6/12 bars while the OB is "
                             "still valid, fresh sweep+reclaims after the return, "
                             "and hypothetical fills/R. Never trades.")
    parser.add_argument("--run-label", default="", help="suffix for historical output files")
    parser.add_argument("--lifecycle", action="store_true",
                        help="experimental: require extension then retracement before arming")
    return parser


def resolve_swing_settings(args: argparse.Namespace,
                          parser: argparse.ArgumentParser) -> tuple[int, int, int]:
    """Resolve the definitive (trend_swing_length, entry_pivot_left,
    entry_pivot_right) triple, reconciling --trend-swing-length with the
    deprecated --swing-length alias, and validating every window size.

        no flag given                  -> 12
        only --swing-length 10         -> trend_swing_length = 10
        only --trend-swing-length 14   -> trend_swing_length = 14
        both given, equal              -> allowed
        both given, different          -> parser.error (ambiguous CLI)
    """
    if (args.trend_swing_length is not None and args.swing_length is not None
            and args.trend_swing_length != args.swing_length):
        parser.error(
            "--trend-swing-length and deprecated --swing-length "
            "cannot have different values"
        )
    trend_swing_length = (
        args.trend_swing_length
        if args.trend_swing_length is not None
        else args.swing_length
        if args.swing_length is not None
        else 12
    )
    if trend_swing_length < 2:
        parser.error("--trend-swing-length must be at least 2")
    if args.entry_pivot_left < 1:
        parser.error("--entry-pivot-left must be at least 1")
    if args.entry_pivot_right < 1:
        parser.error("--entry-pivot-right must be at least 1")
    return trend_swing_length, args.entry_pivot_left, args.entry_pivot_right


def build_config(args: argparse.Namespace, trend_swing_length: int, db_path: Path) -> BotConfig:
    return BotConfig(
        symbol=args.symbol,
        db_path=db_path,
        rr=args.rr,

        trend_swing_length=trend_swing_length,
        entry_pivot_left=args.entry_pivot_left,
        entry_pivot_right=args.entry_pivot_right,
        max_entry_pivot_age_bars=args.max_entry_pivot_age,

        target_mode=args.target_mode,
        breakeven_trigger_r=args.breakeven_at_r,
        max_open_positions=args.max_positions,

        liquidity_risk_sizing_enabled=args.liquidity_risk_sizing,
        choch_risk_sizing_enabled=args.choch_risk_sizing,
        combined_context_risk_sizing_enabled=(
            args.combined_context_risk_sizing
        ),
        displacement_risk_sizing_enabled=(
            args.displacement_risk_sizing
        ),
        choch_displacement_risk_sizing_enabled=(
            not args.fixed_risk and not any((args.liquidity_risk_sizing,
                args.choch_risk_sizing, args.combined_context_risk_sizing,
                args.displacement_risk_sizing, args.three_factor_risk_sizing))
        ) or args.choch_displacement_risk_sizing,
        choch_risk_cap_fraction=resolve_choch_risk_cap(args.fixed_risk, args.choch_risk_cap),
        three_factor_risk_sizing_enabled=args.three_factor_risk_sizing,

        market_capture_enabled=not args.no_market_capture,
        demo_orders_enabled=args.demo_orders,
        dashboard_enabled=not args.no_dashboard,
        dashboard_host=args.dashboard_host,
        dashboard_port=args.dashboard_port,

        # Default is BOS-only, matching BotConfig and the tick backtest runner
        # (see tools/run_tick_backtest.py, fixed 2026-07): CHoCH order blocks
        # are traded only with an explicit --include-choch/--strong-choch-only.
        allowed_break_kinds=(("BOS", "CHoCH")
                             if (args.include_choch or args.strong_choch_only)
                             and not args.bos_only else ("BOS",)),
        strong_choch_only=args.strong_choch_only,
        min_entry_wait_bars=args.min_entry_wait,
        entry_mode=args.entry_mode,
        sweep_reclaim_atr_buffer=args.sweep_atr_buffer,
        sweep_reclaim_max_bars=args.sweep_reclaim_max_bars,
        revival_shadow_audit=args.revival_shadow_audit,
        entry_lifecycle_enabled=args.lifecycle,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_fixed_risk_args(parser, args)
    trend_swing_length, entry_pivot_left, entry_pivot_right = resolve_swing_settings(args, parser)

    db_path = args.db or Path(
        "pine_ob_bot_data/"
        f"paper_t{trend_swing_length}"
        f"_p{entry_pivot_left}x{entry_pivot_right}.sqlite3"
    )
    # Only used by --backtest; keeps different swing/pivot runs from ever
    # overwriting each other's historical output files.
    effective_run_label = args.run_label or (
        f"t{trend_swing_length}"
        f"_p{entry_pivot_left}x{entry_pivot_right}"
    )

    cfg = build_config(args, trend_swing_length, db_path)

    trend_delay_minutes = trend_swing_length * 5
    entry_delay_minutes = entry_pivot_right * 5
    print(f"M5 trend swing length: {trend_swing_length}")
    print(f"M5 trend swing confirmation delay: {trend_delay_minutes} minutes")
    print(f"M5 entry pivot: left={entry_pivot_left} right={entry_pivot_right}")
    print(f"M5 entry pivot confirmation delay: {entry_delay_minutes} minutes")
    print(f"database: {db_path}")

    try:
        if args.backtest:
            summary = run_historical(args.backtest, cfg, args.initial_equity, args.spread,
                                     effective_run_label)
            for key, value in summary.items():
                print(f"{key}: {value}")
            return 0
        PaperApp(cfg, MT5ReadOnlyFeed(cfg)).run(once=args.once)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"[pine-ob-paper] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
