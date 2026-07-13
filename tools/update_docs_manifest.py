"""Check or acknowledge synchronization of core source and technical guide."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "source_manifest.json"
GUIDE = ROOT / "docs" / "ROBOT_TECHNICAL_GUIDE.md"

TRACKED = {
    "run_pine_ob_paper.py": "runner-cli",
    "pine_ob_bot/__init__.py": "public-api",
    "pine_ob_bot/app.py": "app-orchestration",
    "pine_ob_bot/cli_risk.py": "fixed-risk-cli",
    "pine_ob_bot/config.py": "configuration",
    "pine_ob_bot/dashboard_server.py": "dashboard",
    "pine_ob_bot/demo_executor.py": "demo-execution",
    "pine_ob_bot/entry_pivot.py": "entry-pivot",
    "pine_ob_bot/historical.py": "historical",
    "pine_ob_bot/liquidity_context.py": "liquidity-context",
    "pine_ob_bot/market_recorder.py": "market-capture",
    "pine_ob_bot/models.py": "models",
    "pine_ob_bot/mt5_feed.py": "mt5-feed",
    "pine_ob_bot/mtf_context.py": "m15-context",
    "pine_ob_bot/paper.py": "paper-execution-risk",
    "pine_ob_bot/pine_engine.py": "pine-engine",
    "pine_ob_bot/report_worker.py": "report-worker",
    "pine_ob_bot/reporting.py": "reporting",
    "pine_ob_bot/storage.py": "persistence",
    "pine_ob_bot/structure_context.py": "structure-context",
    "pine_ob_bot/tick_historical.py": "tick-historical",
    "tools/fetch_mt5_ticks.py": "tick-download",
    "tools/run_tick_backtest.py": "tick-backtest-cli",
    "tools/run_tick_risk_comparison.py": "tick-risk-comparison",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_files() -> dict[str, dict[str, str]]:
    return {name: {"sha256": sha256(ROOT / name), "guide_section": section}
            for name, section in TRACKED.items()}


def differences() -> list[str]:
    if not MANIFEST.exists():
        return ["manifest is missing"]
    saved = json.loads(MANIFEST.read_text(encoding="utf-8")).get("files", {})
    current = current_files()
    messages = []
    for name, item in current.items():
        if saved.get(name, {}).get("sha256") != item["sha256"]:
            messages.append(f"{name} -> update guide section: {item['guide_section']}")
    for name in saved.keys() - current.keys():
        messages.append(f"removed tracked file: {name}")
    return messages


def write_manifest() -> None:
    if not GUIDE.exists():
        raise SystemExit(f"guide does not exist: {GUIDE}")
    payload = {
        "guide": GUIDE.relative_to(ROOT).as_posix(),
        "acknowledged_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": current_files(),
    }
    MANIFEST.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check only (the default)")
    parser.add_argument("--confirm-guide-updated", action="store_true",
                        help="acknowledge semantic guide review and refresh hashes")
    args = parser.parse_args()
    if args.confirm_guide_updated:
        write_manifest()
        print(f"updated {MANIFEST.relative_to(ROOT)}")
        return 0
    changed = differences()
    if changed:
        print("Technical documentation is stale:")
        for item in changed:
            print(f"- {item}")
        print("Update docs/ROBOT_TECHNICAL_GUIDE.md, then run with --confirm-guide-updated.")
        return 1
    print("technical documentation manifest is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
