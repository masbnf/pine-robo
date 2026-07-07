"""--fixed-risk behavior for both CLI runners.

Covers the bug where --fixed-risk still passed the default
choch_risk_cap_fraction=0.005 into BotConfig, so CHoCH trades stayed capped
at 0.5%% while BOS trades used the full risk_fraction (1%%) -- making
--fixed-risk neither fixed nor uniform. See pine_ob_bot/cli_risk.py for the
shared resolution/validation both runners now go through.
"""
from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

import run_pine_ob_paper as live_cli
from tools import run_tick_backtest as tick_cli

from pine_ob_bot.cli_risk import DEFAULT_CHOCH_RISK_CAP, validate_fixed_risk_args
from pine_ob_bot.config import BotConfig
from pine_ob_bot.models import OrderBlock, Tick
from pine_ob_bot.paper import PaperBroker, SymbolSpec
from pine_ob_bot.trend_filter import TrendDirection

# Both runners expose the identical set of adaptive risk-sizing flags.
ADAPTIVE_FLAGS = (
    "--liquidity-risk-sizing",
    "--choch-risk-sizing",
    "--combined-context-risk-sizing",
    "--displacement-risk-sizing",
    "--choch-displacement-risk-sizing",
    "--three-factor-risk-sizing",
)

ADAPTIVE_MODEL_FIELDS = (
    "liquidity_risk_sizing_enabled",
    "choch_risk_sizing_enabled",
    "combined_context_risk_sizing_enabled",
    "displacement_risk_sizing_enabled",
    "choch_displacement_risk_sizing_enabled",
    "three_factor_risk_sizing_enabled",
)


def _argv(module, *extra: str) -> list[str]:
    """Prepend whatever each runner's parser requires unconditionally.

    tools/run_tick_backtest.py has a required --month; run_pine_ob_paper.py
    has no required flags. Neither requirement is related to --fixed-risk;
    this only keeps parse_args() from failing on missing --month before the
    --fixed-risk logic under test ever runs.
    """
    base = ["--month", "2026-01"] if module is tick_cli else []
    return [*base, *extra]


def _build_cfg(module, argv: list[str]) -> BotConfig:
    """Parse argv with the real runner parser and build its real BotConfig.

    Mirrors exactly what each runner's own main() does before touching MT5
    or any data file: parse, validate --fixed-risk conflicts, then build.
    """
    parser = module.build_parser()
    args = parser.parse_args(_argv(module, *argv))
    validate_fixed_risk_args(parser, args)
    trend_swing_length, _left, _right = module.resolve_swing_settings(args, parser)
    if module is live_cli:
        return module.build_config(args, trend_swing_length, Path("unused.sqlite3"))
    return module.build_config(args, trend_swing_length)


class FixedRiskCliTests(unittest.TestCase):
    """Runs every assertion against both run_pine_ob_paper and
    tools/run_tick_backtest so the two runners are proven to agree, not just
    individually correct."""

    RUNNERS = (("run_pine_ob_paper.py", live_cli), ("tools/run_tick_backtest.py", tick_cli))

    def test_fixed_risk_disables_every_adaptive_model_and_the_choch_cap(self):
        for name, module in self.RUNNERS:
            with self.subTest(runner=name):
                cfg = _build_cfg(module, ["--fixed-risk"])
                for field in ADAPTIVE_MODEL_FIELDS:
                    self.assertFalse(getattr(cfg, field), f"{field} should be False")
                self.assertIsNone(cfg.choch_risk_cap_fraction)

    def test_default_run_keeps_the_standard_choch_cap_and_default_model(self):
        for name, module in self.RUNNERS:
            with self.subTest(runner=name):
                cfg = _build_cfg(module, [])
                self.assertEqual(cfg.choch_risk_cap_fraction, DEFAULT_CHOCH_RISK_CAP)
                self.assertEqual(cfg.choch_risk_cap_fraction, 0.005)
                # The pre-existing default adaptive model (CHoCH+Displacement)
                # must stay enabled exactly as before this fix.
                self.assertTrue(cfg.choch_displacement_risk_sizing_enabled)

    def test_explicit_non_fixed_cap_is_honored(self):
        for name, module in self.RUNNERS:
            with self.subTest(runner=name):
                cfg = _build_cfg(module, ["--choch-risk-cap", "0.007"])
                self.assertEqual(cfg.choch_risk_cap_fraction, 0.007)

    def test_fixed_risk_rejects_every_adaptive_sizing_flag(self):
        for name, module in self.RUNNERS:
            for flag in ADAPTIVE_FLAGS:
                with self.subTest(runner=name, flag=flag):
                    parser = module.build_parser()
                    args = parser.parse_args(_argv(module, "--fixed-risk", flag))
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit):
                            validate_fixed_risk_args(parser, args)

    def test_fixed_risk_rejects_explicit_choch_risk_cap(self):
        for name, module in self.RUNNERS:
            with self.subTest(runner=name):
                parser = module.build_parser()
                args = parser.parse_args(_argv(module, "--fixed-risk", "--choch-risk-cap", "0.02"))
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        validate_fixed_risk_args(parser, args)

    def test_fixed_risk_alone_does_not_raise(self):
        for name, module in self.RUNNERS:
            with self.subTest(runner=name):
                parser = module.build_parser()
                args = parser.parse_args(_argv(module, "--fixed-risk"))
                validate_fixed_risk_args(parser, args)  # must not raise


class FixedRiskAppliedRiskTests(unittest.TestCase):
    """Proves the *effect* of the fix through the real PaperBroker sizing
    path -- no mocking of risk selection -- for both a BOS and a CHoCH
    setup opened under a --fixed-risk-equivalent BotConfig."""

    def test_bos_and_choch_use_the_same_risk_fraction_and_never_exceed_it(self):
        cfg = BotConfig(risk_fraction=0.01, choch_risk_cap_fraction=None,
                        allowed_break_kinds=("BOS", "CHoCH"),
                        entry_lifecycle_enabled=False, max_open_positions=2)
        spec = SymbolSpec(tick_size=.01, tick_value=1, volume_min=.01, volume_step=.01)
        broker = PaperBroker(cfg, 10_000, spec)
        broker.current_m5_trend = TrendDirection.BULLISH

        bos = OrderBlock("bos", "bull", 100, 99, 0, "t0", 0, "t0", break_kind="BOS")
        choch = OrderBlock("choch", "bull", 100, 99, 1, "t1", 1, "t1", break_kind="CHoCH")
        broker.add_ob(bos)
        broker.add_ob(choch)
        broker.market_index = 2
        broker.process_tick(Tick("t2", 99.98, 100.0))

        self.assertEqual(len(broker.positions), 2)
        by_kind = {p.meta["break_kind"]: p for p in broker.positions}
        bos_position, choch_position = by_kind["BOS"], by_kind["CHoCH"]

        self.assertAlmostEqual(bos_position.meta["applied_risk_fraction"], 0.01)
        self.assertAlmostEqual(choch_position.meta["applied_risk_fraction"], 0.01)

        allowed_risk = cfg.risk_fraction * 10_000
        for position in (bos_position, choch_position):
            self.assertLessEqual(position.risk_money, allowed_risk * 1.01)


if __name__ == "__main__":
    unittest.main()
