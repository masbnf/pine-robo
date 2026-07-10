"""Causal M1 candle aggregation from raw Bid/Ask ticks.

Strategy price convention (documented, deliberate): like the existing M5
builder (tick_historical.M5Builder), the M1 OHLC used for STRUCTURE
decisions is built from the Bid. Full Bid/Ask/spread OHLC is preserved on
every bar for diagnostics; actual fills always use the real Bid/Ask of the
executing tick, never a bar price.

Bucket boundary convention (identical to the M5 builder, documented): a
bar covers the half-open interval [minute, minute+1); the tick that starts
a new minute CLOSES the previous bar first. The M1 bucket for 10:04:xx is
"10:04:00" and closes on the first tick at/after 10:05:00 -- exactly the
minute grid the M5 bucket (10:00..10:05) is built on, so an M5 close and
its final M1 close are triggered by the same tick and can be ordered
deterministically by the caller (M1 first, then M5; see tick_historical).

No synthetic bars: minutes without any tick produce NO bar; the gap is
only counted (m1_empty_minutes / m1_tick_gaps). Out-of-order ticks (older
than the current bucket) are counted and safely dropped -- never applied
retroactively. Timestamps of the capture are ISO-8601 with explicit
offset; the source timezone is recorded on every bar.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from .models import Tick


@dataclass(slots=True)
class M1Candle:
    time: str                 # bucket start, ISO-8601
    open: float               # Bid OHLC -- the structure price (see module doc)
    high: float
    low: float
    close: float
    bid_open: float = 0.0
    bid_high: float = 0.0
    bid_low: float = 0.0
    bid_close: float = 0.0
    ask_open: float = 0.0
    ask_high: float = 0.0
    ask_low: float = 0.0
    ask_close: float = 0.0
    spread_open: float = 0.0
    spread_high: float = 0.0
    spread_low: float = 0.0
    spread_close: float = 0.0
    spread_average: float = 0.0
    tick_count: int = 0
    partial: bool = False     # True when flagged as suspiciously thin
    source_timezone: str = "UTC"


@dataclass
class M1Builder:
    """Streaming Bid/Ask tick -> M1 aggregator. One instance per run.

    Counters are kept on the instance and merged into broker stats by the
    caller. `partial_min_ticks` flags bars with fewer ticks as partial
    (diagnostic only -- they are still emitted; consumers decide).
    """
    partial_min_ticks: int = 3
    bucket: str | None = None
    bucket_minute_epoch: int | None = None
    time: str | None = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    ask_open: float = 0.0
    ask_high: float = 0.0
    ask_low: float = 0.0
    ask_close: float = 0.0
    spread_open: float = 0.0
    spread_high: float = 0.0
    spread_low: float = 0.0
    spread_close: float = 0.0
    spread_sum: float = 0.0
    tick_count: int = 0
    source_timezone: str = "UTC"
    last_tick_key: tuple | None = None
    counters: dict = field(default_factory=lambda: {
        "m1_bars_created": 0,
        "m1_empty_minutes": 0,
        "m1_tick_gaps": 0,
        "m1_out_of_order_ticks": 0,
        "m1_duplicate_ticks": 0,
        "m1_partial_bars": 0,
        "m1_max_ticks_per_bar": 0,
        "m1_min_ticks_per_bar": 0,
    })

    def push(self, tick: Tick) -> M1Candle | None:
        """Feed one tick; returns the JUST-CLOSED M1 bar when the tick opens
        a new minute, else None. The returned bar never includes this tick."""
        key = (tick.time, tick.bid, tick.ask)
        if key == self.last_tick_key:
            self.counters["m1_duplicate_ticks"] += 1
            return None
        self.last_tick_key = key
        minute_epoch, bucket_time, tzname = _minute_bucket(tick.time)
        if minute_epoch is None:
            self.counters["m1_out_of_order_ticks"] += 1  # unparseable == unusable
            return None
        if self.bucket_minute_epoch is not None and minute_epoch < self.bucket_minute_epoch:
            # Older than the bucket in progress: count and drop, never apply
            # retroactively (monotonicity guard).
            self.counters["m1_out_of_order_ticks"] += 1
            return None
        closed: M1Candle | None = None
        if self.bucket is not None and minute_epoch > self.bucket_minute_epoch:
            closed = self._finalize()
            skipped = (minute_epoch - self.bucket_minute_epoch) // 60 - 1
            if skipped > 0:
                # Minutes with no tick at all: recorded, never synthesized.
                self.counters["m1_empty_minutes"] += skipped
                self.counters["m1_tick_gaps"] += 1
        if self.bucket is None or minute_epoch > (self.bucket_minute_epoch or -1):
            self.bucket = bucket_time
            self.bucket_minute_epoch = minute_epoch
            self.time = bucket_time
            self.source_timezone = tzname
            spread = max(0.0, tick.ask - tick.bid)
            self.open = self.high = self.low = self.close = tick.bid
            self.ask_open = self.ask_high = self.ask_low = self.ask_close = tick.ask
            self.spread_open = self.spread_high = self.spread_low = self.spread_close = spread
            self.spread_sum = spread
            self.tick_count = 1
            return closed
        # Same minute: extend the bar in progress.
        spread = max(0.0, tick.ask - tick.bid)
        self.high = max(self.high, tick.bid)
        self.low = min(self.low, tick.bid)
        self.close = tick.bid
        self.ask_high = max(self.ask_high, tick.ask)
        self.ask_low = min(self.ask_low, tick.ask)
        self.ask_close = tick.ask
        self.spread_high = max(self.spread_high, spread)
        self.spread_low = min(self.spread_low, spread)
        self.spread_close = spread
        self.spread_sum += spread
        self.tick_count += 1
        return closed

    def _finalize(self) -> M1Candle:
        partial = self.tick_count < self.partial_min_ticks
        counters = self.counters
        counters["m1_bars_created"] += 1
        if partial:
            counters["m1_partial_bars"] += 1
        counters["m1_max_ticks_per_bar"] = max(counters["m1_max_ticks_per_bar"],
                                               self.tick_count)
        counters["m1_min_ticks_per_bar"] = (self.tick_count
                                            if counters["m1_min_ticks_per_bar"] == 0
                                            else min(counters["m1_min_ticks_per_bar"],
                                                     self.tick_count))
        return M1Candle(
            time=self.time, open=self.open, high=self.high, low=self.low,
            close=self.close,
            bid_open=self.open, bid_high=self.high, bid_low=self.low,
            bid_close=self.close,
            ask_open=self.ask_open, ask_high=self.ask_high, ask_low=self.ask_low,
            ask_close=self.ask_close,
            spread_open=self.spread_open, spread_high=self.spread_high,
            spread_low=self.spread_low, spread_close=self.spread_close,
            spread_average=self.spread_sum / self.tick_count if self.tick_count else 0.0,
            tick_count=self.tick_count, partial=partial,
            source_timezone=self.source_timezone)

    def dump_state(self) -> dict:
        """Persist the bar in progress (plus counters) across a restart."""
        state = {key: getattr(self, key) for key in (
            "bucket", "bucket_minute_epoch", "time", "open", "high", "low",
            "close", "ask_open", "ask_high", "ask_low", "ask_close",
            "spread_open", "spread_high", "spread_low", "spread_close",
            "spread_sum", "tick_count", "source_timezone", "partial_min_ticks")}
        state["last_tick_key"] = list(self.last_tick_key) if self.last_tick_key else None
        state["counters"] = dict(self.counters)
        return state

    def load_state(self, data: dict) -> None:
        for key in ("bucket", "bucket_minute_epoch", "time", "open", "high",
                    "low", "close", "ask_open", "ask_high", "ask_low",
                    "ask_close", "spread_open", "spread_high", "spread_low",
                    "spread_close", "spread_sum", "tick_count",
                    "source_timezone", "partial_min_ticks"):
            if key in data:
                setattr(self, key, data[key])
        raw_key = data.get("last_tick_key")
        self.last_tick_key = tuple(raw_key) if raw_key else None
        stored = data.get("counters") or {}
        for key in self.counters:
            self.counters[key] = stored.get(key, self.counters[key])


def _minute_bucket(stamp: str) -> tuple[int | None, str | None, str]:
    """(epoch of minute start, bucket ISO time, tz name) for a tick stamp.

    Fast path mirrors M5Builder: MT5 capture stamps are sortable ISO with a
    fixed layout, so the minute key can be sliced without datetime parsing.
    Fallback parses fully (and treats naive stamps as UTC, documented).
    """
    try:
        # "YYYY-MM-DDTHH:MM:SS(.ffffff)+00:00" -- slice the minute directly.
        if len(stamp) >= 16 and stamp[4] == "-" and stamp[13] == ":":
            parsed = datetime.fromisoformat(stamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            minute = parsed.replace(second=0, microsecond=0)
            return int(minute.timestamp()), minute.isoformat(), str(parsed.tzinfo)
        return None, None, "UTC"
    except ValueError:
        return None, None, "UTC"
