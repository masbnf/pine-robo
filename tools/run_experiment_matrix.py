#!/usr/bin/env python3
"""Run an experiment matrix of tick backtests from a JSON scenario config.

Usage:
  python tools/run_experiment_matrix.py experiments/m1_matrix_0306.json --dry-run
  python tools/run_experiment_matrix.py experiments/m1_matrix_0306.json --only m1seq
  python tools/run_experiment_matrix.py experiments/m1_matrix_0306.json --skip-existing

Each scenario becomes one tools/run_tick_backtest.py invocation over the
configured months via --months (ONE continuous replay -- never one command
per month, never --month all). Existing outputs are never overwritten unless
--force is passed; a scenario counts as existing when either its
tick_<run_label>_summary.csv or its _summaries/<scenario>.json is present.

Per-scenario stdout/stderr go to <report_root>/logs/, run state to
<report_root>/matrix_state.json, and each successful run's final stats block
is normalized into the existing flat-summary format at
<summary_root>/<scenario>.json (keys plus _config_name/_elapsed_s/_label).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_CONFIG_KEYS = ("matrix_name", "symbol", "months", "output_root",
                        "summary_root", "report_root", "common_args", "scenarios")

# Owned by the runner itself; a config that re-specifies one of these would
# produce duplicate/conflicting argv entries for run_tick_backtest.py.
FORBIDDEN_CONFIG_ARGS = ("--month", "--months", "--out", "--run-label", "--symbol")

_SUMMARY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*): (.*)$")


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def load_matrix_config(path: Path) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED_CONFIG_KEYS if key not in config]
    if missing:
        raise ValueError(f"matrix config missing keys: {', '.join(missing)}")
    if not config["months"] or not isinstance(config["months"], list):
        raise ValueError("matrix config 'months' must be a non-empty list")
    if not config["scenarios"]:
        raise ValueError("matrix config 'scenarios' must be a non-empty list")
    seen = set()
    for scenario in config["scenarios"]:
        for key in ("name", "run_label", "args"):
            if key not in scenario:
                raise ValueError(f"scenario missing '{key}': {scenario!r}")
        if scenario["name"] in seen:
            raise ValueError(f"duplicate scenario name: {scenario['name']}")
        seen.add(scenario["name"])
        _reject_forbidden_args(scenario["args"], f"scenario {scenario['name']}")
    _reject_forbidden_args(config["common_args"], "common_args")
    return config


def _reject_forbidden_args(args: list, where: str) -> None:
    bad = [arg for arg in args if arg in FORBIDDEN_CONFIG_ARGS]
    if bad:
        raise ValueError(f"{where} may not set {', '.join(bad)}; "
                         "the matrix runner owns these flags")


def scenario_files(config: dict, scenario: dict, root: Path = ROOT) -> dict:
    out_root = resolve_path(root, config["output_root"])
    summary_root = resolve_path(root, config["summary_root"])
    report_root = resolve_path(root, config["report_root"])
    label = scenario["run_label"]
    return {
        "summary_csv": out_root / f"tick_{label}_summary.csv",
        "trades_csv": out_root / f"tick_{label}_trades.csv",
        "breakdown_csv": out_root / f"tick_{label}_breakdown.csv",
        "report_html": out_root / f"tick_{label}_report.html",
        "events_jsonl": out_root / f"tick_{label}_events.jsonl",
        "summary_json": summary_root / f"{scenario['name']}.json",
        "stdout_log": report_root / "logs" / f"{scenario['name']}.stdout.log",
        "stderr_log": report_root / "logs" / f"{scenario['name']}.stderr.log",
    }


def existing_outputs(files: dict) -> tuple[bool, bool]:
    """(summary CSV exists, normalized _summaries JSON exists)."""
    return files["summary_csv"].exists(), files["summary_json"].exists()


def build_scenario_command(config: dict, scenario: dict,
                           python: str | None = None) -> list[str]:
    command = [python or sys.executable,
               str(Path("tools") / "run_tick_backtest.py"),
               "--symbol", config["symbol"],
               "--months", ",".join(config["months"]),
               "--out", config["output_root"],
               "--run-label", scenario["run_label"],
               *config["common_args"], *scenario["args"]]
    # Dashboard ports collide across concurrent runs; matrix runs are headless.
    if "--no-dashboard" not in command:
        command.append("--no-dashboard")
    return command


def collect_git_metadata(root: Path = ROOT) -> dict:
    meta = {"git_commit": None, "git_dirty": None}
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                capture_output=True, text=True, timeout=15)
        if commit.returncode == 0:
            meta["git_commit"] = commit.stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                capture_output=True, text=True, timeout=15)
        if status.returncode == 0:
            meta["git_dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return meta


def _parse_scalar(raw: str):
    text = raw.strip()
    if text == "None":
        return None
    if text == "True":
        return True
    if text == "False":
        return False
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            continue
    return text


def parse_summary_block(stdout_text: str) -> dict:
    """run_tick_backtest.py prints its stats dict as `key: value` lines at the
    very end of the run; collect only that trailing contiguous block so config
    report lines earlier in the log cannot leak in."""
    pairs = []
    for line in reversed(stdout_text.strip().splitlines()):
        match = _SUMMARY_LINE.match(line.strip())
        if not match:
            break
        pairs.append((match.group(1), _parse_scalar(match.group(2))))
    return dict(reversed(pairs))


def write_summary_json(path: Path, stats: dict, scenario_name: str,
                       run_label: str, elapsed_s: float | None) -> None:
    payload = dict(stats)
    payload["_config_name"] = scenario_name
    payload["_label"] = run_label
    payload["_elapsed_s"] = round(elapsed_s, 1) if elapsed_s is not None else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MatrixState:
    """matrix_state.json holder; safe for concurrent scenario updates."""

    def __init__(self, path: Path, matrix_name: str, order: list[str]):
        self.path = path
        self.matrix_name = matrix_name
        self.order = order
        self.entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def update(self, name: str, **fields) -> None:
        with self._lock:
            self.entries.setdefault(name, {"scenario_name": name}).update(fields)
            self._save()

    def _save(self) -> None:
        payload = {
            "matrix_name": self.matrix_name,
            "updated_at": _utcnow(),
            "scenarios": [self.entries[name] for name in self.order
                          if name in self.entries],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        tmp.replace(self.path)


def _run_scenario(config: dict, scenario: dict, files: dict, command: list[str],
                  state: MatrixState, force: bool) -> str:
    name = scenario["name"]
    state.update(name, status="running", start_time=_utcnow())
    started = time.monotonic()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    files["stdout_log"].parent.mkdir(parents=True, exist_ok=True)
    with open(files["stdout_log"], "w", encoding="utf-8") as out_handle, \
            open(files["stderr_log"], "w", encoding="utf-8") as err_handle:
        result = subprocess.run(command, cwd=ROOT, env=env,
                                stdout=out_handle, stderr=err_handle)
    duration = time.monotonic() - started
    status = "completed" if result.returncode == 0 else "failed"
    if status == "completed":
        stdout_text = files["stdout_log"].read_text(encoding="utf-8",
                                                    errors="replace")
        stats = parse_summary_block(stdout_text)
        if "trades" in stats:
            if force or not files["summary_json"].exists():
                write_summary_json(files["summary_json"], stats, name,
                                   scenario["run_label"], duration)
        else:
            print(f"[{name}] warning: no summary block found in stdout; "
                  f"_summaries JSON not written", file=sys.stderr)
    state.update(name, status=status, end_time=_utcnow(),
                 duration_seconds=round(duration, 1),
                 return_code=result.returncode)
    print(f"[{name}] {status} in {duration/60:.1f} min "
          f"(return code {result.returncode})")
    return status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="matrix config JSON")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the commands without executing anything")
    parser.add_argument("--only", metavar="SCENARIO",
                        help="run a single scenario by name")
    parser.add_argument("--skip-existing", action="store_true",
                        help="silently skip scenarios whose outputs already exist "
                             "(the default also skips them, but warns)")
    parser.add_argument("--force", action="store_true",
                        help="rerun scenarios even when outputs already exist")
    parser.add_argument("--jobs", type=int, default=1,
                        help="concurrent scenario runs (default: 1)")
    args = parser.parse_args(argv)
    if args.force and args.skip_existing:
        parser.error("--force and --skip-existing are mutually exclusive")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    try:
        config = load_matrix_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    scenarios = config["scenarios"]
    if args.only:
        scenarios = [s for s in scenarios if s["name"] == args.only]
        if not scenarios:
            parser.error(f"unknown scenario: {args.only}")

    plans = []
    for scenario in scenarios:
        files = scenario_files(config, scenario)
        csv_exists, json_exists = existing_outputs(files)
        exists = csv_exists or json_exists
        will_run = args.force or not exists
        plans.append((scenario, files, csv_exists, json_exists, will_run))

    if args.dry_run:
        for scenario, files, csv_exists, json_exists, will_run in plans:
            command = build_scenario_command(config, scenario)
            verb = "RUN " if will_run else "SKIP"
            print(f"{verb} {scenario['name']}: "
                  f"{subprocess.list2cmdline(command)}")
            if not will_run:
                print(f"     existing outputs: summary_csv={csv_exists} "
                      f"summary_json={json_exists}")
        return 0

    report_root = resolve_path(ROOT, config["report_root"])
    state = MatrixState(report_root / "matrix_state.json",
                        config["matrix_name"], [s["name"] for s, *_ in plans])
    git_meta = collect_git_metadata()

    to_run = []
    for scenario, files, csv_exists, json_exists, will_run in plans:
        command = build_scenario_command(config, scenario)
        base = {
            "run_label": scenario["run_label"],
            "command": subprocess.list2cmdline(command),
            "start_time": None, "end_time": None,
            "duration_seconds": None, "return_code": None,
            "summary_csv": str(files["summary_csv"]),
            "summary_json": str(files["summary_json"]),
            "stdout_log": str(files["stdout_log"]),
            "stderr_log": str(files["stderr_log"]),
            **git_meta,
        }
        if not will_run:
            if not args.skip_existing:
                print(f"[{scenario['name']}] outputs already exist; skipping "
                      f"(use --force to rerun)")
            state.update(scenario["name"], status="skipped", **base)
        else:
            state.update(scenario["name"], status="pending", **base)
            to_run.append((scenario, files, command))

    failures = 0
    if to_run:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(
                lambda item: _run_scenario(config, item[0], item[1], item[2],
                                           state, args.force),
                to_run))
        failures = sum(1 for status in results if status == "failed")

    ran = len(to_run)
    skipped = len(plans) - ran
    print(f"matrix done: {ran - failures} completed, {failures} failed, "
          f"{skipped} skipped -> state: {state.path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
