"""Breakeven-at-R behaviour of the shared PaperBroker execution engine.

Requirements under test:
  - when the trade's real MFE reaches breakeven_trigger_r, the SL moves to
    entry exactly once (no offset);
  - the stop never moves backward and never re-arms;
  - the closed trade records breakeven_armed, breakeven_armed_time,
    breakeven_exit, original_stop and final_stop;
  - the same broker code runs the tick path (Paper live / Demo / tick
    backtest) and the OHLC replay path, so both are exercised;
  - Demo mirrors the SL move via TRADE_ACTION_SLTP without touching TP.

Run with:  pytest pine_ob_bot/tests/test_breakeven.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from pine_ob_bot.config import BotConfig
    from pine_ob_bot.demo_executor import DemoExecutor
    from pine_ob_bot.models import Candle, PendingOrder, Tick
    from pine_ob_bot.paper import PaperBroker, SymbolSpec
    from pine_ob_bot.trend_filter import TrendDirection
except ImportError:  # pytest invoked from inside pine_ob_bot/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from config import BotConfig
    from demo_executor import DemoExecutor
    from models import Candle, PendingOrder, Tick
    from paper import PaperBroker, SymbolSpec
    from trend_filter import TrendDirection

T = "2024-01-01T00:{:02d}:00+00:00".format


def _make_order(direction: str, entry: float, stop: float, target: float) -> PendingOrder:
    return PendingOrder(id="order-1", ob_id="ob-1", direction=direction, entry=entry,
                        stop=stop, target=target, created_index=0,
                        created_time=T(0), active=True, meta={},
                        lifecycle_state="armed")


def _open_position(broker: PaperBroker, direction: str, entry: float,
                   stop: float, target: float):
    trend = TrendDirection.BULLISH if direction == "bull" else TrendDirection.BEARISH
    broker.update_m5_trend(trend, T(0))
    order = _make_order(direction, entry, stop, target)
    position = broker._open(order, fill=entry, when=T(1), bar_index=0,
                            execution_source="tick", execution_spread=0.0,
                            stop=stop, target=target)
    assert position is not None
    return position


def _broker(trigger: float | None) -> PaperBroker:
    cfg = BotConfig(risk_fraction=0.01, breakeven_trigger_r=trigger)
    return PaperBroker(cfg, equity=10_000.0, spec=SymbolSpec())


def test_tick_path_arms_once_at_trigger_and_never_re_arms():
    broker = _broker(0.5)
    p = _open_position(broker, "bull", entry=1.00, stop=0.90, target=1.20)

    broker.process_tick(Tick(T(2), 1.04, 1.04))  # MFE 0.4R: below trigger
    assert p.stop == 0.90
    assert not p.meta.get("breakeven_armed")
    assert broker.breakeven_events == []

    broker.process_tick(Tick(T(3), 1.05, 1.05))  # MFE 0.5R: arm
    assert p.stop == 1.00  # moved to entry, no offset
    assert p.meta["breakeven_armed"] is True
    assert p.meta["breakeven_armed_time"] == T(3)
    assert broker.stats["breakeven_armed"] == 1
    assert len(broker.breakeven_events) == 1
    event = broker.breakeven_events[0]
    assert event["original_stop"] == 0.90 and event["new_stop"] == 1.00

    broker.process_tick(Tick(T(4), 1.08, 1.08))  # deeper MFE: no second arm
    assert broker.stats["breakeven_armed"] == 1
    assert len(broker.breakeven_events) == 1
    assert p.stop == 1.00  # never moves again (and never backward)


def test_breakeven_exit_records_report_fields():
    broker = _broker(0.5)
    _open_position(broker, "bull", entry=1.00, stop=0.90, target=1.20)
    broker.process_tick(Tick(T(2), 1.05, 1.05))  # arm
    trades = broker.process_tick(Tick(T(3), 1.00, 1.00))  # back to entry: stopped

    assert len(trades) == 1
    trade = trades[0]
    assert trade.pnl == 0.0
    assert trade.meta["breakeven_armed"] is True
    assert trade.meta["breakeven_armed_time"] == T(2)
    assert trade.meta["breakeven_exit"] is True
    assert trade.meta["original_stop"] == 0.90
    assert trade.meta["final_stop"] == 1.00
    # MAE/MFE R remain measured against the original risk distance.
    assert abs(trade.max_favorable_r - 0.5) < 1e-9


def test_win_after_arming_is_not_a_breakeven_exit():
    broker = _broker(0.5)
    _open_position(broker, "bull", entry=1.00, stop=0.90, target=1.20)
    broker.process_tick(Tick(T(2), 1.05, 1.05))  # arm
    trades = broker.process_tick(Tick(T(3), 1.20, 1.20))  # target

    assert len(trades) == 1
    trade = trades[0]
    assert trade.result == "win"
    assert trade.meta["breakeven_armed"] is True
    assert trade.meta["breakeven_exit"] is False
    assert trade.meta["final_stop"] == 1.00


def test_bear_direction_arms_and_exits_at_entry():
    broker = _broker(0.5)
    p = _open_position(broker, "bear", entry=1.00, stop=1.10, target=0.80)

    broker.process_tick(Tick(T(2), 0.95, 0.95))  # MFE 0.5R: arm
    assert p.stop == 1.00
    trades = broker.process_tick(Tick(T(3), 1.00, 1.00))

    assert len(trades) == 1
    assert trades[0].meta["breakeven_exit"] is True
    assert trades[0].meta["original_stop"] == 1.10
    assert trades[0].meta["final_stop"] == 1.00


def test_disabled_by_default_stop_never_moves():
    broker = _broker(None)
    p = _open_position(broker, "bull", entry=1.00, stop=0.90, target=1.20)
    broker.process_tick(Tick(T(2), 1.08, 1.08))

    assert p.stop == 0.90
    assert not p.meta.get("breakeven_armed")
    assert broker.breakeven_events == []
    trades = broker.process_tick(Tick(T(3), 1.00, 1.00))
    assert trades == []  # original stop untouched, position still open


def test_candle_replay_path_shares_the_same_logic():
    broker = _broker(0.5)
    p = _open_position(broker, "bull", entry=1.00, stop=0.90, target=1.20)

    broker.process_candle(Candle(T(5), 1.01, 1.06, 1.01, 1.04), bar_index=1)
    assert p.stop == 1.00
    assert p.meta["breakeven_armed"] is True

    trades = broker.process_candle(Candle(T(10), 1.03, 1.04, 0.99, 1.02), bar_index=2)
    assert len(trades) == 1
    assert trades[0].exit_price == 1.00  # closed at the moved stop
    assert trades[0].meta["breakeven_exit"] is True


# -- Demo SL mirroring ------------------------------------------------------

class _FakeAccount:
    trade_mode = 0
    trade_allowed = True
    margin_free = 1_000_000.0


class _FakeTerminal:
    connected = True
    trade_allowed = True


class _FakeMT5Position:
    ticket = 777
    magic = 1
    comment = ""


class _FakeResult:
    retcode = 10009
    comment = "done"


class _FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    TRADE_ACTION_SLTP = 6
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010

    def __init__(self):
        self.sent_requests = []

    def account_info(self):
        return _FakeAccount()

    def terminal_info(self):
        return _FakeTerminal()

    def positions_get(self, **kwargs):
        return [_FakeMT5Position()]

    def order_send(self, request):
        self.sent_requests.append(request)
        return _FakeResult()


def test_demo_modify_stop_sends_sltp_with_unchanged_tp():
    broker = _broker(0.5)
    p = _open_position(broker, "bull", entry=1.00, stop=0.90, target=1.20)
    p.meta["demo_position_ticket"] = 777
    fake = _FakeMT5()
    executor = DemoExecutor(fake, symbol="XAUUSD", magic=1, deviation_points=10)

    result = executor.modify_stop(p, new_stop=1.00)

    assert result["status"] == "modified"
    assert len(fake.sent_requests) == 1
    request = fake.sent_requests[0]
    assert request["action"] == fake.TRADE_ACTION_SLTP
    assert request["position"] == 777
    assert request["sl"] == 1.00
    assert request["tp"] == 1.20  # TP re-sent unchanged
