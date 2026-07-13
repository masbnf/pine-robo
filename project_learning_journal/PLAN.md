# نقشه راه ساخت ژورنال آموزشی — project_learning_journal

این فایل، سند مرجع (context) داخلی برای اجرای مرحله‌به‌مرحلهٔ instruction ذخیره‌شده در پروژه است.
هدفش این است که در هر نشست جدید یا با هر subagent، بدون از دست دادن نظم کار، بشود دید کجا هستیم.
این فایل خودش یکی از فصل‌های ژورنال نیست؛ فقط برای ردیابی پیشرفت است.

**وضعیت کلی (به‌روزرسانی آخر): فاز ۰، ۱، ۲، ۳ کامل. فاز ۴ کامل (فصل‌های ۱۱ تا ۱۴: Risk Sizing،
Take Profit/Breakeven، Paper Broker/Pending Order، چرخهٔ کامل معامله). جمعاً ۱۴ از ۲۰ فصل آماده و
به فهرست (۰۰_index.html) وصل شده‌اند؛ همه با لینک‌های داخلی/تعادل تگ بررسی‌شده (بدون لینک شکسته).

۹ آزمایش تعاملی (از ۱۰ مورد مشخص‌شده در instruction) در فصل‌های ۰۵ تا ۱۳ جاسازی شده‌اند و منتظر
بک‌اند فاز ۶ هستند: `candle_builder`, `trend_detection_basic`, `entry_pivot_detection`,
`bos_choch_classifier`, `sweep_and_reclaim`, `risk_sizing_calculator`, `take_profit_calculator`,
`breakeven_simulator`, `pending_order_lifecycle`. فقط تجربهٔ دهم (بک‌تست کوچک آموزشی) باقی مانده
که در فصل ۱۵ (فاز ۵) جاسازی خواهد شد.

فازهای باقی‌مانده: ۵ (فصل‌های ۱۵–۲۰: بک‌تست + تجربهٔ دهم، گزارش/CLI، تست‌نویسی، راهنمای Lab،
تمرین‌های تجمیعی، واژه‌نامه)، ۶ (`serve_journal.py` — رجیستری Experiments، اعتبارسنجی، sandboxing)،
۷ (پیاده‌سازی واقعی هندلر پایتونی هر ۱۰ تجربه پشت رجیستری فاز ۶، هم‌نام با experimentId های بالا)،
۸ (تست‌های امنیتی/اعتبارسنجی Lab + اجرای pytest موجود پروژه — نیاز به
`pip install pytest --break-system-packages`)، ۹ (QA نهایی و `documentation_report.html`).

نکتهٔ فنی محیط: ویرایش فایل‌ها با ابزار Edit گاهی در mount شل (بش) با تأخیر/استیل دیده می‌شود؛
منبع مرجع صحت فایل، ابزار Read (مسیر ویندوزی) است، نه `cat`/`wc` در بش.**

---

## نکتهٔ مهم دربارهٔ دامنهٔ پروژه (باید در فصل ۱ به خواننده گفته شود)

در ریشهٔ ریپو **دو ربات مجزا** وجود دارد:

1. ربات قدیمی/موازی: `backtest/engine.py`، `config/settings.py`، `data/loader.py`، `run_backtest.py`، `run_live.py`، `STRATEGY.md`، `SMC_clean_logic.pine` — یک بات SMC/MTF جداگانه.
2. **ربات هدف مستندسازی (طبق مثال صریح خود instruction — `pine_ob_bot/paper.py` → `PaperBroker.add_ob`):**
   پکیج `pine_ob_bot/` + اسکریپت اجرایی `run_pine_ob_paper.py` در ریشه. این «Pine Swing-OB paper trader» است که از اندیکاتور Pine استخراج شده.

**تصمیم:** دامنهٔ اصلی مستندات = `pine_ob_bot/` (+ `run_pine_ob_paper.py` و ابزارهای مرتبط در `tools/` در صورت نیاز).
ربات قدیمی ریشه فقط در حد یک پاراگراف توضیحی در فصل ۱ («این را با پروژهٔ دیگر اشتباه نگیرید») اشاره می‌شود، مستند مستقل نمی‌گیرد — چون instruction صراحتاً روی مسیر و کلاس‌های `pine_ob_bot` مثال زده.

---

## نکات محیطی

* `pytest` روی سندباکس فعلی نصب نیست → پیش از فاز ۸ باید با
  `pip install pytest --break-system-packages` نصب شود تا هم تست‌های موجود پروژه و هم تست‌های runner مستندات اجرا شوند.
* دیتای CSV اصلی (`data/*.csv`) حجیم است → طبق instruction استفاده نمی‌شود؛ برای آزمایش بک‌تست تعاملی، دیتاست کوچک مصنوعی جدا در `runtime_data/` ساخته می‌شود.
* پوشه‌های خروجی/لاگ سنگین موجود (`logs/`, `pine_ob_bot_data/`, `$out/`) نباید در مستندات کپی یا اجرا شوند.

---

## نقشهٔ اولیهٔ ماژول‌ها (خروجی فاز ۰)

| ماژول | خط | نقش احتمالی (باید در فاز ۰ کامل تأیید شود) |
|---|---|---|
| `pine_ob_bot/models.py` | 124 | دیتاکلاس‌ها (Candle و…) |
| `pine_ob_bot/config.py` | 222 | `BotConfig` و پارامترها |
| `pine_ob_bot/trend_filter.py` | 83 | تشخیص Trend/Swing روی M5 |
| `pine_ob_bot/entry_pivot.py` | 111 | Entry Pivot |
| `pine_ob_bot/structure_context.py` | 126 | BOS / CHoCH |
| `pine_ob_bot/liquidity_context.py` | 178 | Sweep / نقدینگی |
| `pine_ob_bot/mtf_context.py` | 89 | کانتکست M15 |
| `pine_ob_bot/position_sizing.py` | 85 | Risk Sizing |
| `pine_ob_bot/paper.py` | 1208 | `PaperBroker`، چرخهٔ Order Block/Pending Order، Fill، SL/TP، Breakeven |
| `pine_ob_bot/pine_engine.py` | 164 | موتور اجرای منطق Pine |
| `pine_ob_bot/historical.py` | 207 | بارگذاری/اجرای بک‌تست کندلی |
| `pine_ob_bot/tick_historical.py` | 189 | بک‌تست تیکی |
| `pine_ob_bot/reporting.py` | 366 | گزارش‌سازی |
| `pine_ob_bot/report_worker.py` | 44 | Worker گزارش |
| `pine_ob_bot/storage.py` | 59 | Persistence (SQLite) |
| `pine_ob_bot/app.py` | 534 | ارکستراسیون اصلی / CLI |
| `pine_ob_bot/cli_risk.py` | 50 | ابزار CLI ریسک |
| `pine_ob_bot/dashboard_server.py` | 49 | داشبورد |
| `pine_ob_bot/market_recorder.py` | 66 | ضبط داده بازار |
| `pine_ob_bot/mt5_feed.py` | 103 | اتصال MT5 (فقط توضیح؛ در Lab اجرا نمی‌شود) |
| `pine_ob_bot/demo_executor.py` | 212 | اجرای Demo (فقط توضیح؛ در Lab اجرا نمی‌شود) |
| `pine_ob_bot/tests/*.py` (۱۰ فایل) | 1808 | تست‌های موجود پروژه |

مستندات کمکی موجود که باید به‌عنوان مرجع (نه منبع کپی) بررسی شوند: `README.md`, `STRATEGY.md`, `docs/ROBOT_TECHNICAL_GUIDE.md`, `docs/ENTRY_FUNNEL_FINDINGS.md`.

---

## فازهای اجرا

### فاز ۰ — تحلیل کامل و نقشهٔ فصل‌ها ✅ (این نشست، سطح اول انجام شد)
- [x] بررسی ساختار کلی ریپو و افتراق دو بات
- [x] شمارش خطوط و فهرست ماژول‌های `pine_ob_bot`
- [ ] خواندن کامل تک‌تک فایل‌های `pine_ob_bot/*.py` و `tests/*.py` (سطح تابع/کلاس) — **هنوز انجام نشده، پیش‌نیاز فازهای ۲ تا ۵**
- [ ] تثبیت نهایی نام و ترتیب فصل‌ها (لیست پیشنهادی پایین همین فایل)
- [ ] فهرست بدهی‌های فنی/ابهامات اولیه برای `documentation_report.html`

### فاز ۱ — اسکلت خروجی
- ساخت `project_learning_journal/`, `assets/css`, `assets/js`, `assets/img`, `runtime_data/`
- قالب پایهٔ HTML مشترک: راست‌چین، UTF-8، فونت فارسی fallback، ناوبری «فصل قبل / فصل بعد / فهرست»، TOC داخلی، سبک بلوک کد آفلاین
- یک CSS مشترک (`assets/css/journal.css`)

### فاز ۲ — فصل‌های پایه (کلیات)
`00_index.html`, `01_project_overview.html`, `02_architecture.html`, `03_execution_flow.html`, `04_configuration.html`, `05_data_models.html`
شامل نمودار معماری، وابستگی ماژول‌ها، جریان اجرا (Mermaid/SVG آفلاین).

### فاز ۳ — فصل‌های منطق معاملاتی هسته
تشخیص Trend روی M5، Entry Pivot، Order Block، BOS، CHoCH، Sweep and Reclaim — هرکدام فصل مستقل طبق تفکیک صریح instruction.

### فاز ۴ — فصل‌های مدیریت معامله
Risk Sizing، Take Profit، Breakeven، Paper Broker، چرخهٔ کامل Pending Order، و فصل مستقل «چرخهٔ کامل یک معامله از ورود داده تا بسته‌شدن».

### فاز ۵ — فصل‌های بک‌تست/گزارش/CLI/تست/واژه‌نامه/تمرین
Backtesting، Paper Trading vs Demo Execution، Reporting، CLI Tools، Testing (توضیح Arrange/Act/Assert هر تست موجود)، `17_exercises.html`، `18_glossary.html`.

### فاز ۶ — بک‌اند آزمایشگاه تعاملی
`serve_journal.py`: سرور محلی، رجیستری Experiments، اعتبارسنجی schema ورودی، ممنوعیت `eval`/`exec`/شبکه/فایل دلخواه، timeout و محدودیت منابع.

### فاز ۷ — پیاده‌سازی ۱۰ آزمایش تعاملی
Candle، Swing/Trend، Entry Pivot، BOS/CHoCH، Sweep and Reclaim، Risk Sizing، Take Profit، Breakeven، چرخهٔ Pending Order، Backtest کوچک — هرکدام با فرانت‌اند (ورودی کنترل‌شده، مراحل اجرا، JSON خام، حالت مقایسه در جای مناسب).

### فاز ۸ — تست محیط تعاملی
تست اجرای صحیح هر experiment، رد experiment ناشناخته/ورودی نامعتبر/path traversal/command injection، timeout، عدم تغییر فایل‌های اصلی، smoke test سرور. + نصب و اجرای تست‌های موجود پروژه (`pine_ob_bot/tests`).

### فاز ۹ — کنترل کیفیت نهایی و گزارش
بررسی لینک‌های داخلی، ترتیب فصل‌ها، بازشدن آفلاین، پوشش کامل فایل‌های پروژه، عدم تغییر فایل‌های اصلی، اجرای همهٔ experimentها با مقادیر min/max/نامعتبر، ثبت زمان اجرا، ساخت `documentation_report.html`، فهرست نهایی فایل‌های ساخته‌شده.

---

## لیست پیشنهادی فصل‌ها (قابل تغییر در پایان فاز ۰ کامل)

00_index · 01_project_overview · 02_architecture · 03_execution_flow · 04_configuration ·
05_data_models · 06_trend_detection · 07_entry_pivots · 08_order_blocks_bos_choch ·
09_sweep_and_reclaim · 10_entry_logic · 11_risk_sizing · 12_take_profit_breakeven ·
13_paper_broker_pending_orders · 14_full_trade_lifecycle · 15_backtesting ·
16_reporting_cli · 17_testing · 18_interactive_lab_guide · 19_exercises · 20_glossary

سپس `documentation_report.html` در ریشهٔ `project_learning_journal/` (خارج از شمارهٔ فصل‌ها).

---

## قدم بعدی

اجرای کامل فاز ۰ (خواندن تک‌تک فایل‌های `pine_ob_bot`) و سپس شروع فاز ۱. با توجه به حجم کار (۲۰+ فصل HTML، سرور تعاملی، ۱۰ آزمایش، تست‌ها، گزارش نهایی)، پیشنهاد می‌شود هر فاز در یک نشست/اجرای جداگانه پیش برود و این فایل بعد از هر فاز به‌روزرسانی شود.
