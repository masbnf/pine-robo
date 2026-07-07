"""Integration tests: the real execution path (PaperBroker._open) must use
the shared, safe sizing function -- and Demo must never diverge from what
Paper already computed.

Run with:  pytest pine_ob_bot/tests/test_paper_broker_sizing.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.demo_executor import DemoExecutor
    from pine_ob_bot.models import PendingOrder
    from pine_ob_bot.paper import PaperBroker, SymbolSpec
    from pine_ob_bot.position_sizing import (
        CALCULATED_VOLUME_BELOW_BROKER_MINIMUM, calculate_safe_position_size)
    from pine_ob_bot.trend_filter import TrendDirection
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from demo_executor import DemoExecutor
    from models import PendingOrder
    from paper import PaperBroker, SymbolSpec
    from position_sizing import CALCULATED_VOLUME_BELOW_BROKER_MINIMUM, calculate_safe_position_size
    from trend_filter import TrendDirection


def _make_order(entry: float, stop: float, target: float = 0.0) -> PendingOrder:
    return PendingOrder(id="order-1", ob_id="ob-1", direction="bull", entry=entry,
                        stop=stop, target=target or entry + 2 * abs(entry - stop),
                        created_index=0, created_time="2024-01-01T00:00:00Z",
                        active=True, meta={}, lifecycle_state="armed")


def test_open_rejects_and_creates_no_position_when_below_broker_minimum():
    """Requirement scenario: Equity=10,000, Risk%=1%% -> the calculated lot is
    smaller than volume_min. No PaperPosition is created (so nothing is ever
    mirrored to MT5 Demo either), the pending order is not left to retry, and
    the reason is recorded for logging/DB."""
    cfg = BotConfig(risk_fraction=0.01)
    spec = SymbolSpec(tick_size=0.01, tick_value=1.0, volume_min=0.01,
                      volume_max=100.0, volume_step=0.01)
    broker = PaperBroker(cfg, equity=10_000.0, spec=spec)
    order = _make_order(entry=2000.0, stop=1000.0)  # distance=1000 -> tiny raw volume

    position = broker._open(order, fill=order.entry, when="2024-01-01T00:05:00Z",
                            bar_index=0, execution_source="test", execution_spread=0.0,
                            stop=order.stop, target=order.target)

    assert position is None
    assert broker.positions == []
    assert order.active is False
    assert order.meta["rejection_reason"] == CALCULATED_VOLUME_BELOW_BROKER_MINIMUM
    assert broker.stats["rejected_volume_below_minimum"] == 1
    assert len(broker.rejected_sizing) == 1
    rejection = broker.rejected_sizing[0]
    assert rejection["rejection_reason"] == CALCULATED_VOLUME_BELOW_BROKER_MINIMUM
    assert rejection["allowed_risk"] == 100.0
    assert rejection["attempted_volume"] != spec.volume_min  # never silently raised


def test_open_accepts_and_matches_shared_sizing_function():
    """When sizing is valid, the position volume/risk must equal exactly what
    calculate_safe_position_size returns for the same inputs -- proving
    PaperBroker has no separate/duplicate sizing math."""
    cfg = BotConfig(risk_fraction=0.01)
    spec = SymbolSpec(tick_size=0.01, tick_value=1.0, volume_min=0.01,
                      volume_max=100.0, volume_step=0.01)
    broker = PaperBroker(cfg, equity=10_000.0, spec=spec)
    broker.update_m5_trend(TrendDirection.BULLISH, "2024-01-01T00:00:00Z")
    order = _make_order(entry=1.00, stop=0.90)

    position = broker._open(order, fill=order.entry, when="2024-01-01T00:05:00Z",
                            bar_index=0, execution_source="test", execution_spread=0.0,
                            stop=order.stop, target=order.target)

    expected = calculate_safe_position_size(
        equity=10_000.0, risk_percent=0.01, entry_price=1.00, stop_loss=0.90,
        tick_size=spec.tick_size, tick_value=spec.tick_value, volume_min=spec.volume_min,
        volume_max=spec.volume_max, volume_step=spec.volume_step)

    assert position is not None
    assert position.volume == expected.volume
    assert position.risk_money == expected.actual_risk
    assert broker.positions == [position]
    assert broker.rejected_sizing == []


# -- Fakes for exercising the real DemoExecutor code without a live MT5 terminal --

class _FakeSymbolInfo:
    def __init__(self, volume_min, volume_max, volume_step):
        self.volume_min, self.volume_max, self.volume_step = volume_min, volume_max, volume_step


class _FakeAccount:
    def __init__(self, trade_mode):
        self.trade_mode = trade_mode
        self.margin_free = 1_000_000.0
        self.trade_allowed = True


class _FakeTerminal:
    connected = True
    trade_allowed = True


class _FakeCheck:
    retcode = 0


class _FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 0
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010

    def __init__(self, spec: SymbolSpec):
        self.spec = spec
        self._account = _FakeAccount(self.ACCOUNT_TRADE_MODE_DEMO)
        self._terminal = _FakeTerminal()

    def account_info(self):
        return self._account

    def terminal_info(self):
        return self._terminal

    def symbol_info(self, symbol):
        return _FakeSymbolInfo(self.spec.volume_min, self.spec.volume_max, self.spec.volume_step)

    def order_calc_margin(self, order_type, symbol, volume, price):
        return 1.0  # trivial: always affordable, isolates the volume-fit logic

    def order_check(self, request):
        return _FakeCheck()


def test_paper_and_demo_never_diverge_on_volume():
    """Demo mirrors whatever Paper already sized; DemoExecutor._fit_open_request
    only ever narrows the volume for margin, it never recomputes risk or bumps
    volume toward/above volume_min. With margin trivially affordable here, the
    fitted volume must come back identical to what Paper computed -- proving
    both execution paths agree on risk for the same setup."""
    cfg = BotConfig(risk_fraction=0.01)
    spec = SymbolSpec(tick_size=0.01, tick_value=1.0, volume_min=0.01,
                      volume_max=100.0, volume_step=0.01)
    broker = PaperBroker(cfg, equity=10_000.0, spec=spec)
    broker.update_m5_trend(TrendDirection.BULLISH, "2024-01-01T00:00:00Z")
    order = _make_order(entry=1.00, stop=0.90)

    position = broker._open(order, fill=order.entry, when="2024-01-01T00:05:00Z",
                            bar_index=0, execution_source="tick", execution_spread=0.0,
                            stop=order.stop, target=order.target)
    assert position is not None

    fake_mt5 = _FakeMT5(spec)
    executor = DemoExecutor(fake_mt5, symbol="XAUUSD", magic=1, deviation_points=10)
    request = {"type": fake_mt5.ORDER_TYPE_BUY, "price": 1.00, "volume": float(position.volume)}
    fitted_request, margin = executor._fit_open_request(request)

    assert margin == 1.0
    assert fitted_request["volume"] == position.volume  # untouched, never raised
