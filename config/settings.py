"""
====================================================================
 SMC / MTF XAUUSD BOT  ---  CENTRAL CONFIG
====================================================================
 نسخه: CALIBRATION — برای تعیین scoring thresholds
 هدف: ~300-380 معامله با داده کامل برای تحلیل scoring
 بعد از تحلیل نتایج، به نسخه PRODUCTION برگرد
====================================================================
"""

# ==============================================================
#  TEST KNOBS
# ==============================================================
TEST = {
    "risk_mult": 1.0,    # خاموش — دست نزن
}

# --------------------------------------------------------------
# 1. INSTRUMENT  —  بدون تغییر
# --------------------------------------------------------------
INSTRUMENT = {
    "symbol":        "XAUUSD",
    "point":         0.01,
    "contract_size": 100,
    "min_lot":       0.01,
    "lot_step":      0.01,
    "pip_value":     0.10,
    "digits":        2,
}

# --------------------------------------------------------------
# 2. TIMEFRAMES  —  بدون تغییر
# --------------------------------------------------------------
TIMEFRAMES = {
    "htf":         "M15",
    "ltf":         "M5",
    "htf_minutes": 15,
    "ltf_minutes": 5,
}

# --------------------------------------------------------------
# 3. LOOKBACK WINDOWS
# --------------------------------------------------------------
LOOKBACK = {
    # ---- M15 (HTF) ----
    "htf_structure": 60,   # پنجره BOS — ثابت
    "htf_ob":        15,   # جستجوی OB — ثابت
    "htf_zone":      15,   # پنجره zone — با OB body fix دیگر zone width از اینجا نمی‌آید
    "htf_liquidity": 40,   # [★ CALIB: 30→40] بیشتر sweep data برای اندازه‌گیری
    "htf_fvg":       10,   # FVG impulse — ثابت

    # ---- M5 (LTF) ----
    "ltf_choch":     30,   # [★ CALIB: 35→30] با CHoCH fix که آخرین رو برمی‌گردونه کافیه
    "ltf_entry_fvg":  3,   # entry refinement — ثابت
    "ltf_sl_ref":     3,   # SL reference — ثابت
    "ltf_zone_arm":  25,   # [★ CALIB: 15→25] بیشتر زمان برای CHoCH → معاملات بیشتر
}

# --------------------------------------------------------------
# 4. FRACTAL / PIVOT SIZES  —  بدون تغییر
# --------------------------------------------------------------
FRACTALS = {
    "htf_swing_left":  2,
    "htf_swing_right": 2,
    "ltf_swing_left":  2,
    "ltf_swing_right": 2,
}

# --------------------------------------------------------------
# 5. ATR  —  بدون تغییر
# --------------------------------------------------------------
ATR = {
    "period":      14,
    "sl_cap_mult": 1.5,
    "min_atr":     0.0,
}

# --------------------------------------------------------------
# 5a. EMA TREND FILTER  —  خاموش (بدون edge در تست قبلی)
# --------------------------------------------------------------
EMA = {
    "enabled": False,
    "period":  200,
}

# --------------------------------------------------------------
# 5b. H1 MACRO TREND GATE
# ★ CRITICAL: باید روشن باشد — این fix اصلی ماست
# warmup_bars را حتماً ≥ 180 نگه دار (40 H1 × 4 = 160 M15 + buffer)
# --------------------------------------------------------------
HTF_TREND = {
    "enabled":          True,
    "tf_minutes":       60,
    "lookback":         40,   # 40 H1 bar = 160 M15 bar -> warmup باید ≥ 165 باشد
    "swing_left":       2,
    "swing_right":      2,
    "block_only_clear": True,
}

# --------------------------------------------------------------
# 5c. RSI  —  خاموش
# --------------------------------------------------------------
RSI = {
    "enabled":       False,
    "period":        14,
    "oversold":      45,
    "overbought":    65,
    "turn_lookback":  5,
    "lookback":      40,
}

# --------------------------------------------------------------
# 6. RISK & TRADE MANAGEMENT
# --------------------------------------------------------------
RISK = {
    "account_balance":    1000.0,
    "risk_per_trade":     0.01,
    "min_rr":             1.5,    # نگه دار — فیلتر RR کیفیتی مهم است
    "max_lot":            1.0,
    "sl_buffer_points":   20,
    "min_stop_points":    50,
    "stop_spread_mult":   5.0,
    "cost_buffer_points": 0,

    # ★ CALIB: cooldown=3→1  بیشتر معامله، داده کافی‌تر
    "cooldown_bars":      1,

    # ★ CALIB: max_open=3→1  یک معامله در یک زمان = داده تمیزتر، بدون تداخل
    "max_open_positions": 1,

    "tp_mode":            "liquidity",
    "tp_fallback":        "rr",
    "fixed_rr":           2.0,
}

# --------------------------------------------------------------
# 6b. TRADE MANAGEMENT  —  همه خاموش برای calibration خالص
# --------------------------------------------------------------
MANAGEMENT = {
    "breakeven_at_r":          0.0,   # خاموش
    "breakeven_buffer_points": 10,
    "trail_after_r":           0.0,   # خاموش
    "trail_distance_r":        1.0,
}

# --------------------------------------------------------------
# 7. SETUP VALIDITY FILTERS
#
# ★ CALIB PHILOSOPHY:
#   فیلترهایی که می‌خواهیم RANGE آن‌ها را اندازه بگیریم → خاموش
#   فیلترهایی که کیفیت fundamental سیگنال را تضمین می‌کنند → روشن
# --------------------------------------------------------------
FILTERS = {
    # ---- کیفیت fundamental: روشن نگه دار ----
    "require_fvg_overlap":     False,  # خاموش — تأثیر بزرگ روی تعداد دارد
    "require_liquidity_sweep": True,   # ★ روشن — sweep کیفیت اصلی setup است
    "one_trade_per_zone":      True,   # ★ روشن — جلوگیری از bias ناحیه‌ای

    # ---- برای CALIBRATION: خاموش — می‌خواهیم RANGE را ببینیم ----
    # این دو dimension را در backtest بدون فیلتر اندازه می‌گیریم
    # بعد از تحلیل log، threshold درست را تعیین می‌کنیم
    "bos_min_body_atr":        0.0,   # [★ CALIB: 0.3→0] اندازه‌گیری کامل، فیلتر نه
    "max_zone_dist_atr":       0.0,   # [★ CALIB: 0.5→0] اندازه‌گیری کامل، فیلتر نه

    # ---- کمی آزادتر برای داده بیشتر ----
    "not_extended_atr":        3.0,   # [★ CALIB: 2.5→3.0] +5-8% معامله بیشتر
    "zone_tap_tolerance_atr":  0.0,   # ثابت — tap دقیق
    "max_trades_per_zone":     2,
}

# --------------------------------------------------------------
# 7b. ZONE SHAPING  (برای OB body fix)
# --------------------------------------------------------------
ZONES = {
    # [TEST: 0.5 -> 1.0] wider zone catches more taps; the new M5-BOS
    # confirmation gate in mtf_strategy.py (STEP3c) is meant to offset the
    # quality cost of that width.
    "ob_atr_mult": 1.0,   # zone = OB body ± 1.0 * ATR
}

# --------------------------------------------------------------
# 8. BACKTEST / IO
# ★ CRITICAL: warmup_bars حتماً باید ≥ 165 باشد
#   دلیل: H1 gate نیاز به 40×4=160 کندل M15 دارد
#   با warmup=80 قبلی، اول 85 H1 bar معتبر رد می‌شد!
# --------------------------------------------------------------
BACKTEST = {
    "data_file":         "data/XAUUSD_M5.csv",
    "datetime_col":      "time",
    "tz":                "UTC",
    "spread_points":     20,
    "commission":        0.0,

    # ★★★ CRITICAL FIX: 80 → 180 ★★★
    # قبلاً: warmup=80 < h1_m15=160 → اولین 80 کندل بدون H1 gate اجرا می‌شد!
    "warmup_bars":       180,

    "verbose":           True,
    "entry_trigger":     "choch",
    "entry_mode":        "limit",
    "entry_expiry_bars": 12,    # [★ CALIB: 19→12] limit order منقضی‌تر، واقعی‌تر
    "slippage_points":   10,
}

# --------------------------------------------------------------
# 9b. SCORING GATE  (quality score after all binary filters pass)
#
#   Score  | Pass rate (>=N) — calibrated on 3905 real XAUUSD M15 samples
#   ───────|────────────────
#   >= 4   |   87.7%
#   >= 5   |   73.3%   ← current setting
#   >= 6   |   54.5%
#   >= 7   |   35.5%
# --------------------------------------------------------------
SCORING = {
    "enabled":   True,

    # ── H1 macro (0-2 pts) ──────────────────────────────────────────
    "h1_fresh_bars":     8,     # H1 BOS within N H1-bars counts as fresh

    # ── M15 BOS quality (0-3 pts) ────────────────────────────────────
    "bos_excess_weak":   0.35,  # close beyond broken swing / ATR  (p25) → +1
    "bos_excess_strong": 0.87,  # same metric at p50               → +1 cumulative
    "bos_body_min":      0.57,  # BOS candle body / ATR            (p50) → +1

    # ── M15 Sweep quality (0-2 pts) ──────────────────────────────────
    "sweep_depth_min":   0.13,  # spike past pool / ATR            (p25) → +1
    "sweep_speed_max":   8,     # M15 bars until close-back        (p50) → +1

    # ── M5 CHoCH quality (0-3 pts) ───────────────────────────────────
    "choch_fresh_good":  10,    # CHoCH <= 10 M5 bars old (50 min) → +1
    "choch_fresh_great":  5,    # CHoCH <=  5 M5 bars old (25 min) → +1 on top
    "choch_body_min":    0.30,  # CHoCH candle body / M5 ATR       → +1

    # ── threshold ────────────────────────────────────────────────────
    "min_score":         5,     # reject if total < 5  (73% pass rate)
    # NOTE: after OB-body fix, add ob_quality point and raise to 6
}

# --------------------------------------------------------------
# 9. LOGGING
# --------------------------------------------------------------
LOGGING = {
    "level":       "INFO",
    "log_file":    "logs/backtest.log",
    "log_signals": True,
}

# --------------------------------------------------------------
# 10. MT5  —  بدون تغییر
# --------------------------------------------------------------
MT5 = {
    "login":     5050273943,
    "password":  "!lKz4xIo",
    "server":    "MetaQuotes-Demo",
    "symbol":    "XAUUSD",
    "timeframe": "M5",
    "lookback_days": 90,
    "bars":      50_000,
    "date_from": None,
    "date_to":   None,
    "out_file":  None,
    "terminal_path": None,
}

# --------------------------------------------------------------
# 11. LIVE  —  بدون تغییر
# --------------------------------------------------------------
LIVE = {
    "enable_trading":      True,
    "require_demo":        True,
    "magic":               50273943,
    "deviation":           30,
    "poll_seconds":        5,
    "bars_to_pull":        1000,
    "use_live_balance":    True,
    "startup_test_trade":  True,
    "log_file":            "logs/live.log",
    "heartbeat_bars":      12,
}

# ==============================================================
#  یادداشت برای تحلیل بعد از backtest:
# ==============================================================
#
#  وقتی log آمد، به این بخش‌ها نگاه کن:
#
#  1. SETUP BREAKDOWN by direction:
#     → آیا SELL کمتر از قبل است؟ (H1 gate باید SELL کاهش بدهد)
#     → آیا BUY/SELL PF هر دو بهتر شد؟
#
#  2. SETUP BREAKDOWN by distance to zone:
#     → با max_zone_dist=OFF، بررسی کن کدام bucket بهترین PF دارد
#     → اگر 0-0.5 بهتر شد → threshold را برای scoring در نظر بگیر
#     → اگر همچنان 2+ بهترین بود → zone هنوز مشکل دارد (OB body fix نیاز است)
#
#  3. MAE ANALYSIS:
#     → آیا Q4 کاهش یافت؟ (CHoCH fix باید تأثیر بگذارد)
#     → اگر Q4 هنوز > 60%، CHoCH fix کامل نشده
#
#  4. FILTER DIAGNOSTICS:
#     → "against_h1_trend" چند درصد فیلتر کرد؟ (هدف: 30-35%)
#     → "no_bos" هنوز بزرگترین فیلتر؟ (طبیعی)
#
#  5. بعد از تحلیل، برای PRODUCTION config:
#     → cooldown_bars:    1 → 3
#     → max_open:         1 → 1  (نگه دار)
#     → bos_min_body_atr: 0 → threshold از تحلیل
#     → max_zone_dist_atr:0 → threshold از تحلیل
#     → warmup_bars:      180  (نگه دار!)
# ==============================================================