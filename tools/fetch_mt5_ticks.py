#!/usr/bin/env python3
"""Download MT5 ticks in monthly ranges, stored as resumable daily CSV.GZ files."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pine_ob_bot.config import BotConfig
from pine_ob_bot.mt5_feed import MT5ReadOnlyFeed


def month_bounds(value: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc)
    return start, (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def iter_days(start: datetime, end: datetime):
    day = start
    while day < end:
        yield day, min(day + timedelta(days=1), end)
        day += timedelta(days=1)


def load_coverage(root: Path) -> dict:
    path = root / "coverage.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def range_exists(root: Path, start: datetime, end: datetime) -> bool:
    coverage = load_coverage(root)
    if (not coverage.get("complete_month") or coverage.get("range_start") != start.isoformat()
            or coverage.get("range_end_exclusive") != end.isoformat()):
        return False
    days = coverage.get("days", [])
    if len(days) != sum(1 for _ in iter_days(start, end)):
        return False
    return all(item.get("status") != "error" and
               (root / item.get("file", "")).is_file() for item in days)


def write_day(mt5, symbol: str, start: datetime, end: datetime, path: Path,
              retries: int, overwrite: bool, existing: dict | None = None) -> dict:
    if path.exists() and not overwrite and existing and existing.get("status") != "error":
        return {**existing, "status": "existing", "file": path.name}
    ticks = None
    error = None
    for attempt in range(retries):
        ticks = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)
        if ticks is not None:
            break
        error = mt5.last_error()
        time.sleep(min(2 ** attempt, 10))
    if ticks is None:
        return {"date": start.date().isoformat(), "ticks": 0, "status": "error",
                "error": repr(error)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    first = last = None
    with gzip.open(temp, "wt", newline="", encoding="utf-8", compresslevel=6) as handle:
        writer = csv.writer(handle)
        writer.writerow(("time", "bid", "ask", "spread", "last", "volume",
                         "volume_real", "flags", "time_msc"))
        seen = None
        count = 0
        for row in ticks:
            msc = int(row["time_msc"])
            # MT5 range endpoints can be inclusive. Enforce [start, end) so
            # midnight belongs only to the next day/month file.
            if not int(start.timestamp() * 1000) <= msc < int(end.timestamp() * 1000):
                continue
            key = (msc, float(row["bid"]), float(row["ask"]), int(row["flags"]))
            if key == seen:
                continue
            seen = key
            stamp = datetime.fromtimestamp(msc / 1000, timezone.utc).isoformat()
            first = first or stamp
            last = stamp
            bid, ask = float(row["bid"]), float(row["ask"])
            writer.writerow((stamp, bid, ask, ask - bid, float(row["last"]),
                             int(row["volume"]), float(row["volume_real"]),
                             int(row["flags"]), msc))
            count += 1
    os.replace(temp, path)
    return {"date": start.date().isoformat(), "ticks": count, "status": "downloaded",
            "first": first, "last": last, "range_start": start.isoformat(),
            "range_end_exclusive": end.isoformat(), "file": path.name}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", action="append", required=True, metavar="YYYY-MM",
                        help="month to fetch; repeat for multiple months")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--out", type=Path,
                        default=Path("pine_ob_bot_data/historical_ticks"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--pause", type=float, default=.25,
                        help="seconds between daily requests")
    args = parser.parse_args()
    requested = list(dict.fromkeys(args.month))
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    jobs = []
    for month in requested:
        start, calendar_end = month_bounds(month)
        effective_end = min(calendar_end, today)
        if effective_end <= start:
            print(f"RANGE NOT CLOSED {month}: no complete UTC day is available")
            continue
        month_root = args.out / month
        if not args.overwrite and effective_end == calendar_end and range_exists(
                month_root, start, calendar_end):
            print(f"RANGE EXISTS {month}: no download needed -> {month_root}")
            continue
        jobs.append((month, start, calendar_end, effective_end, month_root))
    if not jobs:
        return 0
    feed = MT5ReadOnlyFeed(BotConfig(symbol=args.symbol))
    try:
        feed.connect()
        for month, start, calendar_end, end, month_root in jobs:
            old = load_coverage(month_root)
            known = {item.get("date"): item for item in old.get("days", [])}
            results = []
            for day_start, day_end in iter_days(start, end):
                path = month_root / f"ticks_{feed.symbol}_{day_start:%Y%m%d}.csv.gz"
                result = write_day(feed.mt5, feed.symbol, day_start, day_end, path,
                                   args.retries, args.overwrite,
                                   known.get(day_start.date().isoformat()))
                results.append(result)
                print(f"{result['date']} {result['status']} ticks={result['ticks']}", flush=True)
                time.sleep(max(args.pause, 0))
            summary = {"month": month, "symbol": feed.symbol,
                       "range_start": start.isoformat(),
                       "range_end_exclusive": end.isoformat(),
                       "calendar_end_exclusive": calendar_end.isoformat(),
                       "complete_month": end == calendar_end,
                       "boundary_policy": "UTC half-open [start,end)",
                       "ticks": sum(item["ticks"] for item in results), "days": results}
            month_root.mkdir(parents=True, exist_ok=True)
            coverage_path = month_root / "coverage.json"
            temp = coverage_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, coverage_path)
            print(f"MONTH {month}: {summary['ticks']} ticks -> {month_root}")
    finally:
        feed.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
