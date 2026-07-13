"""Generate (or regenerate) the legacy no-flag baseline fixtures.

CRITICAL CONTRACT: the JSON fixtures next to this script were produced by
running THIS script against the PRE-EXPERIMENTAL code (before any of the
flag-gated trade-frequency features existed). The legacy regression test
replays the same synthetic CSV through run_historical with every
experimental flag off and asserts the core trade rows, equity and the
counters captured here are byte-identical. Do NOT regenerate these files
after behaviour-changing edits unless the change is an approved, intended
baseline change -- regenerating them defeats the whole guard.

Run from the repository root:
    python pine_ob_bot/tests/fixtures/gen_legacy_fixture.py
"""
from __future__ import annotations

import csv
import json
import math
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from pine_ob_bot.config import BotConfig  # noqa: E402
from pine_ob_bot.historical import run_historical  # noqa: E402

BAR_COUNT = 1800
# Piecewise trend legs with a pullback wave and asymmetric wicks. Tuned (see
# session notes) so the pre-change code produces 7 sweep_reclaim trades,
# 11 limit trades and ~52 trend-change cancellations on 1800 bars -- enough
# surface for a meaningful row-by-row regression and for revival scenarios.
LEG_LEN, SLOPE, WAVE_AMP, WAVE_PER, WICK_BASE, WICK_VAR = 100, 1.3, 10.0, 22, 1.2, 1.5


def synthetic_bars() -> list[tuple[str, float, float, float, float]]:
    """Deterministic pseudo-market: alternating trend legs + pullback wave.

    Pure function of the bar index (no RNG state), so every platform and
    every run reproduces the exact same OHLC values.
    """
    from datetime import datetime, timedelta, timezone
    out = []
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    close = 2400.0
    prev = close
    for i in range(BAR_COUNT):
        leg = (i // LEG_LEN) % 2
        s = SLOPE if leg == 0 else -SLOPE
        wave_now = WAVE_AMP * math.sin(2 * math.pi * i / WAVE_PER)
        wave_next = WAVE_AMP * math.sin(2 * math.pi * (i + 1) / WAVE_PER)
        c = close + s + (wave_next - wave_now)
        o = prev
        wick_up = WICK_BASE + WICK_VAR * abs(math.sin(i * 1.3))
        wick_dn = WICK_BASE + WICK_VAR * abs(math.cos(i * 1.7))
        h = max(o, c) + wick_up
        low = min(o, c) - wick_dn
        stamp = (t0 + timedelta(minutes=5 * i)).isoformat()
        out.append((stamp, round(o, 2), round(h, 2), round(low, 2), round(c, 2)))
        prev = c
        close = c
    return out


def write_csv(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "open", "high", "low", "close"])
        writer.writerows(synthetic_bars())


def fixture_configs(tmp: Path) -> dict[str, BotConfig]:
    """The two no-experimental-flag configurations frozen as baselines."""
    return {
        # The user's forward-test baseline: t9 p3x3 sweep_reclaim, ATR buffer
        # 0.20, RR 1.5, BOS + strong CHoCH, fixed uniform risk (no CHoCH cap).
        "baseline_sweep_reclaim": BotConfig(
            trend_swing_length=9, entry_pivot_left=3, entry_pivot_right=3,
            rr=1.5, risk_fraction=0.005, choch_risk_cap_fraction=None,
            allowed_break_kinds=("BOS", "CHoCH"), strong_choch_only=True,
            entry_mode="sweep_reclaim", sweep_reclaim_atr_buffer=0.20,
            sweep_reclaim_max_bars=1, atr_period=50,
            db_path=tmp / "sr" / "paper.sqlite3"),
        # The stock limit-mode default path.
        "baseline_limit": BotConfig(
            trend_swing_length=9, entry_pivot_left=3, entry_pivot_right=3,
            rr=1.5, risk_fraction=0.005, choch_risk_cap_fraction=None,
            allowed_break_kinds=("BOS",), entry_mode="limit", atr_period=50,
            db_path=tmp / "limit" / "paper.sqlite3"),
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="legacy_fixture_"))
    csv_path = tmp / "legacy_bars.csv"
    write_csv(csv_path)

    for name in ("baseline_sweep_reclaim", "baseline_limit"):
        # run_historical returns the summary; the row-by-row capture reads
        # back the trades CSV it wrote (each config gets its own directory).
        cfg = fixture_configs(tmp)[name]
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
        summary = run_historical(csv_path, cfg, initial_equity=10_000.0,
                                 spread=0.20, run_label=name)
        out_dir = cfg.db_path.parent
        trades_csv = next(out_dir.glob("historical_*_trades.csv"), None)
        rows = []
        if trades_csv and trades_csv.exists():
            with trades_csv.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    rows.append({key: row.get(key, "") for key in (
                        "opened_time", "closed_time", "direction", "entry",
                        "stop", "target", "exit_price", "volume", "result",
                        "pnl", "r_multiple")})
        core_counters = {key: value for key, value in sorted(summary.items())
                         if isinstance(value, (int, float)) and not isinstance(value, bool)}
        fixture = {"config": name, "bars": BAR_COUNT, "spread": 0.20,
                   "initial_equity": 10_000.0,
                   "summary_counters": core_counters, "trades": rows}
        out = HERE / f"legacy_{name}.json"
        out.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(f"wrote {out} ({len(rows)} trades)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
