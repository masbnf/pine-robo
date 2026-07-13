"""M1 CLI validation, --config-only, snapshots and the flag registry.

Run with:  pytest tests/test_m1_cli_and_flags.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path

import run_pine_ob_paper as live_cli
from tools import run_tick_backtest as tick_cli

from pine_ob_bot.cli_experimental import (build_run_label, experimental_label_tokens,
                                          resolve_experimental_config,
                                          validate_experimental_args)
from pine_ob_bot.config import BotConfig
from pine_ob_bot.feature_flags import (FEATURE_FLAGS, effective_config_report,
                                       effective_config_snapshot,
                                       write_config_snapshots)

MODULES = (live_cli, tick_cli)

M1_BASE = ["--entry-mode", "sweep_reclaim", "--m1-entry-assist"]

INVALID = (
    ["--m1-assist-mode", "entry"],                        # without master
    ["--m1-reclaim-max-bars", "3"],
    ["--m1-max-confirmations-per-ob", "1"],
    ["--m1-entry-expiry-bars", "5"],
    ["--m1-require-closed-bar"],
    ["--no-m1-use-sequence-validation"],
    ["--m1-refine-stop"],
    ["--m1-stop-atr-buffer", "0.2"],                      # without --m1-refine-stop
    ["--m1-entry-assist"],                                # limit entry mode
    [*M1_BASE, "--m1-reclaim-max-bars", "0"],
    [*M1_BASE, "--m1-max-confirmations-per-ob", "0"],
    [*M1_BASE, "--m1-entry-expiry-bars", "0"],
    [*M1_BASE, "--m1-stop-atr-buffer", "0.2"],            # buffer w/o refine-stop
    ["--m1-max-lead-minutes", "30"],                      # without master
    ["--m1-risk-multiplier", "0.5"],                      # without master
    [*M1_BASE, "--m1-max-lead-minutes", "0"],
    [*M1_BASE, "--m1-max-lead-minutes", "-5"],
    [*M1_BASE, "--m1-risk-multiplier", "0.5"],            # entry mode not given (shadow default)
    [*M1_BASE, "--m1-assist-mode", "shadow", "--m1-risk-multiplier", "0.5"],
    [*M1_BASE, "--m1-assist-mode", "sequence", "--m1-risk-multiplier", "0.5"],
    [*M1_BASE, "--m1-assist-mode", "entry", "--m1-risk-multiplier", "0"],
    [*M1_BASE, "--m1-assist-mode", "entry", "--m1-risk-multiplier", "1.5"],
    [*M1_BASE, "--m1-assist-mode", "entry", "--m1-risk-multiplier", "-0.5"],
)


def _argv(module, *extra: str) -> list[str]:
    base = ["--month", "2026-01"] if module is tick_cli else []
    return [*base, *extra]


def _parse(module, *extra):
    parser = module.build_parser()
    return parser, parser.parse_args(_argv(module, *extra))


class M1CliValidationTests(unittest.TestCase):
    def test_invalid_combinations_error_on_both_parsers(self):
        for module in MODULES:
            for combo in INVALID:
                with self.subTest(module=module.__name__, combo=combo):
                    parser, args = _parse(module, *combo)
                    with self.assertRaises(SystemExit), \
                            contextlib.redirect_stderr(io.StringIO()):
                        validate_experimental_args(parser, args)

    def test_m1_rejected_with_ohlc_backtest(self):
        parser, args = _parse(live_cli, *M1_BASE, "--backtest", "data.csv")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            validate_experimental_args(parser, args)

    def test_defaults_resolve_per_spec(self):
        parser, args = _parse(tick_cli, *M1_BASE)
        validate_experimental_args(parser, args)
        resolved = resolve_experimental_config(args)
        self.assertEqual(resolved["m1_assist_mode"], "shadow")   # stage-one default
        self.assertEqual(resolved["m1_reclaim_max_bars"], 3)
        self.assertEqual(resolved["m1_max_confirmations_per_ob"], 1)
        self.assertEqual(resolved["m1_entry_expiry_bars"], 5)
        self.assertTrue(resolved["m1_require_closed_bar"])
        self.assertTrue(resolved["m1_use_sequence_validation"])
        BotConfig(**resolved, entry_mode="sweep_reclaim").validate()

    def test_lead_and_risk_multiplier_resolve_and_label(self):
        for lead, risk, lead_token, risk_token in (
                (15, 0.5, "m1lead15", "m1risk05"),
                (30, 0.25, "m1lead30", "m1risk025"),
                (60, None, "m1lead60", None)):
            extra = [*M1_BASE, "--m1-assist-mode", "entry",
                    "--m1-max-lead-minutes", str(lead)]
            if risk is not None:
                extra += ["--m1-risk-multiplier", str(risk)]
            parser, args = _parse(tick_cli, *extra)
            validate_experimental_args(parser, args)
            resolved = resolve_experimental_config(args)
            self.assertEqual(resolved["m1_max_lead_minutes"], float(lead))
            self.assertEqual(resolved["m1_risk_multiplier"], risk if risk is not None else 1.0)
            BotConfig(**resolved, entry_mode="sweep_reclaim").validate()
            tokens = experimental_label_tokens(args, resolved)
            label = build_run_label("XAUUSD_all_t9_p3x3", tokens, resolved, max_length=200)
            self.assertIn(lead_token, label)
            if risk_token is not None:
                self.assertIn(risk_token, label)

    def test_lead_without_risk_multiplier_defaults_to_noop(self):
        parser, args = _parse(tick_cli, *M1_BASE, "--m1-max-lead-minutes", "45")
        validate_experimental_args(parser, args)
        resolved = resolve_experimental_config(args)
        self.assertEqual(resolved["m1_max_lead_minutes"], 45.0)
        self.assertEqual(resolved["m1_risk_multiplier"], 1.0)

    def test_run_label_tokens_for_all_modes(self):
        for mode, token in (("shadow", "m1shadow"), ("sequence", "m1seq"),
                            ("entry", "m1entry")):
            parser, args = _parse(tick_cli, *M1_BASE, "--m1-assist-mode", mode,
                                  "--m1-reclaim-max-bars", "3")
            resolved = resolve_experimental_config(args)
            tokens = experimental_label_tokens(args, resolved)
            label = build_run_label("XAUUSD_all_t9_p3x3", tokens, resolved,
                                    max_length=200)
            self.assertIn(token, label)
            if mode != "sequence":
                self.assertIn("m1r3", label)


class FlagRegistryTests(unittest.TestCase):
    def test_every_registry_flag_appears_in_report_and_snapshot(self):
        cfg = BotConfig(entry_mode="sweep_reclaim", m1_entry_assist=True,
                        m1_assist_mode="entry")
        report = effective_config_report(cfg, {"run_label": "x"})
        snapshot = effective_config_snapshot(cfg, {"run_label": "x"})
        seen = {**snapshot["feature_flags"]["enabled"],
                **snapshot["feature_flags"]["disabled"]}
        for field, spec in FEATURE_FLAGS.items():
            self.assertIn(field, seen)
            self.assertEqual(seen[field]["cli"], spec["cli"])
            self.assertEqual(seen[field]["default"], spec["default"])
        self.assertIn("M1 Entry Assist", report)
        self.assertIn("[EXPERIMENTAL]", report)
        self.assertIn("[CHANGED]", report)

    def test_config_hash_stable_for_same_config_and_differs_otherwise(self):
        cfg_a = BotConfig(entry_mode="sweep_reclaim", m1_entry_assist=True)
        cfg_b = BotConfig(entry_mode="sweep_reclaim", m1_entry_assist=True)
        cfg_c = BotConfig(entry_mode="sweep_reclaim", m1_entry_assist=True,
                          m1_reclaim_max_bars=5)
        hash_a = effective_config_snapshot(cfg_a, {})["config_hash"]
        hash_b = effective_config_snapshot(cfg_b, {})["config_hash"]
        hash_c = effective_config_snapshot(cfg_c, {})["config_hash"]
        self.assertEqual(hash_a, hash_b)
        self.assertNotEqual(hash_a, hash_c)

    def test_snapshot_files_written_and_free_of_secrets(self):
        import tempfile
        cfg = BotConfig(entry_mode="sweep_reclaim", m1_entry_assist=True)
        directory = Path(tempfile.mkdtemp())
        json_path, txt_path = write_config_snapshots(directory, "labelx", cfg,
                                                     {"run_label": "labelx"})
        self.assertTrue(json_path.exists() and txt_path.exists())
        self.assertTrue(json_path.name.startswith("labelx"))
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["run_label"], "labelx")
        blob = json_path.read_text(encoding="utf-8").lower()
        for secret in ("password", "token", "api_key", "secret"):
            self.assertNotIn(secret, blob)

    def test_config_only_prints_report_and_runs_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tick_cli.main(["--month", "2026-01",
                                  "--entry-mode", "sweep_reclaim",
                                  "--m1-entry-assist", "--m1-assist-mode", "entry",
                                  "--config-only"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("EFFECTIVE STRATEGY CONFIGURATION", text)
        self.assertIn("M1 Entry Assist", text)
        self.assertNotIn("ticks:", text)                  # no backtest ran


if __name__ == "__main__":
    unittest.main()
