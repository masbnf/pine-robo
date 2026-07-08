#!/usr/bin/env python3
"""Run the selected strategy against downloaded MT5 Bid/Ask ticks.

Every flag accepted by the live/paper runner (run_pine_ob_paper.py) is
mirrored here and wired into the same BotConfig fields, so a tick backtest
can reproduce any live configuration exactly. A few flags are inherently
inapplicable to an offline tick replay and are accepted-but-inert for CLI
parity only (see the notes next to each below):

  --spread is NOT present here: unlike the OHLC --backtest path in
  run_pine_ob_paper.py (which has no real spread and must synthesize one),
  this tick backtest already has the real historical Bid/Ask spread on every
  tick, so there is nothing to override.

  --once, --db, --backtest do not apply to a batch tick replay and are
  omitted (there is no live polling loop and no single CSV path here --
  --month/--data-root/--out take their place).

  --no-market-capture, --demo-orders, --no-dashboard/--dashboard-host/
  --dashboard-port are accepted and stored on BotConfig for parity with the
  live CLI, but run_tick_historical() never constructs a MarketRecorder,
  DemoExecutor, or DashboardServer, so they have no effect in this backtest.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pine_ob_bot.cli_risk import resolve_choch_risk_cap, validate_fixed_risk_args
from pine_ob_bot.config import BotConfig
from pine_ob_bot.tick_historical import run_tick_historical


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", required=True, metavar="YYYY-MM")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--data-root", type=Path,
                        default=Path("pine_ob_bot_data/historical_ticks"))
    parser.add_argument("--out", type=Path,
                        default=Path("pine_ob_bot_data/tick_backtests"))
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--warmup-bars", type=int, default=500)

    parser.add_argument("--trend-swing-length", type=int, default=None,
                        help="M5 major trend swing confirmation length (default: 12)")
    parser.add_argument("--entry-pivot-left", type=int, default=5,
                        help="closed M5 bars to the left of a classic entry pivot (default: 5)")
    parser.add_argument("--entry-pivot-right", type=int, default=5,
                        help="closed M5 bars required to confirm the right side of an entry pivot (default: 5)")
    parser.add_argument("--swing-length", type=int, default=None,
                        help="deprecated alias for --trend-swing-length; kept for backward compatibility")

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
                        help="(no effect here; kept for CLI parity with the live runner)")
    parser.add_argument("--demo-orders", action="store_true",
                        help="(no effect here; kept for CLI parity with the live runner)")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="(no effect here; kept for CLI parity with the live runner)")
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
    parser.add_argument("--run-label", default="",
                        help="output label; auto-generated from swing/pivot sizes when omitted")
    parser.add_argument("--lifecycle", action="store_true",
                        help="experimental: require extension then retracement before arming")
    return parser


def resolve_swing_settings(args: argparse.Namespace,
                          parser: argparse.ArgumentParser) -> tuple[int, int, int]:
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


def build_config(args: argparse.Namespace, trend_swing_length: int) -> BotConfig:
    return BotConfig(
        symbol=args.symbol,
        rr=args.rr,

        trend_swing_length=trend_swing_length,
        entry_pivot_left=args.entry_pivot_left,
        entry_pivot_right=args.entry_pivot_right,

        target_mode=args.target_mode,
        breakeven_trigger_r=args.breakeven_at_r,
        max_open_positions=args.max_positions,

        liquidity_risk_sizing_enabled=args.liquidity_risk_sizing,
        choch_risk_sizing_enabled=args.choch_risk_sizing,
        combined_context_risk_sizing_enabled=args.combined_context_risk_sizing,
        displacement_risk_sizing_enabled=args.displacement_risk_sizing,
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

        allowed_break_kinds=("BOS",) if args.bos_only else ("BOS", "CHoCH"),
        strong_choch_only=args.strong_choch_only,
        min_entry_wait_bars=args.min_entry_wait,
        entry_mode=args.entry_mode,
        sweep_reclaim_atr_buffer=args.sweep_atr_buffer,
        entry_lifecycle_enabled=args.lifecycle,
    )


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_fixed_risk_args(parser, args)
    trend_swing_length, entry_pivot_left, entry_pivot_right = resolve_swing_settings(args, parser)

    paths = sorted((args.data_root / args.month).glob(f"ticks_{args.symbol}_*.csv*"))
    if not paths:
        parser.error(f"no tick files found for {args.symbol} {args.month}")

    cfg = build_config(args, trend_swing_length)

    # Keeps different swing/pivot/symbol runs on the same month from ever
    # overwriting each other's output files (same idea as run_pine_ob_paper.py's
    # auto-generated --run-label for --backtest).
    effective_label = args.run_label or (
        f"{args.symbol}_{args.month}"
        f"_t{trend_swing_length}_p{entry_pivot_left}x{entry_pivot_right}"
    )

    print(f"M5 trend swing length: {trend_swing_length}")
    print(f"M5 trend swing confirmation delay: {trend_swing_length * 5} minutes")
    print(f"M5 entry pivot: left={entry_pivot_left} right={entry_pivot_right}")
    print(f"M5 entry pivot confirmation delay: {entry_pivot_right * 5} minutes")

    summary = run_tick_historical(paths, cfg, args.initial_equity, args.out,
                                  effective_label, args.warmup_bars)
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
