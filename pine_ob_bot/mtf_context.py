"""Closed-candle M5 -> M15 context collector; it never gates M5 trades."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone

from .config import BotConfig
from .models import Candle, StructureBreak
from .pine_engine import PineSwingOBEngine


class M15Context:
    def __init__(self, cfg: BotConfig):
        m15_cfg = replace(cfg, timeframe="M5", timeframe_minutes=5,
                          swing_length=cfg.m15_swing_length, atr_period=cfg.m15_atr_period)
        self.engine = PineSwingOBEngine(m15_cfg)
        self.bucket_start: str | None = None
        self.parts: list[Candle] = []
        self.last_break: StructureBreak | None = None

    def process_m5(self, candle: Candle) -> Candle | None:
        dt = datetime.fromisoformat(candle.time.replace("Z", "+00:00")).astimezone(timezone.utc)
        start = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0).isoformat()
        if self.bucket_start is not None and start != self.bucket_start:
            self._flush()
        if self.bucket_start is None:
            self.bucket_start = start
        self.parts.append(candle)
        # M5 timestamps are bar opens; minute 10 is the final bar of a 15m bucket.
        if dt.minute % 15 == 10:
            return self._flush()
        return None

    def _flush(self) -> Candle | None:
        if not self.parts or self.bucket_start is None:
            return None
        # Require all three M5 components. Partial startup buckets are discarded.
        bar = None
        if len(self.parts) == 3:
            bar = Candle(self.bucket_start, self.parts[0].open,
                         max(x.high for x in self.parts), min(x.low for x in self.parts),
                         self.parts[-1].close)
            breaks, _, _ = self.engine.process(bar)
            if breaks:
                self.last_break = breaks[-1]
        self.parts = []
        self.bucket_start = None
        return bar

    def snapshot(self, m5_direction: str, price: float) -> dict:
        high = self.engine.swing_high.level if self.engine.swing_high else None
        low = self.engine.swing_low.level if self.engine.swing_low else None
        position = None
        zone = "unknown"
        if high is not None and low is not None and high > low:
            position = (price - low) / (high - low)
            zone = "discount" if position < 1 / 3 else "premium" if position > 2 / 3 else "equilibrium"
        trend = "bull" if self.engine.trend == 1 else "bear" if self.engine.trend == -1 else "neutral"
        last_ob = next((x for x in self.engine.order_blocks if x.active), None)
        age = (len(self.engine.candles) - 1 - self.last_break.bar_index) if self.last_break else None
        atr = self.engine.atr_values[-1] if self.engine.atr_values else None
        return {
            "m15_trend": trend,
            "m15_alignment": "aligned" if trend == m5_direction else "counter" if trend != "neutral" else "unknown",
            "m15_last_break_kind": self.last_break.kind if self.last_break else "unknown",
            "m15_last_break_direction": self.last_break.direction if self.last_break else "unknown",
            "m15_break_age_bars": age,
            "m15_swing_high": high, "m15_swing_low": low,
            "m15_range_position": position, "m15_range_zone": zone,
            "m15_atr": atr,
            "m15_active_ob_direction": last_ob.direction if last_ob else "none",
            "m15_active_ob_width_atr": ((last_ob.high - last_ob.low) / atr)
            if last_ob and atr else None,
        }

    def dump_state(self, max_candles: int | None = None) -> dict:
        engine = self.engine.dump_state(max_candles)
        last_break = asdict(self.last_break) if self.last_break else None
        if last_break:
            last_break["bar_index"] -= int(engine.get("index_offset", 0))
        return {"engine": engine, "bucket_start": self.bucket_start,
                "parts": [asdict(x) for x in self.parts],
                "last_break": last_break}

    def load_state(self, data: dict) -> None:
        self.engine.load_state(data["engine"])
        self.bucket_start = data.get("bucket_start")
        self.parts = [Candle(**x) for x in data.get("parts", [])]
        self.last_break = StructureBreak(**data["last_break"]) if data.get("last_break") else None
