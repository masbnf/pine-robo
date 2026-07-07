from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Direction = Literal["bull", "bear"]


@dataclass(slots=True)
class Candle:
    time: str
    open: float
    high: float
    low: float
    close: float


@dataclass(slots=True)
class Tick:
    time: str
    bid: float
    ask: float


@dataclass(slots=True)
class Pivot:
    level: float
    bar_index: int
    time: str
    crossed: bool = False


@dataclass(slots=True)
class StructureBreak:
    direction: Direction
    kind: Literal["BOS", "CHoCH"]
    pivot_level: float
    bar_index: int
    time: str


@dataclass(slots=True)
class OrderBlock:
    id: str
    direction: Direction
    high: float
    low: float
    source_index: int
    source_time: str
    formed_index: int
    formed_time: str
    active: bool = True
    break_kind: str = "unknown"
    atr_at_formation: float | None = None

    @property
    def entry(self) -> float:
        return self.high if self.direction == "bull" else self.low

    @property
    def stop(self) -> float:
        return self.low if self.direction == "bull" else self.high


@dataclass(slots=True)
class PendingOrder:
    id: str
    ob_id: str
    direction: Direction
    entry: float
    stop: float
    target: float
    created_index: int
    created_time: str
    active: bool = True
    meta: dict[str, Any] = field(default_factory=dict)
    lifecycle_state: str = "formed"
    accepted_index: int | None = None
    armed_index: int | None = None


@dataclass(slots=True)
class PaperPosition:
    id: str
    order_id: str
    direction: Direction
    entry: float
    stop: float
    target: float
    volume: float
    risk_money: float
    opened_time: str
    opened_index: int | None = None
    max_adverse: float = 0.0
    max_favorable: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Trade:
    id: str
    direction: Direction
    entry: float
    stop: float
    target: float
    exit_price: float
    volume: float
    risk_money: float
    pnl: float
    r_multiple: float
    opened_time: str
    closed_time: str
    result: Literal["win", "loss"]
    order_id: str = ""
    ob_id: str = ""
    opened_index: int | None = None
    closed_index: int | None = None
    max_adverse_r: float = 0.0
    max_favorable_r: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


def to_dict(value: Any) -> dict[str, Any]:
    return asdict(value)
