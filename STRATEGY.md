# منطقِ استراتژی — SMC / MTF (XAUUSD)

خلاصهٔ یک‌خطی:
**BOS (جهت) → سوییپِ نقدینگی → زونِ OB → لمس و مسلح شدن → CHoCH روی M5 (تریگر) → ورودِ market، SL زیر/بالای swing، TP سرِ نقدینگیِ بعدی.**

BUY و SELL آینهٔ کاملِ هم‌اند. هر مرحله برای هر دو جهت روی هر کندلِ M5 اجرا می‌شود (تا وقتی ظرفیتِ پوزیشن هست). همهٔ ضرایب در `config/settings.py` هستند.

> نسخهٔ اعتبارسنجی‌شده (in-sample PF ~۱.۷۸ / OOS PF ~۱.۴۶):
> `entry_trigger="choch"`، و EMA / RSI / FVG-overlap / MANAGEMENT همگی **خاموش**.

---

## مرحله ۱ — بایاسِ M15 (BOS)
آخرین swingها و شکستِ ساختار روی M15 اسکن می‌شود.
- **BUY:** باید BOSِ صعودی باشد (قیمت سقفِ swingِ قبلی را بسته).
- **SELL:** BOSِ نزولی (قیمت کفِ قبلی را بسته).

**ضرایب:** `htf_structure` (عمقِ اسکن) · `htf_swing_left/right` (حساسیتِ pivot؛ کوچک‌تر = swingهای بیشتر).

## مرحله ۱ — فیلترِ extension
فاصلهٔ قیمت تا مبدأ BOS بر حسب ATR محاسبه می‌شود.
- **هر دو:** اگر قیمت بیش از حد از مبدأ دور شده باشد رد می‌شود (دنبالِ قیمتِ دررفته نرو).

**ضرایب:** `ATR.period` · `FILTERS.not_extended_atr` (بزرگ‌تر = سخت‌گیریِ کمتر، معاملهٔ بیشتر).

## مرحله ۱b — گیتِ روندِ EMA (اختیاری؛ الان خاموش)
- **BUY:** قیمت بالای EMA. **SELL:** پایینِ EMA.

**ضرایب:** `EMA.enabled` · `EMA.period`.
> تست شد: تعداد را نصف کرد ولی PF را بهتر نکرد → خاموش.

## مرحله ۲ — جاروی نقدینگی
سقف/کفِ swingهای M15 → استخرهای BSL/SSL.
- **BUY:** باید SSL (زیرِ یک کفِ اخیر) جارو شده باشد.
- **SELL:** باید BSL (بالای یک سقف) جارو شده باشد.

**ضرایب:** `htf_liquidity` (عمقِ اسکن) · `FILTERS.require_liquidity_sweep` (خاموش = این شرط برداشته می‌شود).

## مرحله ۳ — اوردربلاک و زون
- **BUY:** آخرین کندلِ **نزولی** قبل از impulse = زونِ تقاضا.
- **SELL:** آخرین کندلِ **صعودی** قبل از impulse = زونِ عرضه.

**ضرایب:** `htf_ob` (پنجرهٔ جستجوی OB؛ بزرگ‌تر = ممکن است بلوکِ کهنه بگیرد) · `htf_zone` (پهنای زون؛ بزرگ‌تر = زونِ پهن‌تر، لمسِ راحت‌تر).

## مرحله ۳b — مسلح شدنِ زون با لمس (state machine)
وقتی قیمت وارد زون (±تلورانس) شد، زون «مسلح» می‌شود و تا N کندل منتظرِ CHoCH می‌ماند — حتی اگر قیمت کمی از باند خارج شود.
- **BUY:** قیمت وارد زونِ تقاضا. **SELL:** وارد زونِ عرضه.
- اگر قیمت اصلاً به زون نرسد → رد (`zone_not_tapped`).
- اگر مسلح شد ولی CHoCH نیامد → رد (`arm_expired`).

**ضرایب:** `FILTERS.zone_tap_tolerance_atr` (بزرگ‌تر = لمسِ آسون‌تر، معاملهٔ بیشتر) · `require_fvg_overlap` + `htf_fvg` (الان خاموش) · `ltf_zone_arm` (مدتِ مسلح ماندن).

## مرحله ۴ — CHoCH روی M5 (تریگر)
داخل پنجرهٔ بعد از لمس، تغییرِ کاراکتر روی M5.
- **BUY:** CHoCHِ صعودی (بستن بالای آخرین سقفِ ریزِ M5).
- **SELL:** CHoCHِ نزولی (بستن زیرِ آخرین کفِ ریز).

**ضرایب:** `ltf_choch` (پنجرهٔ اسکن؛ بزرگ‌تر = فرصتِ بیشتر، معاملهٔ بیشتر) · `ltf_swing_left/right` (pivotِ M5).

## مرحله ۴b — تأییدِ RSI (اختیاری؛ الان خاموش)
- **BUY:** RSI اخیراً به اشباع‌فروش رفته و حالا برمی‌گردد بالا. **SELL:** برعکس.

**ضرایب:** `RSI.*`.
> تست شد: روی نمونهٔ معتبر PF را بهتر نکرد → خاموش.

## مرحله ۵ — ورود و حد ضرر
**ورود** (`entry_trigger="choch"`): سرِ بازار روی شکستِ CHoCH — **BUY** سرِ ask، **SELL** سرِ bid.
(اگر `entry_trigger="fvg"` باشد: limit سرِ میانهٔ FVG و انتظار برای retrace.)

**حد ضرر:**
- **BUY:** زیرِ کفِ swingِ M5 منهای بافر.
- **SELL:** بالای سقفِ swingِ M5 بعلاوهٔ بافر.
- با سقفِ ATR محدود؛ سپس فیلترِ کفِ استاپ.

**ضرایب:** `entry_trigger` · `spread_points` · `ltf_sl_ref` · `ATR.sl_cap_mult` · `sl_buffer_points` · `TEST.risk_mult` (استاپ را پهن‌تر می‌کند) · `min_stop_points` + `stop_spread_mult` (استاپِ خیلی ریز رد = `bad_stop`).

## مرحله ۶ — حد سود و RR
**TP** (`tp_mode="liquidity"`):
- **BUY:** نزدیک‌ترین BSLِ بالا که حداقل `min_rr×ریسک` دور باشد.
- **SELL:** نزدیک‌ترین SSLِ پایین.
- اگر نقدینگیِ به‌اندازهٔ کافی دور نبود → fallback با `fixed_rr` (یا skip).
- سپس RR = ریوارد ÷ ریسک؛ اگر < `min_rr` رد می‌شود (`low_rr`).

**ضرایب:** `tp_mode` · `tp_fallback` · `fixed_rr` · `min_rr` · `cost_buffer_points` · `TEST.risk_mult` (TP را هم‌نسبت پهن می‌کند).

## سایزِ پوزیشن
`لات = ریسکِ دلاری ÷ (فاصلهٔ استاپ × اندازهٔ قرارداد)` ، که `ریسکِ دلاری = account_balance × risk_per_trade`.

**ضرایب:** `risk_per_trade` · `account_balance` · `max_lot` · `contract_size` · `min_lot` / `lot_step`.
> در لایو: بالانسِ واقعی × `tick_value`ـِ بروکر.

## لایهٔ اجرا (موتور / لایو — یکسان)
- چند معاملهٔ هم‌زمان، سقفِ ورودِ مجدد به هر زون، فاصله بین معاملات، dedupِ سیگنالِ تکراری.

**ضرایب:** `max_open_positions` · `cooldown_bars` · `one_trade_per_zone` / `max_trades_per_zone`.
> فقط بک‌تست: `entry_mode` / `entry_expiry_bars` (مالِ limit) · `slippage_points` · `MANAGEMENT` (breakeven/trail — خاموش).

---

## قیفِ رد شدن (در لاگ) — به همین ترتیب
`no_bos → extended → ema_trend → no_sweep → no_ob → no_zone → zone_not_tapped → no_fvg_overlap → arm_expired → no_choch → rsi_block → no_tp → bad_stop → low_rr → ✅ signal`

بزرگ‌ترین عددِ `-N` در قیف = گلوگاهِ اصلی.

## قانونِ طلایی
هر تغییرِ ضریب را روی **هر دو بازه** (in-sample و OOS) تست کن. اگر فقط in-sample را بهتر کرد و OOS را نه → ردش کن (overfit).
