#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
from pathlib import Path

import pandas as pd


def metrics(part: pd.DataFrame) -> dict:
    r = part["r_multiple"].astype(float)
    pnl = part["pnl"].astype(float)
    wins = r[r > 0]
    losses = r[r <= 0]
    curve = r.cumsum()
    drawdown = curve.cummax().clip(lower=0) - curve
    return {
        "trades": len(part),
        "wins": int((r > 0).sum()),
        "win_rate_pct": (r > 0).mean() * 100,
        "total_r": r.sum(),
        "avg_r": r.mean(),
        "r_profit_factor": wins.sum() / abs(losses.sum()) if losses.sum() else float("inf"),
        "max_drawdown_r": drawdown.max() if len(drawdown) else 0.0,
        "net_pnl": pnl.sum(),
    }


def grouped(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    rows = []
    for period, part in frame.groupby(key, sort=True):
        rows.append({"period": str(period), **metrics(part)})
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create monthly/quarterly trade stability report")
    parser.add_argument("trades", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    parts = []
    for path in args.trades:
        item = pd.read_csv(path)
        item["source"] = path.name
        parts.append(item)
    frame = pd.concat(parts, ignore_index=True)
    frame["opened_time"] = pd.to_datetime(frame["opened_time"], utc=True)
    frame = frame.sort_values("opened_time").drop_duplicates("id")
    frame["month"] = frame["opened_time"].dt.strftime("%Y-%m")
    frame["quarter"] = frame["opened_time"].dt.to_period("Q").astype(str)

    monthly = grouped(frame, "month")
    quarterly = grouped(frame, "quarter")
    overall = pd.DataFrame([{"period": "all", **metrics(frame)}])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    monthly.to_csv(args.out.with_name(args.out.stem + "_monthly.csv"), index=False)
    quarterly.to_csv(args.out.with_name(args.out.stem + "_quarterly.csv"), index=False)

    style = """
    body{font-family:Segoe UI,Arial;background:#0b1020;color:#e7ebf5;max-width:1300px;margin:auto;padding:28px}
    section{background:#121a2e;border:1px solid #23304d;border-radius:12px;padding:16px;margin:16px 0}
    table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px;border-bottom:1px solid #26324c;text-align:right}
    th:first-child,td:first-child{text-align:left}th{color:#9fb0ce}h1,h2{margin-top:0}.muted{color:#93a0ba}
    """
    table_args = dict(index=False, border=0, float_format=lambda value: f"{value:.3f}")
    doc = ("<!doctype html><html><head><meta charset='utf-8'><title>Temporal stability</title>"
           f"<style>{style}</style></head><body><h1>Temporal Backtest Stability</h1>"
           "<p class='muted'>Swing=12 · BOS+CHoCH · CHoCH+Displacement sizing · CHoCH cap=0.5% · RR=1.5</p>"
           f"<section><h2>Overall</h2>{overall.to_html(**table_args)}</section>"
           f"<section><h2>Quarterly</h2>{quarterly.to_html(**table_args)}</section>"
           f"<section><h2>Monthly</h2>{monthly.to_html(**table_args)}</section>"
           "</body></html>")
    args.out.write_text(doc, encoding="utf-8")
    print(overall.to_string(index=False))
    print("\nQuarterly\n", quarterly.to_string(index=False))
    print("\nMonthly\n", monthly.to_string(index=False))
    print(f"\nHTML: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
