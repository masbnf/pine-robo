from __future__ import annotations

from dataclasses import InitVar, dataclass
from pathlib import Path


@dataclass(slots=True)
class BotConfig:
    symbol: str = "XAUUSD"
    timeframe: str = "M5"
    timeframe_minutes: int = 5
    # "Trend Swing": the state-based swing/BOS/CHoCH confirmation window that
    # PineSwingOBEngine uses for Major Swing High/Low, Major BOS/CHoCH and the
    # single M5 Trend State. 12 bars @ M5 = ~60 minutes of confirmation delay.
    trend_swing_length: int = 12
    # "Entry Pivot": the independent, classic symmetric pivot window used only
    # for local pullback/trigger timing (see entry_pivot.py). It never feeds
    # the Trend State. 5 bars @ M5 = ~25 minutes of confirmation delay on the
    # right side; the left side needs no future bars so it adds no delay.
    entry_pivot_left: int = 5
    entry_pivot_right: int = 5
    # Deprecated alias for trend_swing_length, kept only so old call sites and
    # CLI invocations using `swing_length=` keep working unchanged. Never read
    # internally -- use trend_swing_length. See __post_init__ below.
    swing_length: InitVar[int | None] = None
    atr_period: int = 200
    m15_swing_length: int = 10
    m15_atr_period: int = 200
    rr: float = 1.5
    target_mode: str = "fixed_rr"
    # Move the SL to entry exactly once when the trade's real MFE reaches this
    # many R (fractions of the initial entry-to-stop distance). None disables
    # the behaviour. The stop never moves backward, and no offset is applied
    # (a breakeven exit closes at entry, ~0 PnL). Shared by Paper live, Demo
    # mirroring and both backtest runners via PaperBroker._apply_breakeven.
    breakeven_trigger_r: float | None = None
    risk_fraction: float = 0.01
    liquidity_risk_sizing_enabled: bool = False
    choch_risk_sizing_enabled: bool = False
    choch_opposite_risk_fraction: float = 0.0125
    choch_base_risk_fraction: float = 0.0075
    combined_context_risk_sizing_enabled: bool = False
    combined_two_signal_risk_fraction: float = 0.0125
    combined_one_signal_risk_fraction: float = 0.01
    combined_zero_signal_risk_fraction: float = 0.0075
    displacement_risk_sizing_enabled: bool = False
    displacement_rank_window: int = 100
    displacement_rank_min_samples: int = 20
    displacement_top_risk_fraction: float = 0.0125
    displacement_base_risk_fraction: float = 0.0075
    choch_displacement_risk_sizing_enabled: bool = False
    choch_risk_cap_fraction: float | None = None
    three_factor_risk_sizing_enabled: bool = False
    bos_three_factor_risk_enabled: bool = False
    choch_split_risk_enabled: bool = False
    choch_toxic_risk_fraction: float = 0.0025
    choch_sweep_risk_fraction: float = 0.0055
    choch_other_risk_fraction: float = 0.0045
    bos_m15_counter_bonus_fraction: float = 0.0
    entry_age_rank_window: int = 100
    entry_age_rank_min_samples: int = 20
    liquidity_risk_fraction: float = 0.0125
    base_risk_fraction: float = 0.0075
    max_open_positions: int = 1
    allowed_break_kinds: tuple[str, ...] = ("BOS",)
    strong_choch_only: bool = False
    min_entry_wait_bars: int = 0
    entry_mode: str = "limit"
    sweep_reclaim_atr_buffer: float = 0.20
    entry_lifecycle_enabled: bool = False
    poll_seconds: float = 1.0
    fallback_spread: float = 0.20
    history_bars: int = 5000
    state_history_bars: int = 2000
    db_path: Path = Path("pine_ob_bot_data/paper_swing12.sqlite3")
    trades_csv: Path = Path("pine_ob_bot_data/trades_swing12.csv")
    summary_csv: Path = Path("pine_ob_bot_data/summary_swing12.csv")
    log_path: Path = Path("pine_ob_bot_data/paper_swing12.log")
    market_capture_enabled: bool = True
    market_capture_dir: Path = Path("pine_ob_bot_data/market_capture")
    max_live_tick_age_seconds: float = 30.0
    entry_spread_guard_enabled: bool = True
    max_entry_spread_atr_fraction: float = 0.10
    spread_median_window: int = 300
    spread_median_min_samples: int = 20
    spread_median_multiplier: float = 3.0
    daily_reports_dir: Path = Path("pine_ob_bot_data/daily_reports")
    report_timezone: str = "Asia/Tehran"
    report_refresh_seconds: float = 300.0
    breakdown_refresh_seconds: float = 900.0
    candle_poll_seconds: float = 5.0
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    demo_orders_enabled: bool = False
    demo_magic: int = 120512
    demo_deviation_points: int = 100
    demo_max_free_margin_fraction: float = 0.80

    def __post_init__(self, swing_length: int | None) -> None:
        # Deprecated-alias resolution. Runners/tests that still pass the old
        # `swing_length=` kwarg keep working, transparently mapped onto the
        # real field. New code must use trend_swing_length directly.
        if swing_length is not None:
            self.trend_swing_length = swing_length

    def validate(self) -> None:
        if self.timeframe != "M5" or self.timeframe_minutes != 5:
            raise ValueError("v1 supports closed M5 candles only")
        if min(self.trend_swing_length, self.atr_period, self.m15_swing_length,
               self.m15_atr_period) < 1:
            raise ValueError("lookback periods must be positive")
        if self.entry_pivot_left < 1 or self.entry_pivot_right < 1:
            raise ValueError("entry pivot windows must be positive")
        if self.state_history_bars < max(self.trend_swing_length, self.atr_period):
            raise ValueError("state history is shorter than strategy warm-up")
        if self.rr <= 0 or not 0 < self.risk_fraction <= 1:
            raise ValueError("rr and risk_fraction must be positive")
        if self.target_mode not in {"fixed_rr", "m5_liquidity_min_rr"}:
            raise ValueError("invalid target_mode")
        if self.breakeven_trigger_r is not None and self.breakeven_trigger_r <= 0:
            raise ValueError("breakeven_trigger_r must be positive")
        if not 0 < self.base_risk_fraction <= 1 or not 0 < self.liquidity_risk_fraction <= 1:
            raise ValueError("adaptive risk fractions must be positive")
        if not 0 < self.choch_base_risk_fraction <= 1 or not 0 < self.choch_opposite_risk_fraction <= 1:
            raise ValueError("CHoCH risk fractions must be positive")
        combined_fractions = (self.combined_zero_signal_risk_fraction,
                              self.combined_one_signal_risk_fraction,
                              self.combined_two_signal_risk_fraction)
        if any(not 0 < value <= 1 for value in combined_fractions):
            raise ValueError("combined context risk fractions must be positive")
        enabled_models = sum((self.choch_risk_sizing_enabled,
                              self.liquidity_risk_sizing_enabled,
                              self.combined_context_risk_sizing_enabled,
                              self.displacement_risk_sizing_enabled,
                              self.choch_displacement_risk_sizing_enabled,
                              self.three_factor_risk_sizing_enabled))
        if enabled_models > 1:
            raise ValueError("enable only one experimental risk-sizing model at a time")
        if self.displacement_rank_window < 1 or self.displacement_rank_min_samples < 1:
            raise ValueError("displacement rank periods must be positive")
        if not 0 < self.displacement_base_risk_fraction <= 1 or not 0 < self.displacement_top_risk_fraction <= 1:
            raise ValueError("displacement risk fractions must be positive")
        if self.choch_risk_cap_fraction is not None and not 0 < self.choch_risk_cap_fraction <= 1:
            raise ValueError("CHoCH risk cap must be positive")
        if self.entry_age_rank_window < 1 or self.entry_age_rank_min_samples < 1:
            raise ValueError("entry-age rank periods must be positive")
        experimental_fractions = (self.choch_toxic_risk_fraction,
                                  self.choch_sweep_risk_fraction,
                                  self.choch_other_risk_fraction)
        if any(not 0 < value <= 1 for value in experimental_fractions):
            raise ValueError("CHoCH split risk fractions must be positive")
        if not 0 <= self.bos_m15_counter_bonus_fraction <= 1:
            raise ValueError("BOS M15 counter bonus is invalid")
        if self.min_entry_wait_bars < 0 or not self.allowed_break_kinds:
            raise ValueError("entry filters are invalid")
        if self.entry_mode not in {"limit", "sweep_reclaim"}:
            raise ValueError("entry_mode must be 'limit' or 'sweep_reclaim'")
        if self.sweep_reclaim_atr_buffer < 0:
            raise ValueError("sweep_reclaim_atr_buffer cannot be negative")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be positive")
        if self.fallback_spread < 0:
            raise ValueError("fallback_spread cannot be negative")
        if self.max_live_tick_age_seconds <= 0:
            raise ValueError("max_live_tick_age_seconds must be positive")
        if self.max_entry_spread_atr_fraction <= 0 or self.spread_median_multiplier <= 1:
            raise ValueError("entry spread guard thresholds are invalid")
        if (self.spread_median_window < 1 or
                not 1 <= self.spread_median_min_samples <= self.spread_median_window):
            raise ValueError("entry spread guard window is invalid")
        if self.report_refresh_seconds <= 0:
            raise ValueError("report_refresh_seconds must be positive")
        if self.breakdown_refresh_seconds < self.report_refresh_seconds:
            raise ValueError("breakdown refresh cannot be faster than dashboard refresh")
        if self.candle_poll_seconds <= 0:
            raise ValueError("candle_poll_seconds must be positive")
        if not 0 <= self.dashboard_port <= 65535:
            raise ValueError("invalid dashboard port")
        if self.demo_magic <= 0 or self.demo_deviation_points < 0:
            raise ValueError("invalid demo execution settings")
        if not 0 < self.demo_max_free_margin_fraction <= 1:
            raise ValueError("demo margin fraction must be in (0, 1]")


# Deprecated read-only alias for trend_swing_length, attached AFTER the class
# body/decorator instead of via an in-body @property. `swing_length` is also
# the InitVar constructor-kwarg name above; a same-named @property inside the
# class body would be captured by @dataclass(slots=True) as that InitVar's
# default (overwriting the real `None` default with the property object
# itself, since dataclass reads the class namespace at decoration time --
# i.e. after the whole class body, including the later @property statement,
# has already executed). That silently corrupted every BotConfig() call built
# with no explicit swing_length=, replacing trend_swing_length with a
# <property object>. Attaching the property here, after BotConfig already
# exists as a finished (slotted) class, avoids the collision entirely.
BotConfig.swing_length = property(lambda self: self.trend_swing_length,
                                  doc="Deprecated read-only alias for trend_swing_length.")
