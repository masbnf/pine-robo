from __future__ import annotations

from dataclasses import asdict

from .models import PaperPosition


class DemoExecutor:
    """Mirror tick-opened paper positions to MT5, guarded to demo accounts."""

    def __init__(self, mt5, symbol: str, magic: int, deviation_points: int,
                 max_free_margin_fraction: float = 0.80) -> None:
        self.mt5 = mt5
        self.symbol = symbol
        self.magic = int(magic)
        self.deviation_points = int(deviation_points)
        self.max_free_margin_fraction = float(max_free_margin_fraction)
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if account is None or account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            raise RuntimeError("demo order sending refused: connected account is not DEMO")
        if terminal is None or not terminal.connected or not terminal.trade_allowed:
            raise RuntimeError("demo order sending refused: terminal trading is unavailable")
        if not account.trade_allowed:
            raise RuntimeError("demo order sending refused: account trading is unavailable")

    def open(self, position: PaperPosition) -> dict:
        if position.meta.get("demo_order_sent"):
            return {"status": "already_sent", "ticket": position.meta.get("demo_position_ticket")}
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None:
            raise RuntimeError(f"demo open failed: no tick for {self.symbol}")
        is_buy = position.direction == "bull"
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(position.volume),
            "type": self.mt5.ORDER_TYPE_BUY if is_buy else self.mt5.ORDER_TYPE_SELL,
            "price": float(tick.ask if is_buy else tick.bid),
            "sl": float(position.stop),
            "tp": float(position.target),
            "deviation": self.deviation_points,
            "magic": self.magic,
            "comment": f"pineob:{position.id[:12]}",
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        request, margin = self._fit_open_request(request)
        result = self.mt5.order_send(request)
        self._require_success(result, "open")
        ticket = int(result.order or result.deal)
        live_position = self._find_position(position.id, ticket)
        if live_position is not None:
            ticket = int(live_position.ticket)
        position.meta.update({"demo_order_sent": True,
                              "execution_state": "demo_confirmed_active",
                              "demo_position_ticket": ticket,
                              "demo_open_price": float(result.price),
                              "demo_entry_slippage": ((float(result.price) - position.entry)
                                                       if is_buy else
                                                       (position.entry - float(result.price))),
                              "demo_open_retcode": int(result.retcode)})
        return {"status": "opened", "ticket": ticket, "price": float(result.price),
                "volume": float(result.volume), "retcode": int(result.retcode),
                "requested_volume": float(request["volume"]), "calculated_margin": margin}

    def reconcile(self, paper_positions: list[PaperPosition]) -> dict:
        """Compare the restored paper ledger with managed MT5 positions.

        Reconciliation is deliberately read-only: mismatches are marked for
        operator visibility and never create or close a broker position.
        """
        managed = [item for item in (self.mt5.positions_get(symbol=self.symbol) or ())
                   if int(getattr(item, "magic", -1)) == self.magic]
        matched_tickets: set[int] = set()
        matched, missing = [], []
        for paper in paper_positions:
            ticket = paper.meta.get("demo_position_ticket")
            prefix = f"pineob:{paper.id[:12]}"
            found = next((item for item in managed
                          if ((ticket and int(item.ticket) == int(ticket)) or
                              getattr(item, "comment", "") == prefix)), None)
            if found is None:
                paper.meta["execution_state"] = "reconciliation_required_demo_missing"
                missing.append({"position_id": paper.id, "expected_ticket": ticket})
                continue
            actual_ticket = int(found.ticket)
            matched_tickets.add(actual_ticket)
            paper.meta.update({"demo_order_sent": True,
                               "demo_position_ticket": actual_ticket,
                               "execution_state": "demo_confirmed_active"})
            matched.append({"position_id": paper.id, "ticket": actual_ticket})
        orphans = [{"ticket": int(item.ticket),
                    "comment": str(getattr(item, "comment", "")),
                    "volume": float(getattr(item, "volume", 0.0))}
                   for item in managed if int(item.ticket) not in matched_tickets]
        return {"matched": matched, "missing": missing, "orphans": orphans}

    def modify_stop(self, position: PaperPosition, new_stop: float) -> dict:
        """Mirror a paper-side SL move (e.g. breakeven) to the broker position.

        TP is re-sent unchanged; MT5's TRADE_ACTION_SLTP replaces both levels
        so omitting it would clear the target.
        """
        mt5_position = self._find_position(position.id,
                                           position.meta.get("demo_position_ticket"))
        if mt5_position is None:
            return {"status": "position_not_found",
                    "ticket": position.meta.get("demo_position_ticket")}
        request = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": int(mt5_position.ticket),
            "sl": float(new_stop),
            "tp": float(position.target),
            "magic": self.magic,
        }
        result = self.mt5.order_send(request)
        self._require_success(result, "modify_stop")
        return {"status": "modified", "ticket": int(mt5_position.ticket),
                "sl": float(new_stop), "tp": float(position.target),
                "retcode": int(result.retcode)}

    def close(self, position_id: str, direction: str, ticket: int | None,
              volume: float) -> dict:
        mt5_position = self._find_position(position_id, ticket)
        if mt5_position is None:
            deal = self._find_exit_deal(ticket)
            return {"status": "already_closed", "ticket": ticket,
                    "price": float(deal.price) if deal is not None else None,
                    "volume": float(deal.volume) if deal is not None else None,
                    "deal_ticket": int(deal.ticket) if deal is not None else None}
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None:
            raise RuntimeError(f"demo close failed: no tick for {self.symbol}")
        was_buy = direction == "bull"
        request = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "position": int(mt5_position.ticket),
            "volume": float(min(volume, mt5_position.volume)),
            "type": self.mt5.ORDER_TYPE_SELL if was_buy else self.mt5.ORDER_TYPE_BUY,
            "price": float(tick.bid if was_buy else tick.ask),
            "deviation": self.deviation_points,
            "magic": self.magic,
            "comment": f"pineob-close:{position_id[:8]}",
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self.mt5.ORDER_FILLING_IOC,
        }
        result = self.mt5.order_send(request)
        self._require_success(result, "close")
        return {"status": "closed", "ticket": int(mt5_position.ticket),
                "price": float(result.price), "volume": float(result.volume),
                "retcode": int(result.retcode)}

    def _fit_open_request(self, request: dict) -> tuple[dict, float | None]:
        """Return the largest checked volume within the configured margin budget."""
        symbol_info = getattr(self.mt5, "symbol_info", lambda _symbol: None)(self.symbol)
        account = self.mt5.account_info()
        order_check = getattr(self.mt5, "order_check", None)
        calc_margin = getattr(self.mt5, "order_calc_margin", None)
        if symbol_info is None or order_check is None or calc_margin is None:
            return request, None
        minimum = float(symbol_info.volume_min)
        step = float(symbol_info.volume_step)
        candidate = min(float(request["volume"]), float(symbol_info.volume_max))
        candidate = int((candidate + 1e-12) / step) * step
        budget = float(getattr(account, "margin_free", getattr(account, "free_margin", 0.0)))
        budget *= self.max_free_margin_fraction
        accepted = {0, self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_DONE_PARTIAL}
        while candidate + 1e-12 >= minimum:
            trial = dict(request)
            trial["volume"] = round(candidate, 8)
            margin = self.mt5.order_calc_margin(trial["type"], self.symbol,
                                                trial["volume"], trial["price"])
            check = self.mt5.order_check(trial)
            if (margin is not None and float(margin) <= budget and check is not None
                    and int(check.retcode) in accepted):
                return trial, float(margin)
            candidate = round(candidate - step, 8)
        raise RuntimeError("demo open rejected: no volume fits margin and order_check constraints")

    def _find_exit_deal(self, ticket: int | None):
        history = getattr(self.mt5, "history_deals_get", None)
        if history is None or not ticket:
            return None
        deals = history(position=int(ticket)) or ()
        exit_kinds = {value for value in
                      (getattr(self.mt5, "DEAL_ENTRY_OUT", None),
                       getattr(self.mt5, "DEAL_ENTRY_OUT_BY", None)) if value is not None}
        candidates = [item for item in deals
                      if int(getattr(item, "magic", self.magic)) == self.magic
                      and (not exit_kinds or getattr(item, "entry", None) in exit_kinds)]
        return max(candidates, key=lambda item: getattr(item, "time_msc", 0)) if candidates else None

    def _find_position(self, position_id: str, ticket: int | None):
        if ticket:
            matches = self.mt5.positions_get(ticket=int(ticket))
            if matches:
                return matches[0]
        prefix = f"pineob:{position_id[:12]}"
        for item in self.mt5.positions_get(symbol=self.symbol) or ():
            if item.magic == self.magic and item.comment == prefix:
                return item
        return None

    def _require_success(self, result, action: str) -> None:
        if result is None:
            raise RuntimeError(f"demo {action} failed: {self.mt5.last_error()}")
        accepted = {self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_DONE_PARTIAL}
        if result.retcode not in accepted:
            raise RuntimeError(f"demo {action} rejected: retcode={result.retcode} comment={result.comment}")
