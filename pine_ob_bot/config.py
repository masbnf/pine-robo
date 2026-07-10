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
    # Closed M5 bars the reclaim may lag the sweep in sweep_reclaim mode.
    # 1 (default) = sweep and reclaim must happen on the SAME closed bar --
    # the original logic, unchanged. N>1 is experimental (enable via the
    # --sweep-reclaim-max-bars flag on the runners): a bar that wicks through
    # the entry edge opens an N-bar window, and any bar inside it that closes
    # back beyond the edge confirms; a deeper sweep restarts the window; OB
    # invalidation still kills the order at any point.
    sweep_reclaim_max_bars: int = 1
    # Shadow audit of "revive setups after the trend returns" (sweep_reclaim
    # only; enable via --revival-shadow-audit on the runners). NEVER creates,
    # fills or cancels real orders: trend-cancelled setups are additionally
    # tracked in a shadow list, and counters report how many would have been
    # revivable (trend back within 3/6/12 bars while the OB is still valid),
    # how many would have seen a FRESH sweep+reclaim after the return, and
    # the hypothetical R those fills would have produced.
    revival_shadow_audit: bool = False
    # Entry Pivot freshness: a confirmed entry pivot may admit new setups for
    # at most this many closed M5 bars past its CONFIRMATION bar (inclusive:
    # age == limit is still fresh). None = no age limit, i.e. the legacy
    # behaviour where the latest pivot stays valid forever.
    max_entry_pivot_age_bars: int | None = None
    # Move the stop to entry once open profit reaches this many R (e.g. 1.0).
    # None disables the breakeven move entirely. Read by
    # PaperBroker._apply_breakeven on every tick/candle that touches an open
    # position; the "breakeven_armed" stat counts activations.
    breakeven_trigger_r: float | None = None
    # ------------------------------------------------------------------
    # Experimental trade-frequency features. EVERY field below defaults to
    # off/None; they may only be enabled through their dedicated CLI flags
    # (--controlled-revival, --allow-ob-reentry,
    # --wait-for-spread-after-confirmation, --portfolio-risk-cap). With all
    # of them off the broker takes the exact legacy code paths -- guarded by
    # tests/test_legacy_regression.py against pre-change fixtures.
    # ------------------------------------------------------------------
    # Controlled Revival: when a pending sweep_reclaim setup is cancelled
    # purely by an M5 trend change, keep a suspended record; if the trend
    # returns to the original direction within revival_max_return_bars closed
    # bars while the OB is still valid, create a brand-new pending order
    # (new id, linked via metadata) that must earn a completely FRESH
    # sweep+reclaim after the return. The old order is never re-activated.
    # Weak CHoCH setups are never revived. Requires entry_mode=sweep_reclaim.
    controlled_revival: bool = False
    revival_max_return_bars: int = 6
    # When False (explicit opt-out), the revival order is created already
    # sweep-confirmed and fills on the next tick/bar without a fresh sweep.
    revival_require_fresh_sweep: bool = True
    revival_max_per_ob: int = 1
    # Controlled Re-entry: one additional, fully re-validated entry on the
    # same Order Block after the previous trade on it has CLOSED. Needs a
    # fresh sweep+reclaim (the first entry's sweep is never reusable), an
    # aligned trend, a still-valid OB and min_reentry_wait_bars of distance
    # from the exit. Attempt counting includes the first entry, so
    # max_entries_per_ob=2 means "the original entry plus one re-entry".
    # Requires entry_mode=sweep_reclaim. Weak CHoCH never re-enters.
    allow_ob_reentry: bool = False
    max_entries_per_ob: int = 2
    min_reentry_wait_bars: int = 1
    reentry_require_fresh_sweep: bool = True
    reentry_after_loss_only: bool = False
    # Spread Wait: instead of retrying a spread-blocked, already-confirmed
    # order indefinitely (the implicit legacy behaviour), place it in an
    # explicit waiting state bounded by at least one of the three limits;
    # cancel deterministically on timeout, trend change or OB invalidation.
    # Tick-execution paths only (live paper + tick backtest); the OHLC replay
    # has no per-tick spread so the flag is inert there.
    wait_for_spread_after_confirmation: bool = False
    spread_wait_max_seconds: float | None = None
    spread_wait_max_ticks: int | None = None
    spread_wait_max_bars: int | None = None
    # Portfolio risk cap: reject any fill whose projected TOTAL open risk
    # (sum of open positions' risk_money plus the new trade's sized risk,
    # as a fraction of current equity) exceeds this cap. Reject-only in v1:
    # volume is never silently reduced, and a rejected order is deactivated,
    # not retried. None keeps the legacy unlimited behaviour.
    portfolio_risk_cap: float | None = None
    # ------------------------------------------------------------------
    # M1 Entry Assist (experimental, tick paths only, all default OFF).
    # M5 stays the ONLY source of trend/swing/BOS/CHoCH/entry-pivot/OB/
    # setup direction; M1 may only refine ENTRY TIMING on an active, valid
    # M5 setup. Modes: "shadow" (observe-only hypothetical entries),
    # "sequence" (only disambiguate same-bar M5 sweep/reclaim ordering --
    # can veto a false M5 confirmation, creates nothing), "entry" (a valid
    # closed-bar M1 sweep+reclaim may confirm an active M5 setup early;
    # the fill still runs through the normal tick pipeline: spread,
    # spread-wait, position limit, portfolio cap, sizing). With
    # m1_entry_assist False no M1 bar is even built (zero overhead) and
    # behaviour is bit-identical to baseline.
    # ------------------------------------------------------------------
    m1_entry_assist: bool = False
    m1_assist_mode: str = "shadow"
    # Closed M1 bars the M1 reclaim may lag the M1 sweep.
    m1_reclaim_max_bars: int = 3
    # Cap of M1 confirmations (consumed or expired) per Order Block.
    m1_max_confirmations_per_ob: int = 1
    # Closed M1 bars an unfilled M1 confirmation stays valid before the
    # order reverts to the untouched M5 fallback path.
    m1_entry_expiry_bars: int = 5
    # Only CLOSED M1 bars may confirm (never the forming bar's high/low);
    # the fill then uses the first eligible tick after that close.
    m1_require_closed_bar: bool = True
    # In sequence/entry modes, veto an M5 same-bar confirmation whose M1
    # ordering shows the reclaim happened BEFORE the sweep. Never applies
    # in shadow mode (shadow must not change real behaviour).
    m1_use_sequence_validation: bool = True
    # Separate experiment: place the stop behind the real M1 sweep extreme
    # (plus buffer) instead of the M5 stop. Kept apart from entry assist so
    # entry and stop effects are never mixed. Default OFF.
    m1_refine_stop: bool = False
    m1_stop_atr_buffer: float = 0.20
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
        if self.max_entry_pivot_age_bars is not None and self.max_entry_pivot_age_bars < 1:
            raise ValueError("max_entry_pivot_age_bars must be positive or None")
        if self.state_history_bars < max(self.trend_swing_length, self.atr_period):
            raise ValueError("state history is shorter than strategy warm-up")
        if self.rr <= 0 or not 0 < self.risk_fraction <= 1:
            raise ValueError("rr and risk_fraction must be positive")
        if self.target_mode not in {"fixed_rr", "m5_liquidity_min_rr"}:
            raise ValueError("invalid target_mode")
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
        if self.sweep_reclaim_max_bars < 1:
            raise ValueError("sweep_reclaim_max_bars must be at least 1")
        if self.breakeven_trigger_r is not None and self.breakeven_trigger_r <= 0:
            raise ValueError("breakeven_trigger_r must be positive or None")
        if self.revival_max_return_bars < 1:
            raise ValueError("revival_max_return_bars must be at least 1")
        if self.revival_max_per_ob < 1:
            raise ValueError("revival_max_per_ob must be at least 1")
        if self.controlled_revival and self.entry_mode != "sweep_reclaim":
            raise ValueError("controlled_revival requires entry_mode='sweep_reclaim'")
        if self.max_entries_per_ob < 1:
            raise ValueError("max_entries_per_ob must be at least 1")
        if self.allow_ob_reentry and self.max_entries_per_ob < 2:
            raise ValueError("max_entries_per_ob must be at least 2 when re-entry is enabled")
        if self.min_reentry_wait_bars < 0:
            raise ValueError("min_reentry_wait_bars cannot be negative")
        if self.allow_ob_reentry and self.entry_mode != "sweep_reclaim":
            raise ValueError("allow_ob_reentry requires entry_mode='sweep_reclaim'")
        spread_wait_limits = (self.spread_wait_max_seconds, self.spread_wait_max_ticks,
                              self.spread_wait_max_bars)
        if self.wait_for_spread_after_confirmation and all(v is None for v in spread_wait_limits):
            raise ValueError("spread wait needs at least one of max seconds/ticks/bars")
        if any(v is not None and v <= 0 for v in spread_wait_limits):
            raise ValueError("spread wait limits must be positive when set")
        if not self.wait_for_spread_after_confirmation and any(v is not None
                                                               for v in spread_wait_limits):
            raise ValueError("spread wait limits require wait_for_spread_after_confirmation")
        if self.portfolio_risk_cap is not None and not 0 < self.portfolio_risk_cap <= 1:
            raise ValueError("portfolio_risk_cap must be in (0, 1] or None")
        if self.m1_assist_mode not in {"shadow", "sequence", "entry"}:
            raise ValueError("m1_assist_mode must be shadow, sequence or entry")
        if self.m1_reclaim_max_bars < 1:
            raise ValueError("m1_reclaim_max_bars must be at least 1")
        if self.m1_max_confirmations_per_ob < 1:
            raise ValueError("m1_max_confirmations_per_ob must be at least 1")
        if self.m1_entry_expiry_bars < 1:
            raise ValueError("m1_entry_expiry_bars must be at least 1")
        if self.m1_entry_assist and self.entry_mode != "sweep_reclaim":
            raise ValueError("m1_entry_assist requires entry_mode='sweep_reclaim'")
        if self.m1_refine_stop and not self.m1_entry_assist:
            raise ValueError("m1_refine_stop requires m1_entry_assist")
        if self.m1_stop_atr_buffer < 0:
            raise ValueError("m1_stop_atr_buffer cannot be negative")
        if self.max_open_positions < 1:
            raise ValueError("max_open_positions must be positive")
        # Values above 1 mean "burst fill": while the broker is flat, up to
        # max_open_positions pending orders may fill on the same tick/candle
        # (see tests/test_pine_ob_bot.py test_two_position_capacity_fills_two
        # _orders). Once at least one position is open, process_tick/
        # process_candle manage it and return without evaluating new fills, so
        # positions are never ADDED to an existing one -- capacity only applies
        # to simultaneous fills from a flat state.
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
