#!/usr/bin/env python3
"""Aggregate experiment-matrix results into summary CSV/JSON, a Markdown
report and a static HTML dashboard.

Usage:
  python tools/summarize_experiment_matrix.py experiments/m1_matrix_0306.json

Reads the existing flat _summaries/<scenario>.json files (stats dict plus
_config_name/_elapsed_s/_label). When a scenario only has its
tick_<run_label>_summary.csv, the matching flat JSON is created from it so
the summary contract stays single-format. Missing scenarios become
verdict=incomplete rows instead of failing the report.

Outputs (under the config's report_root):
  matrix_summary.csv / matrix_summary.json / matrix_report.md / index.html
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.run_experiment_matrix import (load_matrix_config, resolve_path,
                                         scenario_files, write_summary_json)

REQUIRED_FIELDS = (
    "scenario_name", "run_label", "status", "mode", "m1_reclaim_max_bars",
    "trades", "wins", "win_rate", "total_r", "net_pnl", "profit_factor",
    "max_drawdown_pct",
    "delta_trades", "delta_total_r", "delta_profit_factor", "delta_win_rate",
    "delta_max_drawdown_pct", "delta_net_pnl",
    "verdict", "verdict_reason",
    "summary_path", "trades_path", "breakdown_path", "report_path",
    "events_path",
)

M1_FIELDS = (
    "m1_entry_assist", "m1_assist_mode", "m1_confirmations_total",
    "m1_confirmations_shadow", "m1_confirmations_filled",
    "m1_shadow_total_r", "m1_shadow_wins", "m1_shadow_losses",
    "m1_sequence_valid", "m1_sequence_invalid", "m1_sequence_ambiguous",
    "m1_rejected_confirmation_cap", "m1_rejected_reclaim_too_late",
    "m1_total_r", "m1_wins", "m1_losses", "m1_profit_factor",
    "m1_rejected_lead_too_long", "m1_avg_setup_to_confirm_minutes",
    "m1_max_setup_to_confirm_minutes",
)

DELTA_METRICS = ("trades", "total_r", "profit_factor", "win_rate",
                 "max_drawdown_pct", "net_pnl")


def scenario_flag_value(scenario: dict, flag: str):
    """Input parameters (e.g. --m1-reclaim-max-bars) come from the scenario
    config, never from summary stats."""
    args = scenario.get("args", [])
    if flag in args:
        index = args.index(flag)
        if index + 1 < len(args):
            value = args[index + 1]
            try:
                return int(value)
            except (TypeError, ValueError):
                return value
    return None


def scenario_mode(config: dict, scenario: dict) -> str:
    if scenario["name"] == config.get("baseline_scenario", "baseline"):
        return "baseline"
    mode = scenario_flag_value(scenario, "--m1-assist-mode")
    if mode is not None:
        return str(mode)
    if "--m1-entry-assist" in scenario.get("args", []):
        return "shadow"  # CLI default when --m1-assist-mode is omitted
    return "none"


def _cast_number(text: str):
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            continue
    return text


def summary_from_csv(path: Path) -> dict:
    with open(path, newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    return {key: _cast_number(value) for key, value in row.items()}


def read_scenario_summary(config: dict, scenario: dict, root: Path = ROOT,
                          create_missing_json: bool = True):
    """Return (stats dict or None, files dict). Falls back from the flat
    _summaries JSON to the run's summary CSV, normalizing the CSV into the
    JSON contract on the way."""
    files = scenario_files(config, scenario, root)
    if files["summary_json"].exists():
        return json.loads(files["summary_json"].read_text(encoding="utf-8")), files
    if files["summary_csv"].exists():
        stats = summary_from_csv(files["summary_csv"])
        if create_missing_json:
            write_summary_json(files["summary_json"], stats, scenario["name"],
                               scenario["run_label"], None)
            print(f"[{scenario['name']}] normalized summary CSV -> "
                  f"{files['summary_json']}")
        stats = dict(stats)
        stats["_config_name"] = scenario["name"]
        stats["_label"] = scenario["run_label"]
        stats["_elapsed_s"] = None
        return stats, files
    return None, files


def compute_deltas(stats: dict | None, baseline: dict | None) -> dict:
    deltas = {}
    for metric in DELTA_METRICS:
        value = stats.get(metric) if stats else None
        base = baseline.get(metric) if baseline else None
        deltas[f"delta_{metric}"] = (round(value - base, 6)
                                     if isinstance(value, (int, float))
                                     and isinstance(base, (int, float))
                                     else None)
    return deltas


def classify_verdict(mode: str, stats: dict | None, deltas: dict,
                     baseline: dict | None) -> tuple[str, str]:
    if mode == "baseline":
        return "baseline", "reference scenario"
    if stats is None:
        return "incomplete", "summary missing (scenario not run yet or failed)"
    if mode == "shadow":
        return "observe_only", "shadow mode observes only and never changes entries"
    if baseline is None:
        return "needs_more_data", "baseline summary missing; deltas unavailable"
    if mode == "entry":
        filled = stats.get("m1_confirmations_filled")
        if isinstance(filled, (int, float)) and filled < 20:
            return ("needs_more_data",
                    f"m1_confirmations_filled={int(filled)} < 20 "
                    "(small real-entry sample)")
    pf = stats.get("profit_factor")
    dd = stats.get("max_drawdown_pct")
    base_pf = baseline.get("profit_factor")
    base_dd = baseline.get("max_drawdown_pct")
    d_trades = deltas.get("delta_trades")
    d_total_r = deltas.get("delta_total_r")
    metrics = (pf, dd, base_pf, base_dd, d_trades, d_total_r)
    if any(not isinstance(value, (int, float)) for value in metrics):
        return "needs_more_data", "missing metrics prevent classification"
    if d_total_r < -2.0 or pf < base_pf - 0.10:
        return ("reject",
                f"delta_total_r={d_total_r:+.2f}R, profit_factor={pf:.3f} "
                f"vs baseline {base_pf:.3f}")
    if mode == "sequence":
        if (d_trades <= 0 and pf >= base_pf + 0.03 and dd <= base_dd
                and d_total_r >= -1.0):
            return ("candidate_filter",
                    f"filter: profit_factor {pf:.3f} >= baseline+0.03, "
                    f"drawdown {dd:.2f}% <= baseline, "
                    f"delta_total_r={d_total_r:+.2f}R")
        return ("needs_more_data",
                "sequence filter did not meet candidate_filter thresholds")
    if (d_trades >= 10 and d_total_r >= 0 and pf >= base_pf - 0.03
            and dd <= base_dd + 2.0):
        return ("candidate",
                f"+{int(d_trades)} trades, delta_total_r={d_total_r:+.2f}R, "
                f"profit_factor {pf:.3f}, drawdown {dd:.2f}%")
    return ("needs_more_data",
            f"delta_trades={int(d_trades):+d}, delta_total_r={d_total_r:+.2f}R "
            "did not meet candidate or reject thresholds")


def baseline_acceptance_check(config: dict, baseline: dict | None) -> dict:
    spec = config.get("baseline_acceptance")
    if not spec:
        return {"checked": False, "passed": None, "details": []}
    tolerance = spec.get("tolerance", 0.005)
    details = []
    for metric in ("trades", "total_r", "profit_factor", "max_drawdown_pct"):
        if metric not in spec:
            continue
        expected = spec[metric]
        actual = baseline.get(metric) if baseline else None
        if metric == "trades":
            passed = actual == expected
        else:
            passed = (isinstance(actual, (int, float))
                      and abs(actual - expected) <= tolerance)
        details.append({"metric": metric, "expected": expected,
                        "actual": actual, "passed": passed})
    return {"checked": True,
            "passed": bool(details) and all(d["passed"] for d in details),
            "details": details}


def _relpath(path: Path, start: Path) -> str:
    return os.path.relpath(path, start).replace("\\", "/")


def load_state_statuses(config: dict, root: Path = ROOT) -> dict:
    state_path = resolve_path(root, config["report_root"]) / "matrix_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return {entry["scenario_name"]: entry.get("status")
                for entry in state.get("scenarios", [])}
    except (OSError, ValueError, KeyError):
        return {}


def build_rows(config: dict, root: Path = ROOT,
               create_missing_json: bool = True) -> tuple[list[dict], dict]:
    baseline_name = config.get("baseline_scenario", "baseline")
    state_statuses = load_state_statuses(config, root)

    summaries = {}
    files_by_name = {}
    for scenario in config["scenarios"]:
        stats, files = read_scenario_summary(config, scenario, root,
                                             create_missing_json)
        summaries[scenario["name"]] = stats
        files_by_name[scenario["name"]] = files
    baseline_stats = summaries.get(baseline_name)

    rows = []
    for scenario in config["scenarios"]:
        name = scenario["name"]
        stats = summaries[name]
        files = files_by_name[name]
        mode = scenario_mode(config, scenario)
        deltas = compute_deltas(stats, baseline_stats)
        verdict, reason = classify_verdict(mode, stats, deltas, baseline_stats)
        if stats is not None:
            status = "completed"
        else:
            status = state_statuses.get(name) or "missing"
        row = {
            "scenario_name": name,
            "run_label": scenario["run_label"],
            "status": status,
            "mode": mode,
            "m1_reclaim_max_bars": scenario_flag_value(
                scenario, "--m1-reclaim-max-bars"),
            "trades": stats.get("trades") if stats else None,
            "wins": stats.get("wins") if stats else None,
            "win_rate": stats.get("win_rate") if stats else None,
            "total_r": stats.get("total_r") if stats else None,
            "net_pnl": stats.get("net_pnl") if stats else None,
            "profit_factor": stats.get("profit_factor") if stats else None,
            "max_drawdown_pct": stats.get("max_drawdown_pct") if stats else None,
            **deltas,
            "verdict": verdict,
            "verdict_reason": reason,
            "summary_path": _relpath(files["summary_csv"], root),
            "trades_path": _relpath(files["trades_csv"], root),
            "breakdown_path": _relpath(files["breakdown_csv"], root),
            "report_path": _relpath(files["report_html"], root),
            "events_path": _relpath(files["events_jsonl"], root),
        }
        for field in M1_FIELDS:
            row[field] = stats.get(field) if stats else None
        rows.append(row)

    acceptance = baseline_acceptance_check(config, baseline_stats)
    return rows, acceptance


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _md_table(headers: list[str], body: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(cells) + " |" for cells in body]
    return "\n".join(lines)


def _recommended_actions(config: dict, rows: list[dict],
                         acceptance: dict) -> list[str]:
    actions = []
    incomplete = [r["scenario_name"] for r in rows
                  if r["status"] not in ("completed",)]
    candidates = [r["scenario_name"] for r in rows
                  if r["verdict"] in ("candidate", "candidate_filter")]
    rejects = [r["scenario_name"] for r in rows if r["verdict"] == "reject"]
    if acceptance.get("checked") and acceptance.get("passed") is False:
        actions.append("Baseline acceptance check FAILED - investigate the "
                       "baseline summary before trusting any comparison below.")
    if incomplete:
        actions.append(
            f"Run the {len(incomplete)} remaining scenario(s) "
            f"({', '.join(incomplete)}): "
            f"`python tools/run_experiment_matrix.py "
            f"experiments/{config['matrix_name']}.json --skip-existing` "
            f"(~36 min per run; ~{len(incomplete) * 36 / 60:.1f} h sequential).")
    if candidates:
        actions.append(
            f"Candidate config(s) found ({', '.join(candidates)}): validate "
            "with a walk-forward split before considering any live change. "
            "This tool never modifies live/paper config.")
    if rejects:
        actions.append(f"Rejected: {', '.join(rejects)} - no further action.")
    if not incomplete and not candidates:
        actions.append("No candidate outperformed the baseline - keep the "
                       "current baseline configuration.")
    return actions


def _persian_summary(rows: list[dict], acceptance: dict,
                     actions: list[str]) -> str:
    completed = sum(1 for r in rows if r["status"] == "completed")
    incomplete = [r["scenario_name"] for r in rows if r["status"] != "completed"]
    candidates = [r["scenario_name"] for r in rows
                  if r["verdict"] in ("candidate", "candidate_filter")]
    if acceptance.get("checked"):
        accept_text = ("بررسی پذیرش baseline قبول شد."
                       if acceptance.get("passed")
                       else "بررسی پذیرش baseline رد شد؛ قبل از اعتماد به مقایسه‌ها بررسی کنید.")
    else:
        accept_text = "بررسی پذیرش baseline انجام نشد."
    lines = [
        f"از {len(rows)} سناریو، {completed} سناریو کامل است"
        + (f" و {len(incomplete)} سناریو هنوز اجرا نشده ({'، '.join(incomplete)})."
           if incomplete else "."),
        accept_text,
        (f"سناریوهای کاندیدا: {'، '.join(candidates)}."
         if candidates else "فعلاً هیچ سناریویی به آستانه‌ی کاندیدا نرسیده است."),
    ]
    if incomplete:
        lines.append("اقدام بعدی: اجرای سناریوهای باقی‌مانده با فرمان "
                     "run_experiment_matrix همراه با ‎--skip-existing‎ "
                     "(هر اجرا حدود ۳۶ دقیقه).")
    elif candidates:
        lines.append("اقدام بعدی: اعتبارسنجی walk-forward برای کاندیداها؛ "
                     "این ابزار هیچ تغییری در کانفیگ live/paper نمی‌دهد.")
    else:
        lines.append("اقدام بعدی: نگه داشتن کانفیگ baseline فعلی.")
    return "\n".join(f"<p>{line}</p>" for line in lines)


def render_report_md(config: dict, rows: list[dict], acceptance: dict,
                     root: Path = ROOT) -> str:
    report_root = resolve_path(root, config["report_root"])
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    actions = _recommended_actions(config, rows, acceptance)

    def link(row_path: str) -> str:
        target = root / row_path
        rel = _relpath(target, report_root)
        return f"[{Path(row_path).name}]({rel})" if target.exists() else "-"

    parts = [f"# Experiment Matrix Report - {config['matrix_name']}", ""]
    parts += [f"- Generated: {generated}",
              f"- Symbol: {config['symbol']}",
              f"- Months (one continuous replay): {', '.join(config['months'])}",
              ""]

    parts += ["## Baseline acceptance check", ""]
    if acceptance["checked"]:
        body = [[d["metric"], _fmt(d["expected"], 4), _fmt(d["actual"], 4),
                 "PASS" if d["passed"] else "FAIL"]
                for d in acceptance["details"]]
        parts += [_md_table(["metric", "expected", "actual", "result"], body),
                  "", f"**Overall: "
                      f"{'PASS' if acceptance['passed'] else 'FAIL'}**", ""]
    else:
        parts += ["_No baseline_acceptance block in config._", ""]

    parts += ["## Scenario status", ""]
    body = [[r["scenario_name"], r["run_label"], r["status"], r["mode"],
             _fmt(r["m1_reclaim_max_bars"])] for r in rows]
    parts += [_md_table(["scenario", "run label", "status", "mode", "m1 reclaim bars"],
                        body), ""]

    parts += ["## Headline metrics", ""]
    body = [[r["scenario_name"], _fmt(r["trades"]), _fmt(r["wins"]),
             _fmt(r["win_rate"]), _fmt(r["total_r"], 2), _fmt(r["net_pnl"], 2),
             _fmt(r["profit_factor"]), _fmt(r["max_drawdown_pct"], 2)]
            for r in rows]
    parts += [_md_table(["scenario", "trades", "wins", "win rate", "total R",
                         "net PnL", "profit factor", "max DD %"], body), ""]

    parts += ["## What changed vs baseline", ""]
    body = [[r["scenario_name"], _fmt(r["delta_trades"]),
             _fmt(r["delta_total_r"], 2), _fmt(r["delta_profit_factor"]),
             _fmt(r["delta_win_rate"]), _fmt(r["delta_max_drawdown_pct"], 2),
             _fmt(r["delta_net_pnl"], 2)]
            for r in rows if r["mode"] != "baseline"]
    parts += [_md_table(["scenario", "d trades", "d total R", "d PF",
                         "d win rate", "d max DD %", "d net PnL"], body), ""]

    m1_rows = [r for r in rows if r["m1_assist_mode"]]
    parts += ["## M1 funnel", ""]
    if m1_rows:
        body = [[r["scenario_name"], _fmt(r["m1_assist_mode"]),
                 _fmt(r["m1_confirmations_total"]),
                 _fmt(r["m1_confirmations_shadow"]),
                 _fmt(r["m1_confirmations_filled"]),
                 _fmt(r["m1_rejected_confirmation_cap"]),
                 _fmt(r["m1_rejected_reclaim_too_late"]),
                 _fmt(r["m1_sequence_valid"]), _fmt(r["m1_sequence_invalid"]),
                 _fmt(r["m1_sequence_ambiguous"]),
                 _fmt(r["m1_shadow_total_r"], 2),
                 f"{_fmt(r['m1_shadow_wins'])}/{_fmt(r['m1_shadow_losses'])}",
                 _fmt(r["m1_total_r"], 2),
                 f"{_fmt(r['m1_wins'])}/{_fmt(r['m1_losses'])}",
                 _fmt(r["m1_profit_factor"])]
                for r in m1_rows]
        parts += [_md_table(["scenario", "mode", "confirms", "shadow", "filled",
                             "rej cap", "rej late", "seq valid", "seq invalid",
                             "seq ambig", "shadow R", "shadow W/L", "M1 R",
                             "M1 W/L", "M1 PF"], body), ""]
    else:
        parts += ["_No M1 summaries available yet._", ""]

    parts += ["## Verdicts", ""]
    body = [[r["scenario_name"], r["verdict"], r["verdict_reason"]]
            for r in rows]
    parts += [_md_table(["scenario", "verdict", "reason"], body), ""]

    parts += ["## Output files", ""]
    body = []
    logs_dir = report_root / "logs"
    for r in rows:
        stdout_log = logs_dir / f"{r['scenario_name']}.stdout.log"
        stderr_log = logs_dir / f"{r['scenario_name']}.stderr.log"
        log_links = " / ".join(
            f"[{kind}]({_relpath(p, report_root)})"
            for kind, p in (("stdout", stdout_log), ("stderr", stderr_log))
            if p.exists()) or "-"
        body.append([r["scenario_name"], link(r["summary_path"]),
                     link(r["trades_path"]), link(r["breakdown_path"]),
                     link(r["report_path"]), link(r["events_path"]),
                     log_links])
    parts += [_md_table(["scenario", "summary", "trades", "breakdown",
                         "report", "events", "logs"], body), ""]

    parts += ["## Recommended next action", ""]
    parts += [f"- {action}" for action in actions]
    parts += ["", "## خلاصه و اقدام بعدی", "",
              '<div dir="rtl">', "",
              _persian_summary(rows, acceptance, actions), "",
              "</div>", ""]
    return "\n".join(parts)


def render_dashboard_html(config: dict, rows: list[dict], acceptance: dict,
                          root: Path = ROOT) -> str:
    report_root = resolve_path(root, config["report_root"])
    display_rows = []
    for r in rows:
        display = dict(r)
        for key in ("summary_path", "trades_path", "breakdown_path",
                    "report_path", "events_path"):
            target = root / r[key]
            display[key] = _relpath(target, report_root) if target.exists() else None
        display_rows.append(display)
    payload = json.dumps(display_rows, ensure_ascii=False)
    accept_text = ("PASS" if acceptance.get("passed") else "FAIL") \
        if acceptance.get("checked") else "not checked"
    persian = _persian_summary(rows, acceptance, [])
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{config['matrix_name']} - experiment matrix</title>
<style>
 body {{ font-family: Segoe UI, Tahoma, sans-serif; margin: 24px; color: #222; }}
 h1 {{ font-size: 1.4em; }}
 .meta {{ color: #666; margin-bottom: 12px; }}
 .filters {{ margin: 12px 0; }}
 .filters select {{ margin-right: 8px; padding: 2px 6px; }}
 table {{ border-collapse: collapse; font-size: 0.85em; }}
 th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: right; }}
 th {{ background: #f0f0f0; cursor: pointer; white-space: nowrap; }}
 td.name {{ text-align: left; font-weight: 600; }}
 tr.verdict-candidate td, tr.verdict-candidate_filter td {{ background: #e8f6e8; }}
 tr.verdict-reject td {{ background: #fbe8e8; }}
 tr.verdict-incomplete td, tr.verdict-needs_more_data td {{ background: #fdf6e3; }}
 .rtl {{ direction: rtl; text-align: right; background: #f7f7fb;
        border: 1px solid #ddd; padding: 8px 16px; margin-top: 24px;
        max-width: 720px; }}
 a {{ color: #1a56a0; }}
</style>
</head>
<body>
<h1>Experiment matrix: {config['matrix_name']}</h1>
<div class="meta">Generated {generated} &middot; months: {', '.join(config['months'])}
 &middot; baseline acceptance: <b>{accept_text}</b></div>
<div class="filters">
 Mode <select id="f-mode"><option value="">all</option></select>
 Status <select id="f-status"><option value="">all</option></select>
 Verdict <select id="f-verdict"><option value="">all</option></select>
 <span class="meta">(click a column header to sort)</span>
</div>
<table id="grid"><thead></thead><tbody></tbody></table>
<div class="rtl"><h2>خلاصه و اقدام بعدی</h2>{persian}</div>
<script>
const rows = {payload};
const cols = [
 ["scenario_name","scenario"],["status","status"],["mode","mode"],
 ["m1_reclaim_max_bars","m1 bars"],["trades","trades"],["win_rate","win rate"],
 ["total_r","total R"],["profit_factor","PF"],["max_drawdown_pct","max DD %"],
 ["delta_trades","d trades"],["delta_total_r","d total R"],
 ["delta_profit_factor","d PF"],["delta_max_drawdown_pct","d DD"],
 ["verdict","verdict"],["links","files"]];
let sortKey = null, sortAsc = true;
function fmt(v) {{
 if (v === null || v === undefined) return "-";
 if (typeof v === "number" && !Number.isInteger(v)) return v.toFixed(3);
 return String(v);
}}
function links(r) {{
 const parts = [];
 for (const [key, label] of [["summary_path","summary"],["trades_path","trades"],
      ["breakdown_path","breakdown"],["report_path","report"],["events_path","events"]])
  if (r[key]) parts.push('<a href="' + r[key] + '">' + label + '</a>');
 return parts.join(" ") || "-";
}}
function unique(key) {{ return [...new Set(rows.map(r => r[key]))].sort(); }}
function fill(id, key) {{
 const sel = document.getElementById(id);
 for (const v of unique(key)) {{
  const o = document.createElement("option"); o.value = o.textContent = v;
  sel.appendChild(o);
 }}
 sel.onchange = render;
}}
function render() {{
 const fm = document.getElementById("f-mode").value,
       fs = document.getElementById("f-status").value,
       fv = document.getElementById("f-verdict").value;
 let data = rows.filter(r => (!fm || r.mode === fm) &&
                             (!fs || r.status === fs) &&
                             (!fv || r.verdict === fv));
 if (sortKey) data = [...data].sort((a, b) => {{
  const x = a[sortKey], y = b[sortKey];
  if (x === null || x === undefined) return 1;
  if (y === null || y === undefined) return -1;
  return (x < y ? -1 : x > y ? 1 : 0) * (sortAsc ? 1 : -1);
 }});
 const thead = document.querySelector("#grid thead");
 thead.innerHTML = "<tr>" + cols.map(([k, label]) =>
  '<th data-key="' + k + '">' + label + "</th>").join("") + "</tr>";
 thead.querySelectorAll("th").forEach(th => th.onclick = () => {{
  const key = th.dataset.key;
  if (key === "links") return;
  sortAsc = sortKey === key ? !sortAsc : true; sortKey = key; render();
 }});
 document.querySelector("#grid tbody").innerHTML = data.map(r =>
  '<tr class="verdict-' + r.verdict + '">' + cols.map(([k]) =>
   k === "links" ? "<td>" + links(r) + "</td>" :
   k === "scenario_name" ? '<td class="name" title="' + r.run_label + '">' + r.scenario_name + "</td>" :
   "<td>" + fmt(r[k]) + "</td>").join("") + "</tr>").join("");
}}
fill("f-mode", "mode"); fill("f-status", "status"); fill("f-verdict", "verdict");
render();
</script>
</body>
</html>
"""


def write_outputs(config: dict, rows: list[dict], acceptance: dict,
                  root: Path = ROOT, html: bool = True) -> dict:
    report_root = resolve_path(root, config["report_root"])
    report_root.mkdir(parents=True, exist_ok=True)
    fieldnames = list(REQUIRED_FIELDS) + list(M1_FIELDS)

    csv_path = report_root / "matrix_summary.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("" if row.get(key) is None else row[key])
                             for key in fieldnames})

    json_path = report_root / "matrix_summary.json"
    json_path.write_text(json.dumps({
        "matrix_name": config["matrix_name"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline_acceptance": acceptance,
        "rows": rows,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    md_path = report_root / "matrix_report.md"
    md_path.write_text(render_report_md(config, rows, acceptance, root),
                       encoding="utf-8")

    outputs = {"csv": csv_path, "json": json_path, "md": md_path}
    if html:
        html_path = report_root / "index.html"
        html_path.write_text(
            render_dashboard_html(config, rows, acceptance, root),
            encoding="utf-8")
        outputs["html"] = html_path
    return outputs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="matrix config JSON")
    parser.add_argument("--no-html", action="store_true",
                        help="skip the static index.html dashboard")
    args = parser.parse_args(argv)
    try:
        config = load_matrix_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    rows, acceptance = build_rows(config)
    outputs = write_outputs(config, rows, acceptance, html=not args.no_html)
    completed = sum(1 for row in rows if row["status"] == "completed")
    print(f"{completed}/{len(rows)} scenarios have summaries")
    if acceptance["checked"]:
        print("baseline acceptance: "
              + ("PASS" if acceptance["passed"] else "FAIL"))
    for row in rows:
        print(f"  {row['scenario_name']:<14} {row['status']:<10} "
              f"-> {row['verdict']}")
    for kind, path in outputs.items():
        print(f"wrote {kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
