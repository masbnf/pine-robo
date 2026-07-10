"""project_learning_journal/lab_experiments.py

رجیستری و پیاده‌سازی آزمایش‌های تعاملی ژورنال آموزشی pine_ob_bot.

قواعد امنیتی این فایل (رعایت‌شده در سراسر کد):
  * هیچ eval/exec/subprocess/os.system روی ورودی کاربر وجود ندارد.
  * هیچ مسیر فایلی از ورودی کاربر ساخته نمی‌شود؛ تنها مسیر ثابت و از پیش
    تعیین‌شدهٔ دیتاست آموزشی (data_samples/mini_backtest_sample.csv) خوانده می‌شود.
  * هیچ اتصال شبکه/MT5 برقرار نمی‌شود.
  * فقط توابع/کلاس‌های واقعی پکیج pine_ob_bot فراخوانی می‌شوند (نه بازنویسی
    منطق تجاری)، مگر در مواردی که صریحاً به‌عنوان «نسخهٔ آموزشی ساده‌شده»
    علامت‌گذاری شده باشد.
  * هر اجرا در یک پوشهٔ موقتِ داخل runtime_data/ ایزوله می‌شود و در پایان پاک
    می‌شود؛ هیچ فایلی در pine_ob_bot_data/ یا مسیرهای اصلی پروژه نوشته نمی‌شود.

این فایل توسط serve_journal.py وارد می‌شود. اجرای مستقیم این فایل کاری انجام
نمی‌دهد.
"""
from __future__ import annotations

import csv
import math
import shutil
import uuid
from dataclasses import asdict
from pathlib import Path

from pine_ob_bot.config import BotConfig
from pine_ob_bot.entry_pivot import ClassicEntryPivotDetector
from pine_ob_bot.historical import run_historical
from pine_ob_bot.models import Candle, OrderBlock, PaperPosition, PendingOrder, Tick
from pine_ob_bot.paper import PaperBroker, SymbolSpec
from pine_ob_bot.pine_engine import PineSwingOBEngine
from pine_ob_bot.position_sizing import calculate_safe_position_size
from pine_ob_bot.trend_filter import TrendDirection, validate_trade_direction

HERE = Path(__file__).resolve().parent
DATA_SAMPLES_DIR = HERE / "data_samples"
RUNTIME_DIR = HERE / "runtime_data"
RUNTIME_DIR.mkdir(exist_ok=True)

MAX_CANDLE_ROWS = 300  # سقف عمومی تعداد کندل قابل‌قبول در هر آزمایش


# ---------------------------------------------------------------------------
# ابزارهای کمکی ساخت داده‌های آموزشی (مصنوعی، معین/deterministic، بدون تصادف)
# ---------------------------------------------------------------------------

def _mk_candles(rows: list[tuple[float, float, float, float]], start_minute: int = 0) -> list[Candle]:
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        minute = start_minute + i * 5
        hh, mm = divmod(minute, 60)
        day, hh = divmod(hh, 24)
        time = f"2026-01-0{1 + day}T{hh:02d}:{mm:02d}:00"
        out.append(Candle(time, round(o, 4), round(h, 4), round(l, 4), round(c, 4)))
    return out


def _trend_scenario_bull_choch_9bar() -> list[Candle]:
    # دنبالهٔ دقیق و واقعیِ pine_ob_bot/tests/test_trend_engine_integration.py
    # (BARS) با trend_swing_length=2 و atr_period=5 تأیید شده: کندل‌های ۰ تا ۶
    # خنثی می‌مانند، کندل ۷ یک BOS صعودی و کندل ۸ یک CHoCH نزولی می‌سازد.
    rows = [
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 100.2, 95.0, 95.5),
        (95.5, 96.0, 95.2, 95.8),
        (95.8, 96.2, 95.3, 96.0),
        (96.0, 105.0, 95.9, 104.0),
        (104.0, 104.5, 103.5, 104.2),
        (104.2, 104.6, 103.8, 104.4),
        (104.4, 106.0, 104.0, 105.5),
        (105.5, 105.8, 94.0, 94.5),
    ]
    return _mk_candles(rows)


def _zigzag_candles(n: int, drift: float, wave_len: int = 6, amplitude: float = 0.5) -> list[Candle]:
    rows = []
    price = 100.0
    for i in range(n):
        phase = i % (2 * wave_len)
        local_dir = 1.0 if phase < wave_len else -1.0
        step = drift + amplitude * local_dir
        o = price
        c = o + step
        h = max(o, c) + 0.15
        l = min(o, c) - 0.15
        rows.append((o, h, l, c))
        price = c
    return _mk_candles(rows)


def _entry_pivot_scenario(kind: str) -> list[Candle]:
    n = 35
    rows = []
    for i in range(n):
        base_low = 100.0 + i * 0.05
        if kind == "pullback_35bar" and i == 17:
            base_low -= 3.0
        low = base_low
        high = low + 1.0
        open_ = low + 0.5
        close = low + 0.6
        rows.append((open_, high, low, close))
    return _mk_candles(rows)


# ---------------------------------------------------------------------------
# آزمایش ۱ — ساخت Candle
# ---------------------------------------------------------------------------

def h_candle_builder(inputs: dict) -> dict:
    steps = []
    candle = Candle(time=str(inputs["time"]), open=inputs["open"], high=inputs["high"],
                    low=inputs["low"], close=inputs["close"])
    steps.append(f"Candle ساخته شد: open={candle.open}, high={candle.high}, "
                f"low={candle.low}, close={candle.close}")
    range_size = candle.high - candle.low
    steps.append(f"اندازهٔ range = high - low = {range_size:.4f}")
    bullish = candle.close >= candle.open
    steps.append("جهت: " + ("صعودی (bullish)" if bullish else "نزولی (bearish)"))
    ohlc_valid = (candle.high >= candle.low and candle.low <= candle.open <= candle.high
                  and candle.low <= candle.close <= candle.high)
    warnings = []
    if not ohlc_valid:
        warnings.append("open/close بیرون از بازهٔ [low, high] هستند یا high<low. "
                        "خودِ کلاس Candle در models.py این را بررسی نمی‌کند — "
                        "این بررسی فقط برای همین آزمایش اضافه شده (فصل ۵، بخش «نکته»).")
    steps.append("اعتبار منطقی OHLC بررسی شد (خارج از خودِ کلاس Candle).")
    return {
        "result": {
            "summary": f"یک Candle {'صعودی' if bullish else 'نزولی'} با range برابر {range_size:.4f} ساخته شد.",
            "candle": asdict(candle),
            "range": round(range_size, 4),
            "bullish": bullish,
            "ohlc_valid": ohlc_valid,
        },
        "steps": steps,
        "metrics": {"range": round(range_size, 4), "bullish": bullish, "ohlc_valid": ohlc_valid},
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# آزمایش ۲ — تشخیص Swing/Trend
# ---------------------------------------------------------------------------

def h_trend_detection_basic(inputs: dict) -> dict:
    scenario = inputs["scenario"]
    trend_swing_length = int(inputs["trend_swing_length"])
    if scenario == "bull_choch_9bar":
        candles = _trend_scenario_bull_choch_9bar()
        atr_period = 5
        note = ("این سناریو دقیقاً همان دنبالهٔ ۹کندلیِ تأییدشده در "
               "pine_ob_bot/tests/test_trend_engine_integration.py است (اجرا با "
               "trend_swing_length=2، atr_period=5). با مقادیر دیگر برای «طول Swing»، "
               "نتیجه ممکن است متفاوت یا خالی باشد چون این دنباله فقط ۹ کندل دارد.")
    elif scenario == "simple_uptrend":
        candles = _zigzag_candles(60, drift=0.35)
        atr_period = 5
        note = "دنبالهٔ مصنوعی ۶۰کندلی با درصد صعودی خالص (زیگزاگ + drift مثبت)."
    else:
        candles = _zigzag_candles(60, drift=-0.35)
        atr_period = 5
        note = "دنبالهٔ مصنوعی ۶۰کندلی با درصد نزولی خالص (زیگزاگ + drift منفی)."

    cfg = BotConfig(trend_swing_length=trend_swing_length, atr_period=atr_period)
    engine = PineSwingOBEngine(cfg)

    steps = [note]
    swing_highs, swing_lows, breaks_out = [], [], []
    prev_sh, prev_sl = None, None
    for i, candle in enumerate(candles):
        breaks, formed, invalidated = engine.process(candle)
        if engine.swing_high is not None and engine.swing_high is not prev_sh:
            prev_sh = engine.swing_high
            swing_highs.append({"level": engine.swing_high.level, "bar_index": engine.swing_high.bar_index})
            steps.append(f"کندل {i}: Swing High جدید در سطح {engine.swing_high.level:.3f} "
                        f"(کندل شمارهٔ {engine.swing_high.bar_index}) تأیید شد.")
        if engine.swing_low is not None and engine.swing_low is not prev_sl:
            prev_sl = engine.swing_low
            swing_lows.append({"level": engine.swing_low.level, "bar_index": engine.swing_low.bar_index})
            steps.append(f"کندل {i}: Swing Low جدید در سطح {engine.swing_low.level:.3f} "
                        f"(کندل شمارهٔ {engine.swing_low.bar_index}) تأیید شد.")
        for br in breaks:
            breaks_out.append({"kind": br.kind, "direction": br.direction, "bar_index": br.bar_index,
                               "pivot_level": br.pivot_level})
            trend_label = "صعودی" if br.direction == "bull" else "نزولی"
            steps.append(f"کندل {i}: شکست {br.kind} {trend_label} — Trend به "
                        f"{'صعودی' if engine.trend == 1 else 'نزولی' if engine.trend == -1 else 'خنثی'} تغییر کرد.")

    if not swing_highs and not swing_lows:
        steps.append(f"با طول Swing = {trend_swing_length} و فقط {len(candles)} کندل، هیچ "
                    "Swingی هنوز تأیید نشد (i باید حداقل برابر طول Swing باشد).")

    trend_word = {1: "صعودی", -1: "نزولی", 0: "خنثی"}[engine.trend]
    warnings = []
    if trend_swing_length >= len(candles):
        warnings.append("طول Swing انتخاب‌شده بزرگ‌تر یا مساوی تعداد کندل‌های سناریوست.")

    return {
        "result": {
            "summary": f"روی {len(candles)} کندل: {len(swing_highs)} Swing High، "
                      f"{len(swing_lows)} Swing Low، {len(breaks_out)} شکست. Trend نهایی: {trend_word}.",
            "swing_highs": swing_highs, "swing_lows": swing_lows, "breaks": breaks_out,
            "final_trend": trend_word, "lag_bars": trend_swing_length,
        },
        "steps": steps,
        "metrics": {"swing_highs": len(swing_highs), "swing_lows": len(swing_lows),
                   "bos_count": sum(1 for b in breaks_out if b["kind"] == "BOS"),
                   "choch_count": sum(1 for b in breaks_out if b["kind"] == "CHoCH"),
                   "final_trend": trend_word},
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# آزمایش ۳ — Entry Pivot
# ---------------------------------------------------------------------------

def h_entry_pivot_detection(inputs: dict) -> dict:
    scenario = inputs["scenario"]
    left = int(inputs["pivot_left"])
    right = int(inputs["pivot_right"])
    max_age = int(inputs["max_entry_pivot_age_bars"])

    candles = _entry_pivot_scenario("pullback_35bar" if scenario == "pullback_10bar" else "choppy_range")
    detector = ClassicEntryPivotDetector(left, right)

    steps = []
    pivots = []
    for i, candle in enumerate(candles):
        for pivot in detector.process(candle):
            confirmation_index = pivot.candle_index + pivot.right_bars
            pivots.append({
                "kind": pivot.kind, "price": pivot.price, "candle_index": pivot.candle_index,
                "pivot_time": pivot.pivot_time, "confirmed_at": pivot.confirmed_at,
                "confirmation_index": confirmation_index,
            })
            steps.append(f"کندل {pivot.candle_index}: {'Pivot High' if pivot.kind == 'pivot_high' else 'Pivot Low'} "
                        f"در سطح {pivot.price:.3f} کاندید شد؛ در کندل {confirmation_index} "
                        f"(پس از {right} کندل) تأیید شد.")

    last_index = len(candles) - 1
    freshness = []
    for p in pivots:
        age = last_index - p["confirmation_index"]
        fresh = max_age is None or age <= max_age
        freshness.append({**p, "age_bars_from_last_candle": age, "fresh": fresh})
    if not pivots:
        steps.append("هیچ Pivotی با این پارامترها و این دیتاست شناسایی نشد.")
    else:
        latest = freshness[-1]
        steps.append(f"سن آخرین Pivot نسبت به آخرین کندل موجود ({last_index}): "
                    f"{latest['age_bars_from_last_candle']} کندل — "
                    f"{'Fresh (پذیرفته می‌شود)' if latest['fresh'] else 'Stale (رد می‌شود)'}"
                    f" (طبق فرمول add_ob، سقف مجاز = {max_age}).")

    return {
        "result": {
            "summary": f"روی {len(candles)} کندل، {len(pivots)} Entry Pivot شناسایی شد "
                      f"(left={left}, right={right}).",
            "pivots": freshness,
        },
        "steps": steps,
        "metrics": {"pivots_found": len(pivots),
                   "fresh_count": sum(1 for x in freshness if x["fresh"]),
                   "stale_count": sum(1 for x in freshness if not x["fresh"])},
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# آزمایش ۴ — طبقه‌بندی BOS/CHoCH و فیلتر strong_choch_only
# ---------------------------------------------------------------------------

def h_bos_choch_classifier(inputs: dict) -> dict:
    current_trend = inputs["current_trend"]     # قبل از شکست
    break_direction = inputs["break_direction"]  # bullish/bearish
    percentile = float(inputs["displacement_percentile"])
    strong_only = bool(inputs["strong_choch_only"])

    steps = []
    # همان طبقه‌بندی یک‌خطیِ pine_engine.py::process (بدون بازنویسی منطق: این
    # دقیقاً همان قاعدهٔ کد است، فقط این‌جا مستقیماً روی ورودی کاربر اعمال شده).
    if break_direction == "bullish":
        kind = "CHoCH" if current_trend == "bearish" else "BOS"
    else:
        kind = "CHoCH" if current_trend == "bullish" else "BOS"
    steps.append(f"طبقه‌بندی: چون جهتِ شکست «{break_direction}» و Trend قبل از شکست «{current_trend}» "
                f"بود → {kind} (قاعدهٔ pine_engine.py: مخالفِ Trend = CHoCH، هم‌جهت یا خنثی = BOS).")

    top_quartile = percentile >= 0.75
    steps.append(f"رتبهٔ درصدی displacement = {percentile:.2f} → "
                f"{'چارک بالا (top quartile)' if top_quartile else 'زیر چارک بالا'}.")

    new_trend = break_direction  # طبق فصل ۱۴: Trend بلافاصله پس از شکست به جهت جدید می‌رود
    steps.append(f"طبق ترتیب واقعی کد (فصل ۱۴)، Trend بلافاصله پس از شکست به‌روزرسانی می‌شود؛ "
                f"بنابراین دروازهٔ Trend در add_ob نسبت به جهت جدید ({new_trend}) بررسی می‌شود.")

    ob = OrderBlock(id="lab-ob", direction=("bull" if break_direction == "bullish" else "bear"),
                    high=1960.0, low=1950.0, source_index=0, source_time="2026-01-01T00:00:00",
                    formed_index=1, formed_time="2026-01-01T00:05:00", active=True,
                    break_kind=kind, atr_at_formation=2.0)
    cfg = BotConfig(strong_choch_only=strong_only, allowed_break_kinds=("BOS", "CHoCH"),
                    risk_fraction=0.01)
    broker = PaperBroker(cfg, 10_000.0, SymbolSpec())
    broker.current_m5_trend = TrendDirection(new_trend if new_trend in ("bullish", "bearish") else "neutral")
    steps.append("توجه: allowed_break_kinds در این آزمایش عمداً روی (\"BOS\",\"CHoCH\") تنظیم شده تا "
                "فیلتر strong_choch_only ایزوله بررسی شود؛ پیش‌فرض واقعی پروژه فقط (\"BOS\",) است "
                "مگر با --include-choch (فصل ۱۶).")

    order = broker.add_ob(ob, {"bos_displacement_top_quartile": top_quartile})
    accepted = order is not None
    if accepted:
        steps.append(f"add_ob پذیرفت → PendingOrder با lifecycle_state=\"{order.lifecycle_state}\" ساخته شد.")
        reason = None
    else:
        if strong_only and kind == "CHoCH" and not top_quartile:
            reason = "rejected_weak_choch"
        else:
            reason = "rejected_trend_mismatch_or_neutral (یا break_kind فیلترشده)"
        steps.append(f"add_ob رد کرد → دلیل محتمل: {reason}.")

    return {
        "result": {
            "summary": f"شکست {kind} با displacement={percentile:.2f} → "
                      f"{'پذیرفته شد' if accepted else 'رد شد'}.",
            "break_kind": kind, "top_quartile": top_quartile, "accepted": accepted,
            "reason": reason, "stats_snapshot": {k: v for k, v in broker.stats.items() if v},
        },
        "steps": steps,
        "metrics": {"break_kind": kind, "accepted": accepted, "top_quartile": top_quartile},
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# آزمایش ۵ — Sweep and Reclaim
# ---------------------------------------------------------------------------

def h_sweep_and_reclaim(inputs: dict) -> dict:
    direction = inputs["direction"]
    entry = float(inputs["entry"])
    atr = float(inputs["atr"])
    atr_buffer = float(inputs["atr_buffer"])
    sweep_low_in = float(inputs["sweep_low"])
    sweep_close = float(inputs["sweep_close"])
    max_bars = int(inputs["max_bars"])

    cfg = BotConfig(entry_mode="sweep_reclaim", sweep_reclaim_atr_buffer=atr_buffer,
                    sweep_reclaim_max_bars=max_bars, risk_fraction=0.01)
    broker = PaperBroker(cfg, 10_000.0, SymbolSpec())
    stop = entry - 3 * atr if direction == "bull" else entry + 3 * atr
    order = PendingOrder(id="lab-order", ob_id="lab-ob", direction=direction, entry=entry,
                        stop=stop, target=entry, created_index=0, created_time="2026-01-01T00:00:00",
                        meta={"atr_at_formation": atr}, lifecycle_state="armed")

    candle_high = max(sweep_low_in, sweep_close, entry) + 0.5
    candle_low = min(sweep_low_in, sweep_close, entry) - 0.5
    candle = Candle(time="2026-01-01T00:05:00", open=entry, high=candle_high,
                    low=candle_low, close=sweep_close)

    swept_now = (candle.low < entry) if direction == "bull" else (candle.high > entry)
    reclaimed_now = (candle.close > entry) if direction == "bull" else (candle.close < entry)

    steps = [
        f"کندل ساخته‌شده: open={candle.open:.3f}, high={candle.high:.3f}, "
        f"low={candle.low:.3f}, close={candle.close:.3f}.",
        f"شرط Sweep ({'low < entry' if direction == 'bull' else 'high > entry'}): "
        f"{'برقرار' if swept_now else 'برقرار نیست'}.",
        f"شرط Reclaim ({'close > entry' if direction == 'bull' else 'close < entry'}): "
        f"{'برقرار' if reclaimed_now else 'برقرار نیست'}.",
    ]

    reclaimed, lag = broker._sweep_reclaim_window(order, candle, bar_index=1)
    steps.append(f"PaperBroker._sweep_reclaim_window نتیجه داد: "
                f"{'تأیید شد' if reclaimed else 'تأیید نشد'} (lag={lag}).")

    final_stop = None
    fill_price = None
    if reclaimed:
        buffer = atr * atr_buffer
        final_stop = stop - buffer if direction == "bull" else stop + buffer
        fill_price = candle.close  # نسخهٔ ساده‌شده: بدون اسپرد (spread=0 فرض شده)
        steps.append(f"Stop نهایی با بافر ATR: {final_stop:.3f} (buffer={buffer:.3f}).")
        steps.append(f"قیمت پر شدن (نسخهٔ ساده‌شدهٔ آموزشی، spread=0): {fill_price:.3f}. "
                    "در کد واقعی (process_candle→_try_sweep_reclaim) برای خرید close+spread و برای فروش close است.")
    else:
        steps.append("چون تأیید نشد، سفارش پر نمی‌شود (armed می‌ماند مگر پنجره منقضی/سطح Stop رد شود).")

    return {
        "result": {
            "summary": f"{'Sweep و Reclaim تأیید شد' if reclaimed else 'تأیید نشد'} — lag={lag} کندل.",
            "swept": swept_now, "reclaimed": reclaimed, "lag_bars": lag,
            "final_stop": final_stop, "fill_price": fill_price,
        },
        "steps": steps,
        "metrics": {"swept": swept_now, "reclaimed": reclaimed, "lag_bars": lag},
        "warnings": [] if max_bars == 1 else [
            "max_bars > 1 یک قابلیت آزمایشی است؛ این آزمایش فقط یک کندل واحد را بررسی می‌کند "
            "(برای دیدن پنجرهٔ چندکندلی واقعی، بک‌تست فصل ۱۵ را ببینید)."],
    }


# ---------------------------------------------------------------------------
# آزمایش ۶ — Risk Sizing
# ---------------------------------------------------------------------------

def h_risk_sizing_calculator(inputs: dict) -> dict:
    result = calculate_safe_position_size(
        equity=float(inputs["equity"]), risk_percent=float(inputs["risk_percent"]),
        entry_price=float(inputs["entry_price"]), stop_loss=float(inputs["stop_loss"]),
        tick_size=float(inputs["tick_size"]), tick_value=float(inputs["tick_value"]),
        volume_min=float(inputs["volume_min"]), volume_max=float(inputs["volume_max"]),
        volume_step=float(inputs["volume_step"]),
    )
    steps = [
        f"allowed_risk = equity × risk_percent = {inputs['equity']} × {inputs['risk_percent']} = "
        f"{result.allowed_risk:.4f}",
        f"distance = |entry - stop| = {abs(inputs['entry_price'] - inputs['stop_loss']):.4f}",
        f"حجم نهایی پس از cap و floor روی volume_step: {result.volume:.6f}",
        f"ریسک واقعی محاسبه‌شده: {result.actual_risk:.4f}",
        f"نتیجه: {'معتبر (is_valid=True)' if result.is_valid else f'رد شد — {result.rejection_reason}'}",
    ]
    return {
        "result": {
            "summary": (f"حجم مجاز: {result.volume} — ریسک واقعی: {result.actual_risk:.2f}"
                       if result.is_valid else f"رد شد: {result.rejection_reason}"),
            **asdict(result),
        },
        "steps": steps,
        "metrics": {"volume": result.volume, "actual_risk": round(result.actual_risk, 4),
                   "is_valid": result.is_valid},
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# آزمایش ۷ — Take Profit
# ---------------------------------------------------------------------------

def h_take_profit_calculator(inputs: dict) -> dict:
    side = inputs["side"]
    entry = float(inputs["entry"])
    stop = float(inputs["stop"])
    rr = float(inputs["rr"])
    risk_distance = abs(entry - stop)
    is_buy = side == "buy"
    target = entry + rr * risk_distance if is_buy else entry - rr * risk_distance
    reward_distance = abs(target - entry)
    steps = [
        f"فاصلهٔ ریسک = |entry - stop| = {risk_distance:.4f}",
        f"فرمول (از PaperBroker.add_ob): target = entry {'+' if is_buy else '-'} rr × risk "
        f"= {entry} {'+' if is_buy else '-'} {rr} × {risk_distance:.4f} = {target:.4f}",
        f"فاصلهٔ پاداش = |target - entry| = {reward_distance:.4f}",
        f"نسبت واقعی پاداش به ریسک = {reward_distance / risk_distance if risk_distance else 0:.3f}",
    ]
    return {
        "result": {
            "summary": f"Target = {target:.4f} (فاصلهٔ ریسک {risk_distance:.4f} × RR {rr})",
            "risk_distance": round(risk_distance, 4), "target": round(target, 4),
            "reward_distance": round(reward_distance, 4),
        },
        "steps": steps,
        "metrics": {"target": round(target, 4), "rr_actual": round(reward_distance / risk_distance, 3) if risk_distance else 0},
        "warnings": [] if risk_distance > 0 else ["فاصلهٔ ریسک صفر است؛ entry و stop برابرند."],
    }


# ---------------------------------------------------------------------------
# آزمایش ۸ — Breakeven
# ---------------------------------------------------------------------------

def _simulate_breakeven(entry, stop, target, trigger_r, scenario, direction="bull"):
    cfg = BotConfig(breakeven_trigger_r=trigger_r, risk_fraction=0.01)
    broker = PaperBroker(cfg, 10_000.0, SymbolSpec())
    risk_distance = abs(entry - stop)
    pos = PaperPosition(id="lab-pos", order_id="lab-order", direction=direction, entry=entry,
                       stop=stop, target=target, volume=1.0, risk_money=100.0,
                       opened_time="2026-01-01T00:00:00", meta={"original_stop": stop})
    broker.positions.append(pos)  # _close() removes from this list, so it must be registered first

    if scenario == "straight_to_target":
        prices = [entry + (target - entry) * f for f in (0.25, 0.5, 0.75, 1.0)]
    elif scenario == "straight_to_stop":
        prices = [entry + (stop - entry) * f for f in (0.25, 0.5, 0.75, 1.0)]
    else:  # run_then_reverse
        up = [entry + (target - entry) * f for f in (0.4, 0.7, 1.0)]
        down = [entry + (target - entry) * f for f in (0.6, 0.3, 0.0)] + [stop]
        prices = up + down

    log = []
    final_result = None
    for price in prices:
        broker._track_excursion(pos, price, price)
        armed_before = bool(pos.meta.get("breakeven_armed"))
        broker._apply_breakeven(pos, "tick")
        if not armed_before and pos.meta.get("breakeven_armed"):
            log.append(f"MFE به آستانه رسید → Breakeven arm شد؛ Stop به {pos.stop:.4f} منتقل شد.")
        hit_stop = price <= pos.stop if direction == "bull" else price >= pos.stop
        hit_target = price >= pos.target if direction == "bull" else price <= pos.target
        if hit_stop:
            trade = broker._close(pos, pos.stop, "tick", "loss")
            final_result = trade
            log.append(f"قیمت {price:.4f} به Stop ({pos.stop:.4f}) برخورد کرد → معامله بسته شد.")
            break
        if hit_target:
            trade = broker._close(pos, pos.target, "tick", "win")
            final_result = trade
            log.append(f"قیمت {price:.4f} به Target ({pos.target:.4f}) برخورد کرد → معامله بسته شد.")
            break
    if final_result is None:
        # قیمت به هیچ‌کدام نرسید؛ برای بستن دستی در پایان شبیه‌سازی می‌بندیم.
        final_result = broker._close(pos, prices[-1], "tick",
                                     "win" if prices[-1] >= entry else "loss")
        log.append(f"دنبالهٔ قیمت تمام شد؛ برای نمایش نتیجه با آخرین قیمت ({prices[-1]:.4f}) بسته شد.")
    return log, final_result, risk_distance


def h_breakeven_simulator(inputs: dict) -> dict:
    scenario = inputs["scenario"]
    entry = float(inputs["entry"])
    stop = float(inputs["stop"])
    target = float(inputs["target"])
    trigger_r = float(inputs["breakeven_trigger_r"])
    enabled = bool(inputs["breakeven_enabled"])

    log_on, trade_on, risk_distance = _simulate_breakeven(entry, stop, target,
                                                          trigger_r if enabled else None, scenario)
    log_off, trade_off, _ = _simulate_breakeven(entry, stop, target, None, scenario)

    steps = ["--- با Breakeven " + ("فعال" if enabled else "(اجباراً بررسی هم بدون آن)") + " ---"] + log_on
    steps += ["--- برای مقایسه، با Breakeven خاموش ---"] + log_off

    return {
        "result": {
            "summary": f"نتیجهٔ اصلی: {trade_on.result} (R={trade_on.r_multiple:.2f}) — "
                      f"در برابر بدون Breakeven: {trade_off.result} (R={trade_off.r_multiple:.2f})",
            "with_breakeven": {"result": trade_on.result, "r_multiple": round(trade_on.r_multiple, 3),
                              "final_stop": trade_on.meta.get("final_stop"),
                              "breakeven_armed": trade_on.meta.get("breakeven_armed"),
                              "breakeven_exit": trade_on.meta.get("breakeven_exit")},
            "without_breakeven": {"result": trade_off.result, "r_multiple": round(trade_off.r_multiple, 3),
                                 "final_stop": trade_off.meta.get("final_stop")},
        },
        "steps": steps,
        "metrics": {"r_with_breakeven": round(trade_on.r_multiple, 3),
                   "r_without_breakeven": round(trade_off.r_multiple, 3)},
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# آزمایش ۹ — چرخهٔ عمر Pending Order
# ---------------------------------------------------------------------------

def h_pending_order_lifecycle(inputs: dict) -> dict:
    scenario = inputs["scenario"]
    timeline = []
    steps = []

    def record(label, order, reason=None):
        timeline.append({"event": label, "lifecycle_state": order.lifecycle_state,
                         "active": order.active, "reason": reason})
        steps.append(f"{label} → lifecycle_state=\"{order.lifecycle_state}\"" +
                    (f" (دلیل: {reason})" if reason else ""))

    entry_mode = "sweep_reclaim" if scenario in ("sweep_no_reclaim", "sweep_and_reclaim") else "limit"
    cfg = BotConfig(entry_mode=entry_mode, sweep_reclaim_atr_buffer=0.2, risk_fraction=0.01,
                    allowed_break_kinds=("BOS", "CHoCH"))
    broker = PaperBroker(cfg, 10_000.0, SymbolSpec())
    if inputs.get("entry_mode") and inputs["entry_mode"] != entry_mode:
        steps.append(f"توجه: این سناریو همیشه با entry_mode=\"{entry_mode}\" اجرا می‌شود "
                    f"(ورودی شما \"{inputs['entry_mode']}\" برای این سناریوی خاص نادیده گرفته شد).")

    ob = OrderBlock(id="lab-ob", direction="bull", high=1955.0, low=1950.0, source_index=0,
                    source_time="2026-01-01T00:00:00", formed_index=1,
                    formed_time="2026-01-01T00:05:00", active=True, break_kind="BOS",
                    atr_at_formation=2.0)

    broker.update_m5_trend(TrendDirection.BULLISH, "2026-01-01T00:05:00")
    steps.append("update_m5_trend(BULLISH) اجرا شد.")
    order = broker.add_ob(ob)
    if order is None:
        return {"result": {"summary": "add_ob سفارش را نپذیرفت (غیرمنتظره برای این سناریو)."},
               "steps": steps, "metrics": {}, "warnings": ["سناریو نتوانست سفارش اولیه بسازد."]}
    record("add_ob (ساخت سفارش)", order)

    if scenario == "stable_trend_fill":
        tick = Tick(time="2026-01-01T00:10:00", bid=1954.8, ask=1955.0)
        broker.process_tick(tick)
        record("process_tick (قیمت به Entry رسید)", order)

    elif scenario == "trend_flip":
        broker.update_m5_trend(TrendDirection.BEARISH, "2026-01-01T00:10:00")
        record("update_m5_trend (Trend به BEARISH تغییر کرد)", order,
              order.meta.get("cancellation_reason"))

    elif scenario == "sweep_no_reclaim":
        candle = Candle("2026-01-01T00:10:00", 1954.0, 1954.2, 1948.0, 1953.0)  # sweep بدون reclaim
        broker.process_signal_candle(candle, 2)
        record("process_signal_candle (Sweep بدون Reclaim)", order,
              "sweep_reclaim_confirmed=False — سفارش armed می‌ماند اما هرگز خودکار پر نمی‌شود")

    elif scenario == "sweep_and_reclaim":
        candle = Candle("2026-01-01T00:10:00", 1954.0, 1954.2, 1948.0, 1956.0)  # sweep + reclaim
        broker.process_signal_candle(candle, 2)
        record("process_signal_candle (Sweep و Reclaim تأیید شد)", order)
        tick = Tick(time="2026-01-01T00:15:00", bid=1956.3, ask=1956.5)
        broker.process_tick(tick)
        record("process_tick (پر شدن با قیمت زندهٔ پس از تأیید)", order)

    elif scenario == "ob_invalidated":
        broker.cancel_ob(ob.id)
        record("cancel_ob (Order Block باطل شد)", order)

    final_position = broker.positions[0] if broker.positions else None
    return {
        "result": {
            "summary": f"سناریوی «{scenario}» به حالت نهایی lifecycle_state=\"{order.lifecycle_state}\" رسید.",
            "timeline": timeline,
            "final_position": asdict(final_position) if final_position else None,
        },
        "steps": steps,
        "metrics": {"final_state": order.lifecycle_state, "active": order.active,
                   "filled": final_position is not None},
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# آزمایش ۱۰ — بک‌تست کوچک آموزشی
# ---------------------------------------------------------------------------

def h_mini_backtest(inputs: dict) -> dict:
    csv_path = DATA_SAMPLES_DIR / "mini_backtest_sample.csv"
    if not csv_path.exists():
        return {"result": {"summary": "دیتاست آموزشی پیدا نشد."}, "steps": [],
               "metrics": {}, "warnings": ["mini_backtest_sample.csv در data_samples/ یافت نشد."]}

    tmp_dir = RUNTIME_DIR / f"lab_backtest_{uuid.uuid4().hex[:12]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        cfg = BotConfig(
            trend_swing_length=int(inputs["trend_swing_length"]),
            entry_pivot_left=int(inputs["pivot_left"]),
            entry_pivot_right=int(inputs["pivot_right"]),
            rr=float(inputs["rr"]),
            sweep_reclaim_atr_buffer=float(inputs["sweep_atr_buffer"]),
            strong_choch_only=bool(inputs["strong_choch_only"]),
            allowed_break_kinds=("BOS", "CHoCH"),
            max_entry_pivot_age_bars=int(inputs["max_entry_pivot_age"]) or None,
            breakeven_trigger_r=(float(inputs["breakeven_trigger_r"])
                                 if float(inputs["breakeven_trigger_r"]) > 0 else None),
            db_path=tmp_dir / "lab.sqlite3",
        )
        cfg.validate()
        summary = run_historical(csv_path, cfg, initial_equity=10_000.0, spread=0.20, run_label="lab")

        trades_csv = tmp_dir / "historical_mini_backtest_sample_lab_trades.csv"
        equity_curve = [10_000.0]
        trades_preview = []
        if trades_csv.exists():
            with trades_csv.open(encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                equity = 10_000.0
                for row in reader:
                    try:
                        pnl = float(row.get("pnl", 0) or 0)
                    except ValueError:
                        pnl = 0.0
                    equity += pnl
                    equity_curve.append(round(equity, 2))
                    if len(trades_preview) < 50:
                        trades_preview.append({
                            "direction": row.get("direction"), "result": row.get("result"),
                            "r_multiple": row.get("r_multiple"), "pnl": row.get("pnl"),
                            "opened_time": row.get("opened_time"), "closed_time": row.get("closed_time"),
                        })

        steps = [
            f"run_historical روی {summary.get('bars')} کندل مصنوعی اجرا شد.",
            f"تعداد setup مشاهده‌شده: {summary.get('setups_seen', 0)}",
            f"تعداد سفارش ساخته‌شده: {summary.get('pending_orders_created', 0)}",
            f"تعداد معاملات بسته‌شده: {summary.get('trades', 0)}",
            f"Win rate: {summary.get('win_rate', 0)} — Profit Factor: {summary.get('profit_factor', 0)}",
        ]
        warnings = []
        if summary.get("trades", 0) == 0:
            warnings.append("با این پارامترها هیچ معامله‌ای بسته نشد — دیتاست آموزشی کوچک است؛ "
                            "پارامترها (مثلاً طول Swing یا سن مجاز Pivot) را تغییر دهید.")

        return {
            "result": {
                "summary": f"{summary.get('trades', 0)} معامله — Win Rate "
                          f"{summary.get('win_rate', 0)} — Total R {summary.get('total_r', 0)}",
                "trades": summary.get("trades", 0), "wins": summary.get("wins", 0),
                "win_rate": summary.get("win_rate", 0), "total_r": summary.get("total_r", 0),
                "profit_factor": summary.get("profit_factor", 0),
                "max_drawdown_pct": summary.get("max_drawdown_pct", 0),
                "equity_curve": equity_curve, "trades_preview": trades_preview,
            },
            "steps": steps,
            "metrics": {"trades": summary.get("trades", 0), "win_rate": summary.get("win_rate", 0),
                       "total_r": summary.get("total_r", 0), "profit_factor": summary.get("profit_factor", 0),
                       "max_drawdown_pct": summary.get("max_drawdown_pct", 0)},
            "warnings": warnings,
        }
    except ValueError as exc:
        return {"result": {"summary": f"پیکربندی نامعتبر: {exc}"}, "steps": [], "metrics": {},
               "warnings": [str(exc)]}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# رجیستری مرکزی
# ---------------------------------------------------------------------------

def _f(name, type_, default, **kw):
    d = {"name": name, "type": type_, "default": default}
    d.update(kw)
    return d


EXPERIMENTS = {
    "candle_builder": {
        "handler": h_candle_builder,
        "fields": [
            _f("time", "str", "2026-01-01T00:00:00"),
            _f("open", "float", 1950.00, min=0, max=100000),
            _f("high", "float", 1955.00, min=0, max=100000),
            _f("low", "float", 1948.00, min=0, max=100000),
            _f("close", "float", 1952.50, min=0, max=100000),
        ],
    },
    "trend_detection_basic": {
        "handler": h_trend_detection_basic,
        "fields": [
            _f("scenario", "select", "bull_choch_9bar",
              options=["bull_choch_9bar", "simple_uptrend", "simple_downtrend"]),
            _f("trend_swing_length", "int", 2, min=2, max=30),
        ],
    },
    "entry_pivot_detection": {
        "handler": h_entry_pivot_detection,
        "fields": [
            _f("scenario", "select", "pullback_10bar", options=["pullback_10bar", "choppy_range"]),
            _f("pivot_left", "int", 5, min=1, max=15),
            _f("pivot_right", "int", 5, min=1, max=15),
            _f("max_entry_pivot_age_bars", "int", 6, min=1, max=50),
        ],
    },
    "bos_choch_classifier": {
        "handler": h_bos_choch_classifier,
        "fields": [
            _f("current_trend", "select", "bullish", options=["bullish", "bearish", "neutral"]),
            _f("break_direction", "select", "bearish", options=["bullish", "bearish"]),
            _f("displacement_percentile", "float", 0.5, min=0, max=1),
            _f("strong_choch_only", "bool", True),
        ],
    },
    "sweep_and_reclaim": {
        "handler": h_sweep_and_reclaim,
        "fields": [
            _f("direction", "select", "bull", options=["bull", "bear"]),
            _f("entry", "float", 1950.00, min=0, max=100000),
            _f("atr", "float", 2.0, min=0.01, max=100),
            _f("atr_buffer", "float", 0.20, min=0, max=2),
            _f("sweep_low", "float", 1948.50, min=0, max=100000),
            _f("sweep_close", "float", 1951.00, min=0, max=100000),
            _f("max_bars", "int", 1, min=1, max=5),
        ],
    },
    "risk_sizing_calculator": {
        "handler": h_risk_sizing_calculator,
        "fields": [
            _f("equity", "float", 10000, min=1, max=10000000),
            _f("risk_percent", "float", 0.01, min=0.0001, max=1),
            _f("entry_price", "float", 2000, min=0, max=1000000),
            _f("stop_loss", "float", 1990, min=0, max=1000000),
            _f("tick_size", "float", 0.01, min=0.00001, max=100),
            _f("tick_value", "float", 1.0, min=0.00001, max=100000),
            _f("volume_min", "float", 0.01, min=0.00001, max=1000),
            _f("volume_max", "float", 100, min=0.01, max=100000),
            _f("volume_step", "float", 0.01, min=0.00001, max=100),
        ],
    },
    "take_profit_calculator": {
        "handler": h_take_profit_calculator,
        "fields": [
            _f("side", "select", "buy", options=["buy", "sell"]),
            _f("entry", "float", 2000, min=0, max=1000000),
            _f("stop", "float", 1990, min=0, max=1000000),
            _f("rr", "float", 1.5, min=0.1, max=10),
        ],
    },
    "breakeven_simulator": {
        "handler": h_breakeven_simulator,
        "fields": [
            _f("scenario", "select", "run_then_reverse",
              options=["run_then_reverse", "straight_to_target", "straight_to_stop"]),
            _f("entry", "float", 2000, min=0, max=1000000),
            _f("stop", "float", 1990, min=0, max=1000000),
            _f("target", "float", 2015, min=0, max=1000000),
            _f("breakeven_trigger_r", "float", 1.0, min=0.1, max=5),
            _f("breakeven_enabled", "bool", True),
        ],
    },
    "pending_order_lifecycle": {
        "handler": h_pending_order_lifecycle,
        "fields": [
            _f("scenario", "select", "trend_flip",
              options=["stable_trend_fill", "trend_flip", "sweep_no_reclaim",
                      "sweep_and_reclaim", "ob_invalidated"]),
            _f("entry_mode", "select", "limit", options=["limit", "sweep_reclaim"]),
        ],
    },
    "mini_backtest": {
        "handler": h_mini_backtest,
        "fields": [
            _f("trend_swing_length", "int", 12, min=3, max=30),
            _f("pivot_left", "int", 5, min=1, max=15),
            _f("pivot_right", "int", 5, min=1, max=15),
            _f("rr", "float", 1.5, min=0.5, max=5),
            _f("sweep_atr_buffer", "float", 0.20, min=0, max=2),
            _f("strong_choch_only", "bool", False),
            _f("max_entry_pivot_age", "int", 10, min=1, max=50),
            _f("breakeven_trigger_r", "float", 0, min=0, max=5),
        ],
    },
}
