from __future__ import annotations

import csv
from pathlib import Path

from .models import Candle, Tick


class MarketRecorder:
    """Append-only capture of live Bid/Ask ticks and closed M5 candles."""

    def __init__(self, root: Path, symbol: str) -> None:
        self.root = root
        self.symbol = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in symbol)
        self.root.mkdir(parents=True, exist_ok=True)
        tick_files = sorted(self.root.glob(f"ticks_{self.symbol}_*.csv"))
        tick_row = self._last_row(tick_files[-1]) if tick_files else None
        self.last_tick = ((tick_row[0], float(tick_row[1]), float(tick_row[2]))
                          if tick_row and tick_row[0] != "time" else None)
        candle_row = self._last_row(self.root / f"candles_{self.symbol}_M5.csv")
        self.last_candle_time = (candle_row[0] if candle_row and candle_row[0] != "time"
                                 else None)

    def record_tick(self, tick: Tick) -> bool:
        identity = (tick.time, tick.bid, tick.ask)
        if identity == self.last_tick:
            return False
        self.last_tick = identity
        day = tick.time[:10].replace("-", "")
        self._append(self.root / f"ticks_{self.symbol}_{day}.csv",
                     ("time", "bid", "ask", "spread"),
                     (tick.time, tick.bid, tick.ask, tick.ask - tick.bid))
        return True

    def record_candle(self, candle: Candle) -> bool:
        if candle.time == self.last_candle_time:
            return False
        self.last_candle_time = candle.time
        self._append(self.root / f"candles_{self.symbol}_M5.csv",
                     ("time", "open", "high", "low", "close"),
                     (candle.time, candle.open, candle.high, candle.low, candle.close))
        return True

    @staticmethod
    def _append(path: Path, header: tuple, row: tuple) -> None:
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(header)
            writer.writerow(row)
            handle.flush()

    @staticmethod
    def _last_row(path: Path) -> list[str] | None:
        if not path.exists() or path.stat().st_size == 0:
            return None
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            start = max(0, end - 8192)
            handle.seek(start)
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        if not lines:
            return None
        return next(csv.reader([lines[-1]]), None)
