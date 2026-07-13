"""Shared ``--fixed-risk`` / ``--choch-risk-cap`` resolution for the CLI runners.

``run_pine_ob_paper.py`` and ``tools/run_tick_backtest.py`` both expose the
same risk-sizing flags and must resolve them identically. Centralizing the
resolution and validation here is what keeps that guarantee true instead of
relying on two hand-copied ``if`` chains staying in sync.
"""
from __future__ import annotations

import argparse

DEFAULT_CHOCH_RISK_CAP = 0.005

# CLI dest -> flag, for every adaptive risk-sizing switch that picks BOS/CHoCH
# sizing dynamically. --fixed-risk means none of these may be active.
ADAPTIVE_RISK_SIZING_FLAGS = {
    "liquidity_risk_sizing": "--liquidity-risk-sizing",
    "choch_risk_sizing": "--choch-risk-sizing",
    "combined_context_risk_sizing": "--combined-context-risk-sizing",
    "displacement_risk_sizing": "--displacement-risk-sizing",
    "choch_displacement_risk_sizing": "--choch-displacement-risk-sizing",
    "three_factor_risk_sizing": "--three-factor-risk-sizing",
}


def validate_fixed_risk_args(parser: argparse.ArgumentParser,
                             args: argparse.Namespace) -> None:
    """Reject --fixed-risk combined with any adaptive sizing flag or an
    explicit --choch-risk-cap. Exits via parser.error() (SystemExit) before
    any backtest/live work starts; never silently ignores the conflict.
    """
    if not args.fixed_risk:
        return
    for dest, flag in ADAPTIVE_RISK_SIZING_FLAGS.items():
        if getattr(args, dest):
            parser.error(f"--fixed-risk cannot be combined with {flag}")
    if args.choch_risk_cap is not None:
        parser.error("--fixed-risk cannot be combined with --choch-risk-cap")


def resolve_choch_risk_cap(fixed_risk: bool, explicit_cap: float | None) -> float | None:
    """Resolve the effective ``choch_risk_cap_fraction`` from CLI flags.

    --fixed-risk always removes the cap (BOS and CHoCH then share the same
    BotConfig.risk_fraction). Otherwise an explicit --choch-risk-cap wins,
    and the standard 0.5% default applies when neither was given.
    """
    if fixed_risk:
        return None
    return DEFAULT_CHOCH_RISK_CAP if explicit_cap is None else explicit_cap
