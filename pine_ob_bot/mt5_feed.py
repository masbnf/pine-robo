from __future__ import annotations

import os
from datetime import datetime, timezone

from .config import BotConfig
from .models import Candle, Tick
from .paper import SymbolSpec


class MT5ReadOnlyFeed:
    """Read-only MT5 adapter. Deliberately exposes no order_send operation."""
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise RuntimeError("MetaTrader5 is required on Windows: pip install MetaTrader5") from exc
        self.mt5 = mt5
        self.symbol = cfg.symbol

    def connect(self) -> tuple[float, SymbolSpec]:
        kwargs = self._credentials()
        terminal = kwargs.pop("terminal_path", None)
        ok = self.mt5.initialize(terminal, **kwargs) if terminal else self.mt5.initialize(**kwargs)
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {self.mt5.last_error()}")
        account = self.mt5.account_info()
        if account is None:
            raise RuntimeError("MT5 account_info unavailable")
        self.symbol = self._resolve_symbol(self.cfg.symbol)
        info = self.mt5.symbol_info(self.symbol)
        if info is None:
            raise RuntimeError(f"symbol unavailable: {self.symbol}")
        spec = SymbolSpec(
            tick_size=float(info.trade_tick_size or info.point or 0.01),
            tick_value=float(info.trade_tick_value or 1.0),
            volume_min=float(info.volume_min or 0.01),
            volume_max=float(info.volume_max or 100.0),
            volume_step=float(info.volume_step or 0.01),
        )
        return float(account.equity), spec

    def _credentials(self) -> dict:
        result = {}
        if os.getenv("MT5_LOGIN"):
            result["login"] = int(os.environ["MT5_LOGIN"])
            result["password"] = os.getenv("MT5_PASSWORD", "")
            result["server"] = os.getenv("MT5_SERVER", "")
            if os.getenv("MT5_TERMINAL_PATH"):
                result["terminal_path"] = os.environ["MT5_TERMINAL_PATH"]
            return result
        # Compatibility with the existing project config without copying secrets.
        try:
            from config import settings
            source = settings.MT5
            result = {"login": int(source["login"]), "password": source["password"],
                      "server": source["server"]}
            if source.get("terminal_path"):
                result["terminal_path"] = source["terminal_path"]
        except (ImportError, KeyError, TypeError, ValueError):
            pass
        return result

    def _resolve_symbol(self, wanted: str) -> str:
        if self.mt5.symbol_select(wanted, True) and self.mt5.symbol_info(wanted):
            return wanted
        for symbol in self.mt5.symbols_get() or []:
            if "XAU" in symbol.name.upper() and self.mt5.symbol_select(symbol.name, True):
                return symbol.name
        raise RuntimeError(f"no XAU symbol matching {wanted}")

    def closed_candles(self, count: int) -> list[Candle]:
        rates = None
        # Some terminals cap chart history below the requested bootstrap size.
        # Retry smaller windows while retaining enough data for ATR(200).
        attempts = []
        for size in (count + 1, 2001, 1001, 501, 251):
            if size not in attempts and size <= count + 1:
                attempts.append(size)
        for size in attempts:
            rates = self.mt5.copy_rates_from_pos(self.symbol, self.mt5.TIMEFRAME_M5, 0, size)
            if rates is not None and len(rates) >= min(202, size):
                break
        if rates is None or len(rates) < 2:
            raise RuntimeError(f"MT5 rates unavailable: {self.mt5.last_error()}")
        unique: dict[int, Candle] = {}
        for row in rates[:-1]:  # current bar is still forming
            ts = datetime.fromtimestamp(int(row["time"]), timezone.utc).isoformat()
            unique[int(row["time"])] = Candle(ts, float(row["open"]), float(row["high"]),
                                               float(row["low"]), float(row["close"]))
        return [unique[k] for k in sorted(unique)]

    def tick(self) -> Tick | None:
        value = self.mt5.symbol_info_tick(self.symbol)
        if value is None or value.bid <= 0 or value.ask <= 0:
            return None
        stamp = getattr(value, "time_msc", 0) / 1000 or value.time
        return Tick(datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                    float(value.bid), float(value.ask))

    def shutdown(self) -> None:
        self.mt5.shutdown()
