"""Central feature-flag registry, effective-config report and snapshots.

Single source of truth for what each behaviour flag does: the startup
report, the config snapshot files and (indirectly) reviewer docs all read
THIS registry instead of hand-copying descriptions across files. Parser
help texts stay in the argument definitions (argparse needs them at parse
time), but every flag listed here is marked experimental/stable, carries
its dependencies, and is diffed against the BotConfig defaults to produce
the [CHANGED]/[EXPERIMENTAL]/[WARNING] annotations.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .cli_experimental import stable_config_hash
from .config import BotConfig

# field -> registry record. "field" is the BotConfig attribute the flag
# drives; "detail_fields" are companion attributes shown while enabled.
FEATURE_FLAGS: dict[str, dict] = {
    "strong_choch_only": {
        "cli": "--strong-choch-only", "default": False, "category": "setup",
        "status": "stable",
        "impact": "BOS + top-quartile displacement CHoCH setups are eligible; weak CHoCH rejected.",
        "requires": [], "detail_fields": []},
    "entry_lifecycle_enabled": {
        "cli": "--lifecycle", "default": False, "category": "entry",
        "status": "experimental",
        "impact": "Require extension then retracement before arming a setup.",
        "requires": [], "detail_fields": []},
    "breakeven_trigger_r": {
        "cli": "--breakeven-at-r", "default": None, "category": "risk",
        "status": "stable",
        "impact": "Move SL to entry once when open MFE reaches the trigger R.",
        "requires": [], "detail_fields": []},
    "max_entry_pivot_age_bars": {
        "cli": "--max-entry-pivot-age", "default": None, "category": "setup",
        "status": "experimental",
        "impact": "Reject setups whose confirming entry pivot is older than N closed M5 bars.",
        "requires": [], "detail_fields": []},
    "sweep_reclaim_max_bars": {
        "cli": "--sweep-reclaim-max-bars", "default": 1, "category": "entry",
        "status": "experimental",
        "impact": "Reclaim may lag the sweep by up to N closed M5 bars (1 = legacy same-bar).",
        "requires": [], "detail_fields": []},
    "revival_shadow_audit": {
        "cli": "--revival-shadow-audit", "default": False, "category": "audit",
        "status": "experimental",
        "impact": "Observe-only audit of trend-cancelled setups; never trades.",
        "requires": [], "detail_fields": []},
    "controlled_revival": {
        "cli": "--controlled-revival", "default": False, "category": "entry",
        "status": "experimental",
        "impact": "Trend-cancelled BOS/strong-CHoCH setups may revive once via a brand-new order after the trend returns and a fresh sweep+reclaim confirms.",
        "requires": ["entry_mode=sweep_reclaim"],
        "detail_fields": ["revival_max_return_bars", "revival_require_fresh_sweep",
                          "revival_max_per_ob"]},
    "allow_ob_reentry": {
        "cli": "--allow-ob-reentry", "default": False, "category": "entry",
        "status": "experimental",
        "impact": "One fully re-validated second entry per OB after the previous trade closed (fresh sweep required).",
        "requires": ["entry_mode=sweep_reclaim"],
        "detail_fields": ["max_entries_per_ob", "min_reentry_wait_bars",
                          "reentry_require_fresh_sweep", "reentry_after_loss_only"]},
    "wait_for_spread_after_confirmation": {
        "cli": "--wait-for-spread-after-confirmation", "default": False,
        "category": "execution", "status": "experimental",
        "impact": "Spread-blocked confirmed orders wait in a bounded waiting_spread state instead of retrying forever.",
        "requires": ["one of --spread-wait-max-seconds/ticks/bars"],
        "detail_fields": ["spread_wait_max_seconds", "spread_wait_max_ticks",
                          "spread_wait_max_bars"]},
    "portfolio_risk_cap": {
        "cli": "--portfolio-risk-cap", "default": None, "category": "risk",
        "status": "experimental",
        "impact": "Reject fills whose projected total open risk exceeds the cap (reject-only, no downsizing).",
        "requires": [], "detail_fields": []},
    "m1_entry_assist": {
        "cli": "--m1-entry-assist", "default": False, "category": "entry",
        "status": "experimental",
        "impact": "Allows M1 sweep/reclaim to time entries on active M5 setups (M5 stays the only trend/structure source).",
        "requires": ["entry_mode=sweep_reclaim", "tick data"],
        "detail_fields": ["m1_assist_mode", "m1_reclaim_max_bars",
                          "m1_max_confirmations_per_ob", "m1_entry_expiry_bars",
                          "m1_require_closed_bar", "m1_use_sequence_validation",
                          "m1_max_lead_minutes", "m1_risk_multiplier"]},
    "m1_refine_stop": {
        "cli": "--m1-refine-stop", "default": False, "category": "risk",
        "status": "experimental",
        "impact": "Stops behind the real M1 sweep extreme (+buffer) instead of the M5 stop; kept separate from entry assist.",
        "requires": ["--m1-entry-assist"],
        "detail_fields": ["m1_stop_atr_buffer"]},
    "demo_orders_enabled": {
        "cli": "--demo-orders", "default": False, "category": "execution",
        "status": "stable",
        "impact": "Mirror paper entries/exits to an MT5 DEMO account (refuses non-demo).",
        "requires": [], "detail_fields": []},
}

_RISKY_COMBOS = (
    (lambda cfg: cfg.max_open_positions > 1 and cfg.portfolio_risk_cap is None,
     "max_open_positions > 1 WITHOUT a portfolio risk cap: concurrent fills "
     "multiply open risk unboundedly."),
    (lambda cfg: cfg.m1_refine_stop,
     "M1 stop refinement changes stop distances AND entry timing together; "
     "compare against plain --m1-entry-assist before trusting results."),
    (lambda cfg: cfg.demo_orders_enabled,
     "Demo order mirroring is ON: MT5 orders will be sent to the demo account."),
)


def add_runtime_arguments(parser) -> None:
    parser.add_argument("--config-only", action="store_true",
                        help="parse+validate everything, print the effective "
                             "configuration and exit successfully without touching "
                             "any data, connection or state")
    parser.add_argument("--decision-log", action="store_true",
                        help="write every setup/entry decision event to the "
                             "structured events.jsonl next to the run outputs")
    parser.add_argument("--debug-m1", action="store_true",
                        help="verbose M1 diagnostics (every sweep/reclaim "
                             "detection) in events.jsonl -- debugging only")


def _non_default_fields(cfg: BotConfig) -> dict:
    defaults = BotConfig()
    changed = {}
    for field in dataclasses.fields(BotConfig):
        if not field.init or field.name in ("db_path", "trades_csv", "summary_csv",
                                            "log_path", "market_capture_dir",
                                            "daily_reports_dir"):
            continue
        current = getattr(cfg, field.name)
        if current != getattr(defaults, field.name):
            changed[field.name] = current
    return changed


def effective_config_report(cfg: BotConfig, run_info: dict) -> str:
    changed = _non_default_fields(cfg)
    bar = "=" * 60
    thin = "-" * 60
    lines = [bar, "EFFECTIVE STRATEGY CONFIGURATION", bar, ""]
    core = [("Symbol", cfg.symbol),
            ("Data Source", run_info.get("data_source", "Tick")),
            ("Strategy Timeframe", "M5"),
            ("Derived Timeframes", "M1, M5" if cfg.m1_entry_assist else "M5"),
            ("Entry Mode", cfg.entry_mode),
            ("Trend Swing", cfg.trend_swing_length),
            ("Entry Pivot", f"{cfg.entry_pivot_left}x{cfg.entry_pivot_right}"),
            ("RR", cfg.rr),
            ("Risk Fraction", f"{cfg.risk_fraction * 100:.2f}%"),
            ("Max Positions", cfg.max_open_positions),
            ("Allowed Breaks", "+".join(cfg.allowed_break_kinds))]
    for name, value in core:
        lines.append(f"{name}: {value}")
    lines += ["", thin, "ENABLED FEATURES", thin]
    disabled = []
    for field, spec in FEATURE_FLAGS.items():
        value = getattr(cfg, field, None)
        # Boolean flags are "on" when True; value flags (numbers/None) are
        # "on" only when they DIFFER from the stock default (e.g. the sweep
        # window at its legacy value of 1 is not an enabled feature).
        enabled = (bool(value) if isinstance(spec["default"], bool)
                   else value != spec["default"])
        title = field.replace("_", " ").title()
        if not enabled:
            disabled.append(f"[OFF] {title}")
            continue
        marks = ""
        if spec["status"] == "experimental":
            marks += " [EXPERIMENTAL]"
        if field in changed or spec["default"] != value:
            marks += " [CHANGED]"
        lines.append(f"[ON] {title}{marks}")
        lines.append(f"     Effect: {spec['impact']}")
        if not isinstance(value, bool):
            lines.append(f"     Value: {value}")
        for detail in spec["detail_fields"]:
            detail_value = getattr(cfg, detail, None)
            flag = " [CHANGED]" if detail in changed else ""
            lines.append(f"     {detail}: {detail_value}{flag}")
        if spec["requires"]:
            lines.append(f"     Requires: {', '.join(spec['requires'])}")
    lines += disabled
    other_changed = {k: v for k, v in changed.items()
                     if k not in FEATURE_FLAGS and
                     not any(k in s["detail_fields"] for s in FEATURE_FLAGS.values())}
    if other_changed:
        lines += ["", thin, "NON-DEFAULT PARAMETERS [CHANGED]", thin]
        for key, value in sorted(other_changed.items()):
            lines.append(f"{key} = {value}")
    lines += ["", thin, "SAFETY LIMITS", thin,
              "M5 Trend Required: yes",
              "Weak CHoCH Allowed: " + ("no" if (cfg.strong_choch_only or
                                                 "CHoCH" not in cfg.allowed_break_kinds)
                                        else "YES [WARNING]"),
              "Free Market Entry: no",
              "Portfolio Risk Cap: " + (f"{cfg.portfolio_risk_cap}" if
                                        cfg.portfolio_risk_cap is not None else "disabled"),
              "Demo Orders: " + ("ENABLED" if cfg.demo_orders_enabled else "disabled"),
              "Real Orders: unavailable"]
    warnings = [message for check, message in _RISKY_COMBOS if check(cfg)]
    if warnings:
        lines += ["", thin, "WARNINGS", thin]
        lines += [f"[WARNING] {message}" for message in warnings]
    lines += ["", thin, "OUTPUT", thin]
    for key in ("run_label", "output_directory", "config_snapshot",
                "trades_csv", "events_log"):
        if run_info.get(key):
            lines.append(f"{key.replace('_', ' ').title()}: {run_info[key]}")
    lines.append(bar)
    return "\n".join(lines)


def effective_config_snapshot(cfg: BotConfig, run_info: dict) -> dict:
    changed = _non_default_fields(cfg)
    config_dict = {}
    for field in dataclasses.fields(BotConfig):
        if not field.init:
            continue
        value = getattr(cfg, field.name)
        config_dict[field.name] = str(value) if isinstance(value, Path) else value
    flags_enabled = {}
    flags_disabled = {}
    for field, spec in FEATURE_FLAGS.items():
        value = getattr(cfg, field, None)
        record = {"cli": spec["cli"], "status": spec["status"],
                  "category": spec["category"], "impact": spec["impact"],
                  "requires": spec["requires"], "value": value,
                  "default": spec["default"]}
        enabled = (bool(value) if isinstance(spec["default"], bool)
                   else value != spec["default"])
        (flags_enabled if enabled else flags_disabled)[field] = record
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "raw_cli_arguments": run_info.get("argv", sys.argv[1:]),
        "effective_config": config_dict,
        "feature_flags": {"enabled": flags_enabled, "disabled": flags_disabled},
        "non_default_values": {k: (str(v) if isinstance(v, Path) else v)
                               for k, v in changed.items()},
        "experimental_enabled": [f for f, s in FEATURE_FLAGS.items()
                                 if s["status"] == "experimental" and getattr(cfg, f, None)],
        "run_label": run_info.get("run_label"),
        "config_hash": stable_config_hash(config_dict),
        "data_source": run_info.get("data_source", "Tick"),
        "symbol": cfg.symbol,
        "timeframes": ["M1", "M5"] if cfg.m1_entry_assist else ["M5"],
        "output_paths": {k: str(v) for k, v in run_info.items()
                         if k.endswith(("_directory", "_csv", "_log", "snapshot"))},
        "code_version": _git_sha(),
    }


def write_config_snapshots(directory: Path, label: str, cfg: BotConfig,
                           run_info: dict) -> tuple[Path, Path]:
    """Write {label}_effective_config.json/.txt (label-prefixed so different
    runs can never overwrite each other's snapshots)."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{label}_effective_config.json"
    txt_path = directory / f"{label}_effective_config.txt"
    json_path.write_text(json.dumps(effective_config_snapshot(cfg, run_info),
                                    indent=1, sort_keys=True, default=str) + "\n",
                         encoding="utf-8")
    txt_path.write_text(effective_config_report(cfg, run_info) + "\n",
                        encoding="utf-8")
    return json_path, txt_path


def write_events_jsonl(path: Path, events: list[dict]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")


def _git_sha() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=5,
                                cwd=Path(__file__).resolve().parent)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
