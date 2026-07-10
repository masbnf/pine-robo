"""Shared CLI wiring for the experimental trade-frequency features.

``run_pine_ob_paper.py`` and ``tools/run_tick_backtest.py`` both expose the
same experimental flags and must parse, validate, resolve and label them
identically -- same pattern as cli_risk.py. Everything here is opt-in:
with no experimental flag on the command line, resolve_experimental_config()
returns only the (all-off) master switches and the auto run label is exactly
the legacy one.

Dependent parameters use ``default=None`` at the parser so "user gave the
flag" is distinguishable from "not given": any dependent flag without its
master flag is a parser.error before any backtest/live work starts. The
spec defaults (revival 6/fresh/1, re-entry 2/1/fresh/False) are applied at
resolve time, only while the master flag is on.
"""
from __future__ import annotations

import argparse
import hashlib
import json

MAX_LABEL_LENGTH = 64

_SPREAD_WAIT_LIMITS = (
    ("spread_wait_max_seconds", "--spread-wait-max-seconds"),
    ("spread_wait_max_ticks", "--spread-wait-max-ticks"),
    ("spread_wait_max_bars", "--spread-wait-max-bars"),
)


def add_experimental_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "experimental trade-frequency features",
        "All OFF by default; with none of these flags the run is bit-for-bit "
        "identical to the legacy behaviour (guarded by the legacy regression "
        "test). Revival/re-entry require --entry-mode sweep_reclaim.")
    group.add_argument("--controlled-revival", action="store_true",
                       help="REAL revival of setups cancelled purely by an M5 trend "
                            "change: if the trend returns in time and the OB is still "
                            "valid, a brand-new order is created and must earn a fresh "
                            "sweep+reclaim. Never re-activates the old order; weak "
                            "CHoCH is never revived.")
    group.add_argument("--revival-max-return-bars", type=int, default=None, metavar="N",
                       help="closed M5 bars the trend may take to return "
                            "(default 6; requires --controlled-revival)")
    group.add_argument("--revival-require-fresh-sweep",
                       action=argparse.BooleanOptionalAction, default=None,
                       help="require a completely fresh sweep+reclaim after the trend "
                            "returns (default: required; opt out with "
                            "--no-revival-require-fresh-sweep; requires "
                            "--controlled-revival)")
    group.add_argument("--revival-max-per-ob", type=int, default=None, metavar="N",
                       help="maximum revivals per Order Block "
                            "(default 1; requires --controlled-revival)")
    group.add_argument("--allow-ob-reentry", action="store_true",
                       help="allow one controlled second entry on an Order Block after "
                            "the previous trade on it CLOSED: still-valid OB, aligned "
                            "trend, min wait, and a completely fresh sweep+reclaim "
                            "(the first entry's sweep is never reusable). Weak CHoCH "
                            "never re-enters.")
    group.add_argument("--max-entries-per-ob", type=int, default=None, metavar="N",
                       help="total entry attempts per OB including the first entry "
                            "(default 2; requires --allow-ob-reentry; must be >= 2)")
    group.add_argument("--min-reentry-wait-bars", type=int, default=None, metavar="N",
                       help="closed M5 bars between the previous exit and the earliest "
                            "re-entry order (default 1; requires --allow-ob-reentry)")
    group.add_argument("--reentry-require-fresh-sweep",
                       action=argparse.BooleanOptionalAction, default=None,
                       help="require a fresh sweep+reclaim after the previous exit "
                            "(default: required; opt out with "
                            "--no-reentry-require-fresh-sweep; requires "
                            "--allow-ob-reentry)")
    group.add_argument("--reentry-after-loss-only", action="store_true",
                       help="only allow a re-entry after a LOSING trade on the OB "
                            "(requires --allow-ob-reentry)")
    group.add_argument("--wait-for-spread-after-confirmation", action="store_true",
                       help="when a confirmed entry is spread-blocked, wait in an "
                            "explicit waiting_spread state (bounded by the limits "
                            "below) instead of the legacy unbounded implicit retry; "
                            "fills use the real price at the moment the spread "
                            "becomes acceptable and are re-sized then. Tick paths "
                            "only (live paper / tick backtest).")
    group.add_argument("--spread-wait-max-seconds", type=float, default=None, metavar="N",
                       help="cancel the wait after N seconds (requires "
                            "--wait-for-spread-after-confirmation)")
    group.add_argument("--spread-wait-max-ticks", type=int, default=None, metavar="N",
                       help="cancel the wait after N blocked ticks (requires "
                            "--wait-for-spread-after-confirmation)")
    group.add_argument("--spread-wait-max-bars", type=int, default=None, metavar="N",
                       help="cancel the wait after N closed M5 bars (requires "
                            "--wait-for-spread-after-confirmation)")
    group.add_argument("--portfolio-risk-cap", type=float, default=None, metavar="FRACTION",
                       help="reject any fill whose projected total open risk (sum of "
                            "open positions' entry-to-stop risk plus the new trade, "
                            "as a fraction of equity) exceeds this cap, e.g. 0.0075 "
                            "= 0.75%%. Reject-only: volume is never reduced. "
                            "Default: no cap (legacy).")
    group.add_argument("--m1-entry-assist", action="store_true",
                       help="EXPERIMENTAL: build causal M1 bars from the same tick "
                            "stream and use them to refine ENTRY TIMING on active, "
                            "valid M5 setups. M5 stays the only source of trend/"
                            "structure/OB/setups; requires --entry-mode sweep_reclaim "
                            "and tick data (rejected with the OHLC --backtest path).")
    group.add_argument("--m1-assist-mode", choices=("shadow", "sequence", "entry"),
                       default=None,
                       help="shadow = observe-only hypothetical M1 entries (default; "
                            "the first evaluation stage); sequence = only "
                            "disambiguate same-M5-bar sweep/reclaim ordering (can "
                            "veto a false confirmation); entry = valid closed-bar "
                            "M1 sweep+reclaim may enter an active M5 setup early. "
                            "Requires --m1-entry-assist.")
    group.add_argument("--m1-reclaim-max-bars", type=int, default=None, metavar="N",
                       help="closed M1 bars the M1 reclaim may lag the M1 sweep "
                            "(default 3; requires --m1-entry-assist)")
    group.add_argument("--m1-max-confirmations-per-ob", type=int, default=None,
                       metavar="N",
                       help="cap of M1 confirmations per Order Block "
                            "(default 1; requires --m1-entry-assist)")
    group.add_argument("--m1-entry-expiry-bars", type=int, default=None, metavar="N",
                       help="closed M1 bars an unfilled M1 confirmation stays valid "
                            "before reverting to the clean M5 fallback "
                            "(default 5; requires --m1-entry-assist)")
    group.add_argument("--m1-require-closed-bar",
                       action=argparse.BooleanOptionalAction, default=None,
                       help="only CLOSED M1 bars may confirm; fill uses the first "
                            "eligible tick after the close (default: required; "
                            "requires --m1-entry-assist)")
    group.add_argument("--m1-use-sequence-validation",
                       action=argparse.BooleanOptionalAction, default=None,
                       help="in sequence/entry modes, veto M5 same-bar confirmations "
                            "whose M1 ordering shows reclaim-before-sweep (default: "
                            "on; requires --m1-entry-assist)")
    group.add_argument("--m1-refine-stop", action="store_true",
                       help="SEPARATE experiment: place the stop behind the real M1 "
                            "sweep extreme plus --m1-stop-atr-buffer instead of the "
                            "M5 stop (requires --m1-entry-assist; default off so "
                            "entry and stop effects never mix)")
    group.add_argument("--m1-stop-atr-buffer", type=float, default=None, metavar="FLOAT",
                       help="ATR buffer behind the M1 sweep extreme "
                            "(default 0.20; requires --m1-refine-stop)")


def validate_experimental_args(parser: argparse.ArgumentParser,
                               args: argparse.Namespace) -> None:
    """Reject invalid/ambiguous combinations before any work starts."""
    if args.sweep_reclaim_max_bars < 1:
        parser.error("--sweep-reclaim-max-bars must be at least 1")
    if not args.controlled_revival:
        for dest, flag in (("revival_max_return_bars", "--revival-max-return-bars"),
                           ("revival_require_fresh_sweep",
                            "--[no-]revival-require-fresh-sweep"),
                           ("revival_max_per_ob", "--revival-max-per-ob")):
            if getattr(args, dest) is not None:
                parser.error(f"{flag} requires --controlled-revival")
    else:
        if args.entry_mode != "sweep_reclaim":
            parser.error("--controlled-revival requires --entry-mode sweep_reclaim")
        if args.revival_max_return_bars is not None and args.revival_max_return_bars < 1:
            parser.error("--revival-max-return-bars must be at least 1")
        if args.revival_max_per_ob is not None and args.revival_max_per_ob < 1:
            parser.error("--revival-max-per-ob must be at least 1")
    if not args.allow_ob_reentry:
        for dest, flag in (("max_entries_per_ob", "--max-entries-per-ob"),
                           ("min_reentry_wait_bars", "--min-reentry-wait-bars"),
                           ("reentry_require_fresh_sweep",
                            "--[no-]reentry-require-fresh-sweep")):
            if getattr(args, dest) is not None:
                parser.error(f"{flag} requires --allow-ob-reentry")
        if args.reentry_after_loss_only:
            parser.error("--reentry-after-loss-only requires --allow-ob-reentry")
    else:
        if args.entry_mode != "sweep_reclaim":
            parser.error("--allow-ob-reentry requires --entry-mode sweep_reclaim")
        if args.max_entries_per_ob is not None and args.max_entries_per_ob < 2:
            parser.error("--max-entries-per-ob must be at least 2 when "
                         "--allow-ob-reentry is on")
        if args.min_reentry_wait_bars is not None and args.min_reentry_wait_bars < 0:
            parser.error("--min-reentry-wait-bars cannot be negative")
    if not args.wait_for_spread_after_confirmation:
        for dest, flag in _SPREAD_WAIT_LIMITS:
            if getattr(args, dest) is not None:
                parser.error(f"{flag} requires --wait-for-spread-after-confirmation")
    else:
        values = [getattr(args, dest) for dest, _ in _SPREAD_WAIT_LIMITS]
        if all(value is None for value in values):
            parser.error("--wait-for-spread-after-confirmation needs at least one "
                         "of --spread-wait-max-seconds / --spread-wait-max-ticks / "
                         "--spread-wait-max-bars")
        for (dest, flag), value in zip(_SPREAD_WAIT_LIMITS, values):
            if value is not None and value <= 0:
                parser.error(f"{flag} must be positive")
    if args.portfolio_risk_cap is not None and not 0 < args.portfolio_risk_cap <= 1:
        parser.error("--portfolio-risk-cap must be in (0, 1]")
    if not args.m1_entry_assist:
        for dest, flag in (("m1_assist_mode", "--m1-assist-mode"),
                           ("m1_reclaim_max_bars", "--m1-reclaim-max-bars"),
                           ("m1_max_confirmations_per_ob",
                            "--m1-max-confirmations-per-ob"),
                           ("m1_entry_expiry_bars", "--m1-entry-expiry-bars"),
                           ("m1_require_closed_bar", "--[no-]m1-require-closed-bar"),
                           ("m1_use_sequence_validation",
                            "--[no-]m1-use-sequence-validation")):
            if getattr(args, dest) is not None:
                parser.error(f"{flag} requires --m1-entry-assist")
        if args.m1_refine_stop:
            parser.error("--m1-refine-stop requires --m1-entry-assist")
    else:
        if args.entry_mode != "sweep_reclaim":
            parser.error("--m1-entry-assist requires --entry-mode sweep_reclaim")
        if getattr(args, "backtest", None):
            parser.error("--m1-entry-assist needs tick data; the OHLC --backtest "
                         "path has no ticks to build M1 bars from")
        if args.m1_reclaim_max_bars is not None and args.m1_reclaim_max_bars < 1:
            parser.error("--m1-reclaim-max-bars must be at least 1")
        if (args.m1_max_confirmations_per_ob is not None and
                args.m1_max_confirmations_per_ob < 1):
            parser.error("--m1-max-confirmations-per-ob must be at least 1")
        if args.m1_entry_expiry_bars is not None and args.m1_entry_expiry_bars < 1:
            parser.error("--m1-entry-expiry-bars must be at least 1")
    if args.m1_stop_atr_buffer is not None and not args.m1_refine_stop:
        parser.error("--m1-stop-atr-buffer requires --m1-refine-stop")
    if args.m1_stop_atr_buffer is not None and args.m1_stop_atr_buffer < 0:
        parser.error("--m1-stop-atr-buffer cannot be negative")


def resolve_experimental_config(args: argparse.Namespace) -> dict:
    """BotConfig kwargs for the experimental features.

    Spec defaults are applied ONLY while the corresponding master flag is on;
    with everything off this returns just the three False switches and a None
    cap, which are exactly the BotConfig defaults.
    """
    kwargs: dict = {
        "controlled_revival": args.controlled_revival,
        "allow_ob_reentry": args.allow_ob_reentry,
        "wait_for_spread_after_confirmation": args.wait_for_spread_after_confirmation,
        "portfolio_risk_cap": args.portfolio_risk_cap,
    }
    if args.controlled_revival:
        kwargs["revival_max_return_bars"] = (
            args.revival_max_return_bars if args.revival_max_return_bars is not None else 6)
        kwargs["revival_require_fresh_sweep"] = (
            True if args.revival_require_fresh_sweep is None
            else args.revival_require_fresh_sweep)
        kwargs["revival_max_per_ob"] = (
            args.revival_max_per_ob if args.revival_max_per_ob is not None else 1)
    if args.allow_ob_reentry:
        kwargs["max_entries_per_ob"] = (
            args.max_entries_per_ob if args.max_entries_per_ob is not None else 2)
        kwargs["min_reentry_wait_bars"] = (
            args.min_reentry_wait_bars if args.min_reentry_wait_bars is not None else 1)
        kwargs["reentry_require_fresh_sweep"] = (
            True if args.reentry_require_fresh_sweep is None
            else args.reentry_require_fresh_sweep)
        kwargs["reentry_after_loss_only"] = args.reentry_after_loss_only
    if args.wait_for_spread_after_confirmation:
        kwargs["spread_wait_max_seconds"] = args.spread_wait_max_seconds
        kwargs["spread_wait_max_ticks"] = args.spread_wait_max_ticks
        kwargs["spread_wait_max_bars"] = args.spread_wait_max_bars
    kwargs["m1_entry_assist"] = args.m1_entry_assist
    if args.m1_entry_assist:
        kwargs["m1_assist_mode"] = args.m1_assist_mode or "shadow"
        kwargs["m1_reclaim_max_bars"] = (
            args.m1_reclaim_max_bars if args.m1_reclaim_max_bars is not None else 3)
        kwargs["m1_max_confirmations_per_ob"] = (
            args.m1_max_confirmations_per_ob
            if args.m1_max_confirmations_per_ob is not None else 1)
        kwargs["m1_entry_expiry_bars"] = (
            args.m1_entry_expiry_bars if args.m1_entry_expiry_bars is not None else 5)
        kwargs["m1_require_closed_bar"] = (
            True if args.m1_require_closed_bar is None else args.m1_require_closed_bar)
        kwargs["m1_use_sequence_validation"] = (
            True if args.m1_use_sequence_validation is None
            else args.m1_use_sequence_validation)
        kwargs["m1_refine_stop"] = args.m1_refine_stop
        if args.m1_refine_stop:
            kwargs["m1_stop_atr_buffer"] = (
                args.m1_stop_atr_buffer if args.m1_stop_atr_buffer is not None else 0.20)
    return kwargs


def experimental_label_tokens(args: argparse.Namespace, resolved: dict) -> list[str]:
    """Short, readable tokens for the auto run label -- only for features
    that are actually active, so a no-flag run keeps the exact legacy label
    (and therefore never collides with or overwrites older output files)."""
    tokens: list[str] = []
    if args.sweep_reclaim_max_bars > 1:
        tokens.append(f"sr{args.sweep_reclaim_max_bars}")
    if resolved.get("controlled_revival"):
        tokens.append(f"rev{resolved['revival_max_return_bars']}")
        if not resolved.get("revival_require_fresh_sweep", True):
            tokens.append("revnofs")
    if resolved.get("allow_ob_reentry"):
        tokens.append(f"re{resolved['max_entries_per_ob']}")
        if resolved.get("min_reentry_wait_bars", 1) != 1:
            tokens.append(f"rew{resolved['min_reentry_wait_bars']}")
        if resolved.get("reentry_after_loss_only"):
            tokens.append("reloss")
    if resolved.get("wait_for_spread_after_confirmation"):
        if resolved.get("spread_wait_max_seconds") is not None:
            tokens.append(f"spwait{resolved['spread_wait_max_seconds']:g}")
        elif resolved.get("spread_wait_max_ticks") is not None:
            tokens.append(f"spwait{resolved['spread_wait_max_ticks']}t")
        else:
            tokens.append(f"spwait{resolved['spread_wait_max_bars']}b")
    if resolved.get("m1_entry_assist"):
        mode = resolved.get("m1_assist_mode", "shadow")
        tokens.append({"shadow": "m1shadow", "sequence": "m1seq",
                       "entry": "m1entry"}[mode])
        if mode != "sequence":
            tokens.append(f"m1r{resolved.get('m1_reclaim_max_bars', 3)}")
        if resolved.get("m1_refine_stop"):
            tokens.append("m1rs")
    if args.max_positions > 1:
        tokens.append(f"pos{args.max_positions}")
    if resolved.get("portfolio_risk_cap") is not None:
        tokens.append("prisk" + f"{resolved['portfolio_risk_cap']:.4f}".split(".")[1])
    if getattr(args, "max_entry_pivot_age", None) is not None:
        tokens.append(f"mea{args.max_entry_pivot_age}")
    return tokens


def stable_config_hash(payload: dict) -> str:
    """Stable short hash of a config payload (sorted JSON, sha1/8)."""
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()[:8]


def build_run_label(base: str, tokens: list[str], hash_payload: dict,
                    max_length: int = MAX_LABEL_LENGTH) -> str:
    """base + tokens, or a short readable base + stable config hash when the
    tokenized name would get unwieldy. A no-token call returns base verbatim."""
    if not tokens:
        return base
    label = "_".join([base, *tokens])
    if len(label) <= max_length:
        return label
    return f"{base}_x{stable_config_hash(hash_payload)}"
