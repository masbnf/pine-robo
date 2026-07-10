"""Experiment-matrix runner and aggregator tests.

Run with:  pytest tests/test_experiment_matrix.py -v
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import run_experiment_matrix as matrix_cli
from tools import run_tick_backtest as tick_cli
from tools import summarize_experiment_matrix as summarize_cli

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = REPO_ROOT / "experiments" / "m1_matrix_0306.json"

EXPECTED_LABELS = {
    "baseline": "XAUUSD_0306_t9_p3x3",
    "m1shadow_r1": "XAUUSD_0306_t9_p3x3_m1shadow_m1r1",
    "m1shadow_r2": "XAUUSD_0306_t9_p3x3_m1shadow_m1r2",
    "m1shadow_r3": "XAUUSD_0306_t9_p3x3_m1shadow_m1r3",
    "m1shadow_r5": "XAUUSD_0306_t9_p3x3_m1shadow_m1r5",
    # sequence mode's auto-label carries no m1rN suffix; the completed run on
    # disk is tick_XAUUSD_0306_t9_p3x3_m1seq_* so the config matches that.
    "m1seq": "XAUUSD_0306_t9_p3x3_m1seq",
    "m1entry_r1": "XAUUSD_0306_t9_p3x3_m1entry_m1r1",
    "m1entry_r2": "XAUUSD_0306_t9_p3x3_m1entry_m1r2",
    "m1entry_r3": "XAUUSD_0306_t9_p3x3_m1entry_m1r3",
    "m1entry_r5": "XAUUSD_0306_t9_p3x3_m1entry_m1r5",
}

HEADLINE_BASELINE = {
    "trades": 126, "wins": 60, "win_rate": 0.47619047619047616,
    "net_pnl": 2409.060000000019, "total_r": 25.22254194796843,
    "profit_factor": 1.2912560087435077,
    "max_drawdown_pct": 10.034746059217035, "equity": 12409.06,
}

SHADOW_ARGS = ["--m1-entry-assist", "--m1-assist-mode", "shadow",
               "--m1-reclaim-max-bars", "1"]
SEQ_ARGS = ["--m1-entry-assist", "--m1-assist-mode", "sequence",
            "--m1-reclaim-max-bars", "3"]


def entry_args(reclaim_bars: int) -> list[str]:
    return ["--m1-entry-assist", "--m1-assist-mode", "entry",
            "--m1-reclaim-max-bars", str(reclaim_bars)]


def scenario(name: str, label: str, args: list[str]) -> dict:
    return {"name": name, "run_label": label, "args": list(args), "tags": []}


class MatrixTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out_root = self.root / "outputs"
        self.summary_root = self.out_root / "_summaries"
        self.report_root = self.root / "reports"
        self.summary_root.mkdir(parents=True)

    def write_config(self, scenarios: list[dict], **overrides):
        config = {
            "matrix_name": "test_matrix",
            "symbol": "XAUUSD",
            "months": ["2026-03", "2026-04"],
            "output_root": self.out_root.as_posix(),
            "summary_root": self.summary_root.as_posix(),
            "report_root": self.report_root.as_posix(),
            "baseline_scenario": "baseline",
            "baseline_acceptance": {"trades": 126, "total_r": 25.2225,
                                    "profit_factor": 1.291,
                                    "max_drawdown_pct": 10.0347,
                                    "tolerance": 0.005},
            "common_args": ["--fixed-risk", "--no-dashboard"],
            "scenarios": scenarios,
        }
        config.update(overrides)
        path = self.root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path, config

    def write_summary_json(self, name: str, label: str, stats: dict) -> None:
        payload = {**stats, "_config_name": name, "_label": label,
                   "_elapsed_s": 1.0}
        (self.summary_root / f"{name}.json").write_text(
            json.dumps(payload), encoding="utf-8")

    def write_summary_csv(self, label: str, stats: dict) -> Path:
        keys = ("trades", "wins", "win_rate", "net_pnl", "total_r",
                "profit_factor", "max_drawdown_pct", "equity")
        path = self.out_root / f"tick_{label}_summary.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(",".join(keys) + "\n"
                        + ",".join(str(stats[key]) for key in keys) + "\n",
                        encoding="utf-8")
        return path


class ConfigParsingTests(MatrixTestCase):
    def test_real_config_parses_with_expected_labels(self):
        config = matrix_cli.load_matrix_config(REAL_CONFIG)
        self.assertEqual(config["matrix_name"], "m1_matrix_0306")
        self.assertEqual(config["months"],
                         ["2026-03", "2026-04", "2026-05", "2026-06"])
        labels = {s["name"]: s["run_label"] for s in config["scenarios"]}
        self.assertEqual(labels, EXPECTED_LABELS)
        self.assertIn("--no-dashboard", config["common_args"])
        self.assertIn("--strong-choch-only", config["common_args"])

    def test_missing_keys_rejected(self):
        path = self.root / "bad.json"
        path.write_text(json.dumps({"matrix_name": "x"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing keys"):
            matrix_cli.load_matrix_config(path)

    def test_forbidden_args_rejected(self):
        path, _ = self.write_config(
            [scenario("baseline", "L0", [])],
            common_args=["--month", "2026-01"])
        with self.assertRaisesRegex(ValueError, "--month"):
            matrix_cli.load_matrix_config(path)
        path, _ = self.write_config(
            [scenario("baseline", "L0", ["--run-label", "evil"])])
        with self.assertRaisesRegex(ValueError, "--run-label"):
            matrix_cli.load_matrix_config(path)

    def test_duplicate_scenario_names_rejected(self):
        path, _ = self.write_config([scenario("a", "L1", []),
                                     scenario("a", "L2", [])])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            matrix_cli.load_matrix_config(path)


class CommandConstructionTests(MatrixTestCase):
    def test_uses_months_never_month(self):
        _, config = self.write_config(
            [scenario("m1shadow_r1", "LBL_shadow", SHADOW_ARGS)])
        command = matrix_cli.build_scenario_command(
            config, config["scenarios"][0])
        self.assertEqual(command.count("--months"), 1)
        self.assertEqual(command[command.index("--months") + 1],
                         "2026-03,2026-04")
        self.assertNotIn("--month", command)

    def test_no_dashboard_appended_when_absent(self):
        _, config = self.write_config(
            [scenario("baseline", "L0", [])], common_args=["--fixed-risk"])
        command = matrix_cli.build_scenario_command(
            config, config["scenarios"][0])
        self.assertEqual(command.count("--no-dashboard"), 1)

    def test_explicit_run_label_and_out(self):
        _, config = self.write_config(
            [scenario("m1seq", "LBL_seq", SEQ_ARGS)])
        command = matrix_cli.build_scenario_command(
            config, config["scenarios"][0])
        self.assertEqual(command[command.index("--run-label") + 1], "LBL_seq")
        self.assertEqual(command[command.index("--out") + 1],
                         config["output_root"])
        for arg in SEQ_ARGS:
            self.assertIn(arg, command)


class MonthsFlagTests(unittest.TestCase):
    def test_months_accepted_and_month_still_works(self):
        parser = tick_cli.build_parser()
        args = parser.parse_args(["--months", "2026-03,2026-04"])
        self.assertEqual(args.months, "2026-03,2026-04")
        self.assertIsNone(args.month)
        args = parser.parse_args(["--month", "2026-01"])
        self.assertEqual(args.month, "2026-01")

    def test_month_and_months_mutually_exclusive(self):
        parser = tick_cli.build_parser()
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["--month", "2026-01", "--months", "2026-03"])
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args([])

    def test_duplicate_months_rejected(self):
        parser = tick_cli.build_parser()
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            tick_cli.parse_months_list("2026-03,2026-03", parser)

    def test_label_token_matches_existing_0306_naming(self):
        self.assertEqual(tick_cli.months_label_token(
            ["2026-03", "2026-04", "2026-05", "2026-06"]), "0306")


class ExistingOutputDetectionTests(MatrixTestCase):
    def test_detects_summary_csv_and_summaries_json(self):
        _, config = self.write_config(
            [scenario("baseline", "L0", [])])
        files = matrix_cli.scenario_files(config, config["scenarios"][0])
        self.assertEqual(matrix_cli.existing_outputs(files), (False, False))
        self.write_summary_csv("L0", HEADLINE_BASELINE)
        self.assertEqual(matrix_cli.existing_outputs(files), (True, False))
        self.write_summary_json("baseline", "L0", HEADLINE_BASELINE)
        self.assertEqual(matrix_cli.existing_outputs(files), (True, True))

    def test_skip_existing_marks_state_and_runs_nothing(self):
        path, _ = self.write_config([scenario("baseline", "L0", [])])
        self.write_summary_json("baseline", "L0", HEADLINE_BASELINE)
        with mock.patch.object(matrix_cli, "collect_git_metadata",
                               return_value={"git_commit": "abc",
                                             "git_dirty": False}), \
                mock.patch.object(matrix_cli.subprocess, "run",
                                  side_effect=AssertionError("must not run")), \
                contextlib.redirect_stdout(io.StringIO()):
            code = matrix_cli.main([str(path), "--skip-existing"])
        self.assertEqual(code, 0)
        state = json.loads((self.report_root / "matrix_state.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(state["scenarios"][0]["status"], "skipped")

    def test_force_and_skip_existing_are_exclusive(self):
        path, _ = self.write_config([scenario("baseline", "L0", [])])
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            matrix_cli.main([str(path), "--force", "--skip-existing"])


class DryRunTests(MatrixTestCase):
    def test_dry_run_prints_commands_and_executes_nothing(self):
        path, _ = self.write_config(
            [scenario("baseline", "L0", []),
             scenario("m1shadow_r1", "L1", SHADOW_ARGS)])
        self.write_summary_json("m1shadow_r1", "L1", HEADLINE_BASELINE)
        out = io.StringIO()
        with mock.patch.object(matrix_cli.subprocess, "run",
                               side_effect=AssertionError("must not run")), \
                contextlib.redirect_stdout(out):
            code = matrix_cli.main([str(path), "--dry-run"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("RUN  baseline:", text)
        self.assertIn("--months 2026-03,2026-04", text)
        self.assertIn("SKIP m1shadow_r1:", text)
        self.assertFalse((self.report_root / "matrix_state.json").exists())

    def test_only_selects_single_scenario(self):
        path, _ = self.write_config(
            [scenario("baseline", "L0", []),
             scenario("m1seq", "L1", SEQ_ARGS)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = matrix_cli.main([str(path), "--dry-run", "--only", "m1seq"])
        self.assertEqual(code, 0)
        self.assertNotIn("baseline", out.getvalue())
        self.assertIn("m1seq", out.getvalue())
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            matrix_cli.main([str(path), "--dry-run", "--only", "nope"])


class RunnerExecutionTests(MatrixTestCase):
    def test_mocked_run_writes_state_and_summary_json(self):
        path, _ = self.write_config([scenario("solo", "L_solo", [])])

        def fake_run(command, cwd=None, env=None, stdout=None, stderr=None,
                     **kwargs):
            stdout.write("EFFECTIVE STRATEGY CONFIGURATION\n"
                         "months included: 2026-03, 2026-04 (10 files)\n"
                         "trades: 5\nwins: 3\nwin_rate: 0.6\nnet_pnl: 100.0\n"
                         "total_r: 2.5\nprofit_factor: 1.5\n"
                         "max_drawdown_pct: 4.0\nequity: 10100.0\n")
            return SimpleNamespace(returncode=0)

        with mock.patch.object(matrix_cli, "collect_git_metadata",
                               return_value={"git_commit": "deadbeef",
                                             "git_dirty": True}), \
                mock.patch.object(matrix_cli.subprocess, "run",
                                  side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()):
            code = matrix_cli.main([str(path)])
        self.assertEqual(code, 0)

        state = json.loads((self.report_root / "matrix_state.json")
                           .read_text(encoding="utf-8"))
        entry = state["scenarios"][0]
        self.assertEqual(entry["scenario_name"], "solo")
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["return_code"], 0)
        self.assertEqual(entry["git_commit"], "deadbeef")
        self.assertTrue(entry["git_dirty"])
        self.assertIsNotNone(entry["duration_seconds"])
        self.assertIn("--months", entry["command"])

        summary = json.loads((self.summary_root / "solo.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(summary["trades"], 5)
        self.assertEqual(summary["_config_name"], "solo")
        self.assertEqual(summary["_label"], "L_solo")

    def test_failed_run_marked_failed(self):
        path, _ = self.write_config([scenario("solo", "L_solo", [])])

        def fake_run(command, stdout=None, stderr=None, **kwargs):
            stderr.write("boom\n")
            return SimpleNamespace(returncode=3)

        with mock.patch.object(matrix_cli, "collect_git_metadata",
                               return_value={"git_commit": None,
                                             "git_dirty": None}), \
                mock.patch.object(matrix_cli.subprocess, "run",
                                  side_effect=fake_run), \
                contextlib.redirect_stdout(io.StringIO()):
            code = matrix_cli.main([str(path)])
        self.assertEqual(code, 1)
        state = json.loads((self.report_root / "matrix_state.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(state["scenarios"][0]["status"], "failed")
        self.assertFalse((self.summary_root / "solo.json").exists())


class GitMetadataTests(unittest.TestCase):
    def test_degrades_gracefully_when_git_fails(self):
        with mock.patch.object(matrix_cli.subprocess, "run",
                               side_effect=FileNotFoundError("no git")):
            meta = matrix_cli.collect_git_metadata()
        self.assertEqual(meta, {"git_commit": None, "git_dirty": None})


class SummaryBlockParsingTests(unittest.TestCase):
    def test_trailing_block_parsed_with_types(self):
        text = ("EFFECTIVE STRATEGY CONFIGURATION\n"
                "months included: 2026-03 (30 files)\n"
                "some progress line\n"
                "trades: 126\nwin_rate: 0.476\nm1_assist_mode: shadow\n"
                "portfolio_risk_cap: None\nm1_entry_assist: True\n")
        stats = matrix_cli.parse_summary_block(text)
        self.assertEqual(stats["trades"], 126)
        self.assertAlmostEqual(stats["win_rate"], 0.476)
        self.assertEqual(stats["m1_assist_mode"], "shadow")
        self.assertIsNone(stats["portfolio_risk_cap"])
        self.assertIs(stats["m1_entry_assist"], True)
        self.assertNotIn("months", stats)


class VerdictTests(unittest.TestCase):
    BASE = HEADLINE_BASELINE

    def _verdict(self, mode, stats):
        deltas = summarize_cli.compute_deltas(stats, self.BASE)
        verdict, _ = summarize_cli.classify_verdict(mode, stats, deltas,
                                                    self.BASE)
        return verdict

    def test_baseline_and_incomplete(self):
        self.assertEqual(summarize_cli.classify_verdict(
            "baseline", self.BASE, {}, self.BASE)[0], "baseline")
        self.assertEqual(summarize_cli.classify_verdict(
            "entry", None, {}, self.BASE)[0], "incomplete")

    def test_shadow_is_observe_only(self):
        self.assertEqual(self._verdict("shadow", dict(self.BASE)), "observe_only")

    def test_entry_small_sample_needs_more_data(self):
        stats = {**self.BASE, "trades": 150, "total_r": 30.0,
                 "m1_confirmations_filled": 8}
        self.assertEqual(self._verdict("entry", stats), "needs_more_data")

    def test_entry_candidate(self):
        stats = {**self.BASE, "trades": 140, "total_r": 28.5,
                 "profit_factor": 1.30, "max_drawdown_pct": 10.5,
                 "m1_confirmations_filled": 45}
        self.assertEqual(self._verdict("entry", stats), "candidate")

    def test_entry_reject_on_total_r_loss(self):
        stats = {**self.BASE, "trades": 150, "total_r": 20.0,
                 "profit_factor": 1.25, "max_drawdown_pct": 10.5,
                 "m1_confirmations_filled": 45}
        self.assertEqual(self._verdict("entry", stats), "reject")

    def test_reject_on_profit_factor_drop(self):
        stats = {**self.BASE, "trades": 150, "total_r": 25.0,
                 "profit_factor": 1.15, "max_drawdown_pct": 10.5,
                 "m1_confirmations_filled": 45}
        self.assertEqual(self._verdict("entry", stats), "reject")

    def test_sequence_candidate_filter(self):
        stats = {**self.BASE, "trades": 120, "total_r": 24.5,
                 "profit_factor": 1.35, "max_drawdown_pct": 9.8}
        self.assertEqual(self._verdict("sequence", stats), "candidate_filter")

    def test_sequence_not_meeting_thresholds(self):
        stats = {**self.BASE, "trades": 120, "total_r": 24.5,
                 "profit_factor": 1.30, "max_drawdown_pct": 9.8}
        self.assertEqual(self._verdict("sequence", stats), "needs_more_data")

    def test_entry_between_thresholds_needs_more_data(self):
        stats = {**self.BASE, "trades": 130, "total_r": 26.0,
                 "profit_factor": 1.29, "max_drawdown_pct": 10.2,
                 "m1_confirmations_filled": 45}
        self.assertEqual(self._verdict("entry", stats), "needs_more_data")


class AcceptanceCheckTests(MatrixTestCase):
    def test_passes_within_tolerance(self):
        _, config = self.write_config([scenario("baseline", "L0", [])])
        result = summarize_cli.baseline_acceptance_check(
            config, HEADLINE_BASELINE)
        self.assertTrue(result["checked"])
        self.assertTrue(result["passed"])

    def test_fails_on_trade_count_mismatch(self):
        _, config = self.write_config([scenario("baseline", "L0", [])])
        result = summarize_cli.baseline_acceptance_check(
            config, {**HEADLINE_BASELINE, "trades": 125})
        self.assertFalse(result["passed"])

    def test_unchecked_without_spec(self):
        _, config = self.write_config([scenario("baseline", "L0", [])])
        config.pop("baseline_acceptance")
        result = summarize_cli.baseline_acceptance_check(
            config, HEADLINE_BASELINE)
        self.assertFalse(result["checked"])


class AggregationTests(MatrixTestCase):
    def _build_matrix(self):
        scenarios = [
            scenario("baseline", "L_base", []),
            scenario("m1shadow_r1", "L_shadow", SHADOW_ARGS),
            scenario("m1entry_good", "L_good", entry_args(2)),
            scenario("m1entry_small", "L_small", entry_args(3)),
            scenario("m1entry_bad", "L_bad", entry_args(5)),
            scenario("m1seq", "L_seq", SEQ_ARGS),
            scenario("m1entry_missing", "L_missing", entry_args(1)),
            scenario("csv_only", "L_csv", entry_args(1)),
        ]
        path, config = self.write_config(scenarios)
        self.write_summary_json("baseline", "L_base", HEADLINE_BASELINE)
        # m1_reclaim_max_bars=99 poison value: config (1) must win over stats
        self.write_summary_json("m1shadow_r1", "L_shadow", {
            **HEADLINE_BASELINE, "m1_assist_mode": "shadow",
            "m1_entry_assist": True, "m1_reclaim_max_bars": 99,
            "m1_confirmations_total": 187, "m1_confirmations_shadow": 187,
            "m1_confirmations_filled": 0, "m1_shadow_total_r": 23.0,
            "m1_shadow_wins": 84, "m1_shadow_losses": 103,
            "m1_sequence_valid": 74, "m1_sequence_invalid": 81,
            "m1_sequence_ambiguous": 1, "m1_rejected_confirmation_cap": 239,
            "m1_rejected_reclaim_too_late": 98, "m1_total_r": 0.0,
            "m1_wins": 0, "m1_losses": 0, "m1_profit_factor": None})
        self.write_summary_json("m1entry_good", "L_good", {
            **HEADLINE_BASELINE, "trades": 140, "wins": 70, "win_rate": 0.5,
            "total_r": 28.5, "net_pnl": 2800.0, "profit_factor": 1.30,
            "max_drawdown_pct": 10.5, "m1_assist_mode": "entry",
            "m1_entry_assist": True, "m1_confirmations_filled": 45})
        self.write_summary_json("m1entry_small", "L_small", {
            **HEADLINE_BASELINE, "trades": 132, "total_r": 26.0,
            "m1_assist_mode": "entry", "m1_entry_assist": True,
            "m1_confirmations_filled": 8})
        self.write_summary_json("m1entry_bad", "L_bad", {
            **HEADLINE_BASELINE, "trades": 150, "total_r": 20.0,
            "profit_factor": 1.25, "m1_assist_mode": "entry",
            "m1_entry_assist": True, "m1_confirmations_filled": 45})
        self.write_summary_json("m1seq", "L_seq", {
            **HEADLINE_BASELINE, "trades": 120, "total_r": 24.5,
            "profit_factor": 1.35, "max_drawdown_pct": 9.8,
            "m1_assist_mode": "sequence", "m1_entry_assist": True})
        self.write_summary_csv("L_csv", HEADLINE_BASELINE)
        return path, config

    def test_rows_deltas_verdicts_and_outputs(self):
        _, config = self._build_matrix()
        with contextlib.redirect_stdout(io.StringIO()):
            rows, acceptance = summarize_cli.build_rows(config)
        by_name = {row["scenario_name"]: row for row in rows}

        self.assertTrue(acceptance["passed"])

        base = by_name["baseline"]
        self.assertEqual(base["verdict"], "baseline")
        self.assertEqual(base["delta_trades"], 0)
        self.assertEqual(base["delta_total_r"], 0)

        good = by_name["m1entry_good"]
        self.assertEqual(good["delta_trades"], 14)
        self.assertAlmostEqual(good["delta_total_r"], 3.277458, places=4)
        self.assertAlmostEqual(good["delta_profit_factor"], 0.008744, places=4)
        self.assertEqual(good["verdict"], "candidate")

        self.assertEqual(by_name["m1shadow_r1"]["verdict"], "observe_only")
        self.assertEqual(by_name["m1entry_small"]["verdict"], "needs_more_data")
        self.assertEqual(by_name["m1entry_bad"]["verdict"], "reject")
        self.assertEqual(by_name["m1seq"]["verdict"], "candidate_filter")
        self.assertEqual(by_name["m1entry_missing"]["verdict"], "incomplete")
        self.assertEqual(by_name["m1entry_missing"]["status"], "missing")

        # input parameter comes from scenario config, not the poisoned stats
        self.assertEqual(by_name["m1shadow_r1"]["m1_reclaim_max_bars"], 1)
        # M1 stats fields pass through when present, stay None when absent
        self.assertEqual(by_name["m1shadow_r1"]["m1_shadow_wins"], 84)
        self.assertIsNone(by_name["baseline"]["m1_confirmations_total"])

        # CSV-only run was normalized into the flat _summaries contract
        created = self.summary_root / "csv_only.json"
        self.assertTrue(created.exists())
        payload = json.loads(created.read_text(encoding="utf-8"))
        self.assertEqual(payload["trades"], 126)
        self.assertEqual(payload["_label"], "L_csv")
        self.assertEqual(by_name["csv_only"]["status"], "completed")

    def test_outputs_written_including_persian_section(self):
        path, config = self._build_matrix()
        with contextlib.redirect_stdout(io.StringIO()):
            code = summarize_cli.main([str(path)])
        self.assertEqual(code, 0)
        report_md = (self.report_root / "matrix_report.md").read_text(
            encoding="utf-8")
        self.assertIn("خلاصه و اقدام بعدی", report_md)
        self.assertIn("Baseline acceptance check", report_md)
        self.assertIn("candidate_filter", report_md)
        self.assertTrue((self.report_root / "matrix_summary.csv").exists())
        self.assertTrue((self.report_root / "matrix_summary.json").exists())
        self.assertTrue((self.report_root / "index.html").exists())
        summary = json.loads((self.report_root / "matrix_summary.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(len(summary["rows"]), 8)

    def test_aggregation_survives_missing_effective_config_snapshots(self):
        # Old runs predate *_effective_config.json; none exist in this fixture
        # and aggregation must not require them.
        _, config = self._build_matrix()
        snapshots = list(self.out_root.glob("*_effective_config.json"))
        self.assertEqual(snapshots, [])
        with contextlib.redirect_stdout(io.StringIO()):
            rows, _ = summarize_cli.build_rows(config)
        self.assertEqual(len(rows), 8)

    def test_missing_baseline_degrades_to_needs_more_data(self):
        scenarios = [scenario("baseline", "L_base", []),
                     scenario("m1entry_good", "L_good", entry_args(2))]
        _, config = self.write_config(scenarios)
        self.write_summary_json("m1entry_good", "L_good", {
            **HEADLINE_BASELINE, "trades": 140, "total_r": 28.5,
            "m1_assist_mode": "entry", "m1_confirmations_filled": 45})
        with contextlib.redirect_stdout(io.StringIO()):
            rows, acceptance = summarize_cli.build_rows(config)
        by_name = {row["scenario_name"]: row for row in rows}
        self.assertEqual(by_name["baseline"]["verdict"], "baseline")
        self.assertEqual(by_name["m1entry_good"]["verdict"], "needs_more_data")
        self.assertFalse(acceptance["passed"])


if __name__ == "__main__":
    unittest.main()
