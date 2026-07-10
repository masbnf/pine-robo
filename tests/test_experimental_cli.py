"""CLI wiring/validation of the experimental trade-frequency flags on BOTH
real runner parsers (run_pine_ob_paper.py and tools/run_tick_backtest.py),
plus auto run-label token/hash behaviour.

Run with:  pytest tests/test_experimental_cli.py -v
"""
from __future__ import annotations

import contextlib
import io
import unittest

import run_pine_ob_paper as live_cli
from tools import run_tick_backtest as tick_cli

from pine_ob_bot.cli_experimental import (build_run_label, experimental_label_tokens,
                                          resolve_experimental_config,
                                          validate_experimental_args)
from pine_ob_bot.config import BotConfig

MODULES = (live_cli, tick_cli)

INVALID_COMBOS = (
    ["--revival-max-return-bars", "6"],                       # without master
    ["--revival-max-per-ob", "1"],                            # without master
    ["--no-revival-require-fresh-sweep"],                     # without master
    ["--max-entries-per-ob", "2"],                            # without master
    ["--min-reentry-wait-bars", "1"],                         # without master
    ["--reentry-after-loss-only"],                            # without master
    ["--spread-wait-max-seconds", "30"],                      # without master
    ["--spread-wait-max-ticks", "10"],                        # without master
    ["--spread-wait-max-bars", "3"],                          # without master
    ["--wait-for-spread-after-confirmation"],                 # no limit given
    ["--portfolio-risk-cap", "0"],                            # <= 0
    ["--portfolio-risk-cap", "-0.01"],
    ["--sweep-reclaim-max-bars", "0"],                        # < 1
    ["--controlled-revival"],                                 # limit entry mode
    ["--allow-ob-reentry"],                                   # limit entry mode
    ["--entry-mode", "sweep_reclaim", "--controlled-revival",
     "--revival-max-per-ob", "0"],
    ["--entry-mode", "sweep_reclaim", "--controlled-revival",
     "--revival-max-return-bars", "0"],
    ["--entry-mode", "sweep_reclaim", "--allow-ob-reentry",
     "--max-entries-per-ob", "1"],                            # < 2 with re-entry
    ["--entry-mode", "sweep_reclaim", "--wait-for-spread-after-confirmation",
     "--spread-wait-max-ticks", "-3"],
)

VALID_FULL = ["--entry-mode", "sweep_reclaim", "--sweep-reclaim-max-bars", "2",
              "--controlled-revival", "--allow-ob-reentry",
              "--wait-for-spread-after-confirmation",
              "--spread-wait-max-seconds", "30",
              "--max-positions", "2", "--portfolio-risk-cap", "0.0075"]


def _argv(module, *extra: str) -> list[str]:
    base = ["--month", "2026-01"] if module is tick_cli else []
    return [*base, *extra]


def _parse(module, *extra):
    parser = module.build_parser()
    args = parser.parse_args(_argv(module, *extra))
    return parser, args


class ExperimentalCliValidationTests(unittest.TestCase):
    def test_invalid_combinations_error_before_any_work(self):
        for module in MODULES:
            for combo in INVALID_COMBOS:
                with self.subTest(module=module.__name__, combo=combo):
                    parser, args = _parse(module, *combo)
                    with self.assertRaises(SystemExit), \
                            contextlib.redirect_stderr(io.StringIO()):
                        validate_experimental_args(parser, args)

    def test_full_valid_combo_parses_on_both_runners(self):
        for module in MODULES:
            parser, args = _parse(module, *VALID_FULL)
            validate_experimental_args(parser, args)          # must not raise
            resolved = resolve_experimental_config(args)
            self.assertTrue(resolved["controlled_revival"])
            self.assertEqual(resolved["revival_max_return_bars"], 6)
            self.assertTrue(resolved["revival_require_fresh_sweep"])
            self.assertEqual(resolved["revival_max_per_ob"], 1)
            self.assertEqual(resolved["max_entries_per_ob"], 2)
            self.assertEqual(resolved["min_reentry_wait_bars"], 1)
            self.assertTrue(resolved["reentry_require_fresh_sweep"])
            self.assertFalse(resolved["reentry_after_loss_only"])
            self.assertEqual(resolved["spread_wait_max_seconds"], 30.0)
            self.assertEqual(resolved["portfolio_risk_cap"], 0.0075)
            BotConfig(**resolved, entry_mode="sweep_reclaim").validate()

    def test_no_flags_resolve_to_botconfig_defaults(self):
        default = BotConfig()
        for module in MODULES:
            parser, args = _parse(module)
            validate_experimental_args(parser, args)
            resolved = resolve_experimental_config(args)
            for key, value in resolved.items():
                self.assertEqual(value, getattr(default, key),
                                 f"{module.__name__}: {key}")


class RunLabelTests(unittest.TestCase):
    def test_no_flags_keep_the_exact_legacy_label(self):
        for module in MODULES:
            parser, args = _parse(module)
            resolved = resolve_experimental_config(args)
            tokens = experimental_label_tokens(args, resolved)
            self.assertEqual(tokens, [])
            self.assertEqual(build_run_label("XAUUSD_all_t9_p3x3", tokens, resolved),
                             "XAUUSD_all_t9_p3x3")

    def test_active_flags_produce_readable_tokens(self):
        parser, args = _parse(tick_cli, *VALID_FULL, "--max-entry-pivot-age", "12")
        resolved = resolve_experimental_config(args)
        tokens = experimental_label_tokens(args, resolved)
        label = build_run_label("XAUUSD_all_t9_p3x3", tokens, resolved, max_length=200)
        for piece in ("sr2", "rev6", "re2", "spwait30", "pos2", "prisk0075", "mea12"):
            self.assertIn(piece, label)

    def test_overlong_label_falls_back_to_stable_hash(self):
        parser, args = _parse(tick_cli, *VALID_FULL)
        resolved = resolve_experimental_config(args)
        tokens = experimental_label_tokens(args, resolved)
        short = build_run_label("XAUUSD_all_t9_p3x3", tokens, resolved, max_length=24)
        again = build_run_label("XAUUSD_all_t9_p3x3", tokens, resolved, max_length=24)
        self.assertEqual(short, again)                        # stable
        self.assertTrue(short.startswith("XAUUSD_all_t9_p3x3_x"))
        self.assertLessEqual(len(short), 24 + len("_x") + 8)


if __name__ == "__main__":
    unittest.main()
