"""THE critical guard: no new flag = no behavioral change.

Replays the committed deterministic synthetic market through run_historical
with every experimental flag OFF, in both frozen baseline configurations
(the user's sweep_reclaim forward-test profile and the stock limit profile),
and asserts the output is row-by-row identical to the fixtures captured
from the PRE-experimental code (see fixtures/gen_legacy_fixture.py -- do not
regenerate them casually).

Compared per config: every trade row (timestamps, direction, entry, stop,
target, exit, volume, result, pnl, R), final equity, trade/win/loss counts,
pending/filled order counts and every numeric summary counter that existed
before the experiments.

Run with:  pytest pine_ob_bot/tests/test_legacy_regression.py -v
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(FIXTURES))

import gen_legacy_fixture as gen  # noqa: E402

try:
    from pine_ob_bot.historical import run_historical
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from historical import run_historical

TRADE_FIELDS = ("opened_time", "closed_time", "direction", "entry", "stop",
                "target", "exit_price", "volume", "result", "pnl", "r_multiple")


def _run_baseline(name: str, tmp_path: Path) -> tuple[list[dict], dict]:
    csv_path = tmp_path / "legacy_bars.csv"
    if not csv_path.exists():
        gen.write_csv(csv_path)
    cfg = gen.fixture_configs(tmp_path)[name]
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    summary = run_historical(csv_path, cfg, initial_equity=10_000.0,
                             spread=0.20, run_label=name)
    trades_csv = next(cfg.db_path.parent.glob("historical_*_trades.csv"))
    rows = []
    with trades_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append({key: row.get(key, "") for key in TRADE_FIELDS})
    return rows, summary


@pytest.mark.parametrize("name", ["baseline_sweep_reclaim", "baseline_limit"])
def test_no_flag_output_is_bit_identical_to_prechange_fixture(name, tmp_path):
    fixture = json.loads((FIXTURES / f"legacy_{name}.json").read_text(encoding="utf-8"))
    rows, summary = _run_baseline(name, tmp_path)

    # Row-by-row trade equality: every timestamp and every price string must
    # match exactly what the pre-change code wrote.
    assert len(rows) == len(fixture["trades"]), (
        f"trade count changed: {len(rows)} vs fixture {len(fixture['trades'])}")
    for i, (produced, expected) in enumerate(zip(rows, fixture["trades"])):
        assert produced == expected, (
            f"trade row {i} differs: "
            f"{ {k: (produced[k], expected[k]) for k in produced if produced[k] != expected[k]} }")

    # Every numeric counter captured pre-change (trades/wins/equity/total_r,
    # pending/filled/cancelled counters, sweep telemetry, ...) must be equal.
    for key, expected in fixture["summary_counters"].items():
        produced = summary.get(key)
        assert isinstance(produced, (int, float)), f"summary key {key} vanished"
        assert abs(float(produced) - float(expected)) < 1e-9, (
            f"counter {key} drifted: {produced} vs {expected}")


def test_no_flag_run_creates_no_experimental_state(tmp_path):
    """With every flag off the experimental ledgers stay empty end-to-end."""
    csv_path = tmp_path / "legacy_bars.csv"
    gen.write_csv(csv_path)
    cfg = gen.fixture_configs(tmp_path)["baseline_sweep_reclaim"]
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    summary = run_historical(csv_path, cfg, initial_equity=10_000.0,
                             spread=0.20, run_label="stateless")
    assert summary["revival_suspended"] == 0
    assert summary["revival_orders_created"] == 0
    assert summary["reentry_candidates"] == 0
    assert summary["reentry_orders_created"] == 0
    assert summary["spread_wait_started"] == 0
    assert summary["rejected_portfolio_risk_cap"] == 0
    # M1 Entry Assist off: no engine, no bars, no M1 state of any kind.
    assert summary["m1_bars_created"] == 0
    assert summary["m1_setups_observed"] == 0
    assert summary["m1_confirmations_total"] == 0
    assert summary["m1_entry_assist"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
