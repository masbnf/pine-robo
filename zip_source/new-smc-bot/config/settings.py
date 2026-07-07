"""
====================================================================
 SMC / MTF XAUUSD BOT  ---  CENTRAL CONFIG
====================================================================
This is the ONLY file you normally need to touch to tune the bot.
Every number the strategy uses lives here. Edit, save, re-run.

Values follow the Multi-Timeframe (MTF) Smart-Money model:
    M15 = bias & zones      M5 = trigger & execution
Tune each window +/-20% to your own data.
====================================================================
"""

# --------------------------------------------------------------
# 1. INSTRUMENT
# --------------------------------------------------------------
INSTRUMENT = {
    "symbol":        "XAUUSD",
    "point":         0.01,    # smallest price increment (gold = 0.01)
    "contract_size": 100,     # oz per 1.0 lot (standard XAUUSD = 100). pnl = move * cs * lot
    "min_lot":       0.01,    # broker minimum volume
    "lot_step":      0.01,    # broker volume step
    "pip_value":     0.10,    # legacy/reference only (live sizing uses broker tick value)
    "digits":        2,       # price decimal places
}

# --------------------------------------------------------------
# 2. TIMEFRAMES
# --------------------------------------------------------------
TIMEFRAMES = {
    "htf":   "M15",   # higher tf -> bias & zones
    "ltf":   "M5",    # lower  tf -> trigger & execution
    "htf_minutes": 15,
    "ltf_minutes": 5,
}

# --------------------------------------------------------------
# CLEAN SMC (mirrors SMC_clean_logic.pine)
# --------------------------------------------------------------
CLEAN_SMC = {
    "structure": "swing",       # "swing" (10) or "internal" (5)
    "swing_length": 10,
    "internal_length": 5,
    "structure_lookback": 120,
    "trade_events": ("BOS", "CHoCH"),
    "require_fvg_overlap": False,
    "retest_expiry_bars": 60,
    "htf_ema_enabled": False,
    "htf_ema_period": 200,
    "rr_ratio": 2.0,
}

# H1/M15 warm-up. Re-evaluated only when a closed M15 candle is received.
WARMUP = {
    "h1_swing_len": 1,
    "h1_lookback": 48,
    "m15_swing_len": 4,
    "m15_block_size": 16,
    "max_zones": 50,
    "max_ob_zones": 100,
    "merge_threshold": 1.0,
    "keep_last_clear_h1_trend": False,
    "require_zone_overlap": False,
    "h1_proximity": 2.0,
    "min_score_extra_life": 2,
    "base_max_sweeps": 2,
    "touched_max_sweeps": 3,
    "near_h1_max_sweeps": 4,
    "liquidity_sl_buffer_points": 10,
}

# --------------------------------------------------------------
# 3. LOOKBACK WINDOWS  (candles)  ---  the heart of the model
#    Keep these as FIXED rolling buffers, never growing slices.
# --------------------------------------------------------------
LOOKBACK = {
    # ---- M15 (HTF) ----
    "htf_structure":   60,   # BOS / swing structure scan (40-60)
    "htf_ob":          15,   # Order Block search, pre-impulse (10-15)
    "htf_zone":        15,   # Supply/Demand origin window  (10-15)
    "htf_liquidity":   50,   # BSL/SSL swing scan          (30-50)
    "htf_fvg":         15,   # HTF FVG impulse window, unfilled (10-15)

    # ---- M5 (LTF) ----
    "ltf_choch":       25,   # Entry CHoCH scan after tap   (15-25)
    "ltf_entry_fvg":    6,   # Entry FVG/OB refinement      (3-6 post-CHoCH)
    "ltf_sl_ref":       5,   # SL swing reference (immediate)(3-5)
    "ltf_zone_arm":    25,   # bars a tapped zone stays "armed" waiting for CHoCH
}

# --------------------------------------------------------------
# 4. FRACTAL / PIVOT SIZES  (candles each side of pivot)
# --------------------------------------------------------------
FRACTALS = {
    "htf_swing_left":   2,   # M15 swing pivot = 5-candle fractal (2/2)
    "htf_swing_right":  2,   #   use 3/3 for fewer, cleaner swings
    "ltf_swing_left":   2,   # M5 swing pivot = 3-5 fractal (1/1 .. 2/2)
    "ltf_swing_right":  2,
}

# --------------------------------------------------------------
# 5. ATR  (volatility filter + SL cap)
# --------------------------------------------------------------
ATR = {
    "period":       14,      # ATR lookback (on entry TF / M5)
    "sl_cap_mult":  1.5,     # max SL distance = sl_cap_mult * ATR
    "min_atr":      0.0,     # skip setups when ATR below this ($). 0 = off
}

# --------------------------------------------------------------
# 5a. EMA TREND FILTER  (HTF bias gate)
#     Only take BUYS when price is above the M15 EMA, SELLS when below.
#     Filters out setups that fight the bigger trend.
# --------------------------------------------------------------
EMA = {
    "enabled":  False,   # tested: halved trades, PF unchanged (1.54) -> no edge added
    "period":   200,     # EMA length on the M15 series (~the bigger trend)
}

# --------------------------------------------------------------
# 5b. RSI  (M5 entry confirmation — extra gate after the CHoCH)
#     For a BUY: RSI must have dipped into oversold recently AND be turning up.
#     For a SELL: RSI must have spiked to overbought recently AND be turning down.
# --------------------------------------------------------------
RSI = {
    "enabled":       False,  # False = ignore RSI entirely (tested: didn't improve PF)
    "period":        14,
    "oversold":      45,     # BUY needs RSI to have reached <= this
    "overbought":    65,     # SELL needs RSI to have reached >= this
    "turn_lookback": 5,      # within the last N M5 bars RSI must have hit the extreme
    "lookback":      40,     # bars used to compute the RSI series
}

# --------------------------------------------------------------
# 6. RISK & TRADE MANAGEMENT
# --------------------------------------------------------------
RISK = {
    "account_balance":  1000.0,  # starting equity for backtest sizing
    "risk_per_trade":   0.01,      # 1% of balance risked per trade
    "min_rr":           1.5,       # reject setups below this reward:risk (lower = more trades)
    "max_lot":          1.0,       # hard lot cap
    "sl_buffer_points": 20,        # extra points beyond swing for SL (20 * point)
    "min_stop_points":  50,        # reject setups whose stop is tighter than this ($0.50)
    "stop_spread_mult": 5.0,       # stop must be >= this * spread (cost must be small vs risk)
    "cost_buffer_points": 0,       # extra reward required to cover commission/slippage (points)
    "cooldown_bars":    3,         # wait N M5 bars after a trade before taking a new one
    "max_open_positions": 3,       # how many trades can be open at once (1 = old behaviour)
    "tp_mode":          "liquidity",  # "liquidity" -> next M15 BSL/SSL, or "rr"
    "tp_fallback":      "rr",      # when no liquidity is far enough: "rr" (fixed RR) or "skip"
    "fixed_rr":         2.0,       # used when tp_mode == "rr" or as the fallback target
}

# --------------------------------------------------------------
# 6b. TRADE MANAGEMENT  (dynamic stop handling after entry)
# --------------------------------------------------------------
MANAGEMENT = {
    "breakeven_at_r":        0.0,  # move SL to entry once price reaches +N x initial risk (0 = off)
    "breakeven_buffer_points": 10, # lock this many points beyond entry (covers costs) at breakeven
    "trail_after_r":         0.0,  # start trailing once price reaches +N x risk (0 = off)
    "trail_distance_r":      1.0,  # trailing stop sits this many R behind the bar extreme
}

# --------------------------------------------------------------
# 7. SETUP VALIDITY FILTERS
# --------------------------------------------------------------
#
#   >>> TRADE FREQUENCY DIAL <<<
#   Each filter you relax = MORE trades (but lower average quality).
#   Order of impact, loosest first:
#     1. require_fvg_overlap     -> False   (biggest jump)
#     2. require_liquidity_sweep -> False
#     3. zone_tap_tolerance_atr  -> raise   (0.3 .. 1.0)
#     4. one_trade_per_zone      -> False   (allow re-entries)
#     5. not_extended_atr        -> raise   (4.0+) or 0 to disable
#   For FEWER, higher-quality trades, do the opposite.
#
FILTERS = {
    "require_fvg_overlap":     False,  # zone must overlap an unfilled HTF FVG
    "require_liquidity_sweep": True,  # require SSL/BSL sweep before entry
    "not_extended_atr":        2.5,   # reject if price > N*ATR from BOS origin (0 = off)
    "one_trade_per_zone":      True,  # True = cap of 1 per zone. False = use max_trades_per_zone
    "max_trades_per_zone":     2,     # max entries allowed from the same M15 zone (balance)
    "zone_tap_tolerance_atr":  0.0,   # count price within N*ATR of the zone as a tap
}

# --------------------------------------------------------------
# 8. BACKTEST / IO
# --------------------------------------------------------------
BACKTEST = {
    "data_file":      "data/XAUUSD_M5.csv",  # M5 OHLC source (resampled to M15)
    "datetime_col":   "time",
    "tz":             "UTC",
    "spread_points":  20,        # simulated spread (20 * point = $0.20)
    "commission":     0.0,       # per-trade commission ($)
    "warmup_bars":    80,        # bars to skip before trading (>= max htf window)
    "verbose":        True,
    # --- entry trigger & fill realism ---
    "entry_trigger":  "choch",   # market entry after BOS -> OB retest -> internal CHoCH
                                 # "fvg"   = limit order at the FVG mid (wait for retrace)
    "entry_mode":     "limit",   # (fvg only) "limit" = fill if price reaches level; "instant" = old
    "entry_expiry_bars": 19,      # M5 bars a limit order stays live before it's cancelled
    "slippage_points": 10,       # adverse slippage on entry & SL fills (10 = $0.10)
    "rr_probe_horizon_bars": 288, # shadow-only: test R targets for 24h after entry
}

# --------------------------------------------------------------
# 9. LOGGING
# --------------------------------------------------------------
LOGGING = {
    "level":        "INFO",      # DEBUG | INFO | WARNING | ERROR
    "log_file":     "logs/backtest.log",
    "log_signals":  False,  # True for per-trade detail; False keeps full runs fast
}

# --------------------------------------------------------------
# 10. METATRADER 5  (used by tools/fetch_mt5_data.py)
#     NOTE: keep real credentials out of git. This is a DEMO account.
# --------------------------------------------------------------
MT5 = {
    "login":     5050273943,
    "password":  "!lKz4xIo",
    "server":    "MetaQuotes-Demo",
    "symbol":    "XAUUSD",     # auto-falls back to any symbol containing "XAU"
    "timeframe": "M5",         # M1 M5 M15 M30 H1 H4 D1
    "lookback_days": 180,       # download by DATE: last N days (90 = ~3 months). None = use bars
    "bars":      50_000,       # used only when lookback_days is None
    # --- explicit date range (for out-of-sample tests). Set both to use it. ---
    "date_from": "2025-06-26",         # OOS window start (UTC)
    "date_to":   "2025-12-26",        # OOS window end
    "out_file":  None,         # None = write straight to BACKTEST['data_file']
    # MT5 terminal path (only needed if auto-connect can't find it). e.g.:
    # "terminal_path": r"C:\\Program Files\\MetaTrader 5\\terminal64.exe",
    "terminal_path": None,
}

# --------------------------------------------------------------
# 11. LIVE FORWARD-TEST  (used by run_live.py)  —  DEMO ONLY
# --------------------------------------------------------------
LIVE = {
    "enable_trading":  True,   # False = detect & LOG signals only (no orders).
                                #         Flip to True to actually place DEMO orders.
    "require_demo":    True,    # refuse to run if the account is not a demo account
    "magic":           50273943,  # tag so the bot only manages its own trades
    "deviation":       30,      # max slippage (points) allowed on market orders
    "poll_seconds":    5,       # how often to check for a newly-closed M5 bar
    "bars_to_pull":    1000,    # recent M5 bars fetched each cycle (>= all lookbacks)
    "use_live_balance": True,   # size positions from live equity (else config balance)
}
