# راهنمای فنی جامع ربات Pine Swing-OB

این سند مرجع فنی زنده پروژه است. نسخه توضیح‌داده‌شده، همان کد موجود در شاخه فعلی است؛ فایل `docs/source_manifest.json` اثر انگشت آن را نگه می‌دارد و تست‌ها اجازه نمی‌دهند کد تحت پوشش بدون تأیید به‌روزرسانی همین سند تغییر کند.

> «خط‌به‌خط» در این سند یعنی تمام بلوک‌های اجرایی، کلاس‌ها و توابع به ترتیب کد توضیح داده شده‌اند. خطوط خالی، importهای بدیهی و CSS تولیدی جداگانه تفسیر نشده‌اند. شماره خط برای یافتن سریع است؛ نام کلاس/تابع مرجع پایدار محسوب می‌شود.

## 1. هدف و پروفایل فعلی

ربات روی XAUUSD و کندل بسته‌شده M5 ساختار Swing، BOS/CHoCH و Swing Order Block را استخراج می‌کند. اجرای مجازی با Bid/Ask انجام می‌شود؛ با سوییچ صریح `--demo-orders` همان ورود/خروج به حساب **Demo** در MT5 نیز mirror می‌شود. داده تیک و کندل برای forward test ذخیره و گزارش HTML روزانه و تجمعی در پس‌زمینه تولید می‌شود.

پروفایل پیش‌فرض runner:

| پارامتر | مقدار |
|---|---:|
| Trend Swing (M5) | 12 |
| Entry Pivot (چپ/راست) | 5/5 |
| شکست‌های مجاز | BOS + CHoCH |
| RR | 1.5 |
| مدل ریسک | CHoCH + Displacement تطبیقی |
| سقف ریسک CHoCH | 0.5% |
| پوزیشن هم‌زمان | 1 |
| ورود | Limit مجازی |
| ثبت بازار / داشبورد | فعال |
| ارسال سفارش Demo | خاموش، مگر با `--demo-orders` |

نکته: مقدارهای dataclass در `BotConfig` الزاماً همان پروفایل runner نیستند؛ `run_pine_ob_paper.py` گزینه‌های CLI را روی آن‌ها اعمال می‌کند.

## 2. معماری و ارتباط اجزا

```text
MT5 terminal
  ├─ closed M5 candles ─> MT5ReadOnlyFeed ─> PaperApp.on_closed_candle
  │                                         ├─ PaperBroker replay execution
  │                                         ├─ M15/Liquidity context
  │                                         ├─ PineSwingOBEngine
  │                                         ├─ CHoCH/Displacement context
  │                                         └─ SQLite snapshot/events
  └─ Bid/Ask ticks ─────> PaperApp.on_tick ─> PaperBroker tick execution
                                            ├─ MarketRecorder CSV
                                            └─ DemoExecutor (فقط با مجوز صریح)

SQLite + broker snapshot ─> ReportWorker ─> CSV/HTML ─> DashboardServer
Historical CSV ───────────> run_historical ─> همان Engine و PaperBroker
MT5 copy_ticks_range ─────> monthly CSV.GZ ─> tick_historical ─> Bid/Ask backtest
```

اصل مهم معماری: مسیر historical و live موتور سیگنال و broker یکسان دارند. تفاوت فقط منبع داده و کیفیت اجرای intrabar است.

## 3. چرخه اجرا

1. runner آرگومان‌ها را می‌خواند و `BotConfig` را می‌سازد.
2. `PaperApp` تنظیمات را validate، لاگ و SQLite را باز، به MT5 وصل و مشخصات symbol/equity را دریافت می‌کند.
3. اگر snapshot وجود داشته باشد state بازیابی و کندل‌های جاافتاده replay می‌شوند؛ در اجرای اول فقط warm-up انجام می‌شود و سود تاریخی جعلی ساخته نمی‌شود.
4. حلقه اصلی هر حدود 1 ثانیه tick را می‌خواند، هر 5 ثانیه کندل بسته جدید را بررسی می‌کند و هر 5 دقیقه snapshot گزارش را به worker می‌دهد.
5. worker گزارش‌های سنگین را خارج از حلقه معامله می‌سازد؛ breakdown تجمعی هر 15 دقیقه است.
6. هنگام خروج، گزارش نهایی، state و CSVها ذخیره و feed/server بسته می‌شوند.

## 4. منطق بازار و Pine port

### ATR و حذف کندل پرنوسان

True Range برابر بیشینه `high-low`، فاصله high تا close قبلی و فاصله low تا close قبلی است. ATR با RMA سازگار با Pine محاسبه می‌شود. اگر دامنه کندل حداقل `2 × ATR(200)` باشد، high/low تجزیه‌شده جابه‌جا می‌شوند تا کندل غیرعادی pivot کاذب تولید نکند.

### Trend Swing (ساختار اصلی، تنها منبع Trend State)

از نسخه فعلی دو سیستم Swing کاملاً مستقل روی M5 اجرا می‌شوند که نقش هرکدام عمداً جدا نگه داشته شده:

- **Trend Swing** (`cfg.trend_swing_length`، پیش‌فرض 12): همان موتور state-based قبلی (`PineSwingOBEngine._update_swing`). تنها منبع Major Swing High/Low، Major BOS/CHoCH و تک state جهت معامله (`current_m5_trend` روی broker) است. تأخیر تأیید آن حدود `trend_swing_length × 5` دقیقه است (پیش‌فرض ۶۰ دقیقه).
- **Entry Pivot** (`cfg.entry_pivot_left`/`cfg.entry_pivot_right`، پیش‌فرض 5/5): پیوت کلاسیک متقارن و کاملاً مستقل (`entry_pivot.py`، کلاس `ClassicEntryPivotDetector`) که **فقط** برای تأیید/timing محلی pullback و trigger روی یک OB از قبل ساخته‌شده توسط Trend Swing استفاده می‌شود. این پیوت هرگز `current_m5_trend` را عوض نمی‌کند و هرگز به‌تنهایی OB خلاف‌جهت جدید نمی‌سازد. تأخیر تأیید آن فقط سمت راست است، یعنی `entry_pivot_right × 5` دقیقه (پیش‌فرض ۲۵ دقیقه)؛ سمت چپ چون کندل آینده لازم ندارد تأخیری اضافه نمی‌کند.

پس از وجود `trend_swing_length` کندل سمت راست، کاندیدای قدیمی Trend Swing بررسی می‌شود. عبور parsed high/lowهای جدید تعیین می‌کند leg عوض شده است یا نه. با تغییر leg، `swing_high` یا `swing_low` جدید ساخته می‌شود. این تأخیر تأیید intentional است و look-ahead نیست: pivot فقط در زمان قابل‌دانستن ثبت می‌شود.

Entry Pivot با همان منطق ولی پنجره متقارن (`left_bars`/`right_bars`) و مقایسه سخت‌گیرانه `>`/`<` (تساوی رد می‌شود) کار می‌کند؛ یک کاندیدا فقط وقتی `right_bars` کندل بسته بعد از خودش موجود باشد منتشر می‌شود (`EntryPivot.confirmed_at`) و هر پیوت دقیقاً یک‌بار منتشر می‌شود. `PaperBroker.add_ob` قبل از ساخت سفارش، وجود یک Entry Pivot هم‌جهت را شرط می‌گذارد (`enable_entry_pivot_gate`/`update_entry_pivot`)؛ این gate در تست‌های قدیمی که مستقیم `add_ob` صدا می‌زنند به‌صورت پیش‌فرض غیرفعال است اما در هر سه مسیر واقعی اجرا (`PaperApp`، `run_historical`، `run_tick_historical`) همیشه فعال می‌شود.

### BOS و CHoCH

شکست فقط با **close** معتبر است و همیشه از Trend Swing می‌آید، نه از Entry Pivot:

- عبور close از swing high: شکست bull.
- عبور close از swing low: شکست bear.
- اگر شکست خلاف trend فعلی باشد CHoCH، وگرنه BOS است.
- هر pivot فقط یک‌بار با پرچم `crossed` مصرف می‌شود.

### ساخت و ابطال Order Block

در مسیر pivot تا کندل شکست، برای bull کمترین parsed low و برای bear بیشترین parsed high انتخاب می‌شود؛ high/low کندل منبع محدوده OB است. Bull OB در عبور low زیر کف و Bear OB در عبور high بالای سقف باطل می‌شود. ورود bull سقف OB و stop کف آن است؛ برای bear برعکس.

## 5. اجرای معامله و قیمت‌ها

### Tick زنده

اجرای زنده کاملاً tick-only است: کندل بسته M5 فقط سیگنال، context، سن بازار و lifecycle سفارش را به‌روزرسانی می‌کند و اجازه ندارد pending را fill یا position را با OHLC ببندد. ورود، SL، TP و MAE/MFE زنده فقط از ترتیب واقعی Bid/Ask تیک‌ها ساخته می‌شوند. در حالت `sweep_reclaim` کندل بسته صرفاً reclaim را تأیید می‌کند و fill با قیمت نخستین تیک مجاز بعدی انجام می‌شود.

- ورود Long با Ask و خروج Long با Bid سنجیده می‌شود.
- ورود Short با Bid و خروج Short با Ask سنجیده می‌شود.
- tick تکراری و tick قدیمی‌تر از 30 ثانیه معامله نمی‌شود.
- ورود وقتی spread از سقف تطبیقی عبور کند متوقف می‌شود: ۱۰٪ ATR زمان تشکیل OB و، پس از حداقل ۲۰ نمونه، سه برابر میانه ۳۰۰ تیک اخیر. سقف سخت‌گیرانه‌تر اعمال می‌شود؛ فیلتر عدد ثابت دلاری ندارد.
- pendingها بر اساس زمان ساخت مرتب‌اند؛ قدیمی‌تر اولویت دارد.
- به‌طور پیش‌فرض فقط یک position باز می‌شود.

### Replay کندلی

OHLC ورودی Bid فرض شده است. spread fallback برای سمت Ask اعمال می‌شود. positionهای موجود پیش از fill جدید بررسی می‌شوند؛ سفارش در کندل تشکیل خودش مجاز نیست. اگر SL و TP در یک کندل لمس شوند، SL اول است. این سیاست محافظه‌کارانه ابهام مسیر intrabar را حل می‌کند، ولی جای دیتای tick تاریخی را نمی‌گیرد.

### بک‌تست تیک تاریخی

`fetch_mt5_ticks.py` با `copy_ticks_range` هر ماه را روزبه‌روز دانلود می‌کند و برای resume هر روز یک `CSV.GZ` اتمیک می‌سازد. ماه کامل موجود با پیام `RANGE EXISTS` بدون اتصال مجدد رد می‌شود و ماه ناقص فقط روزهای مفقود را می‌گیرد. تمام مرزها UTC و نیمه‌باز `[start,end)` هستند تا نیمه‌شب روز/ماه بعد تکراری نشود. ماه جاری فقط تا آخرین روز کامل دریافت می‌شود. `run_tick_backtest.py` فایل‌ها را streaming می‌خواند، M5 Bid را بدون look-ahead می‌سازد، سیگنال را فقط پس از بسته‌شدن M5 تولید و ورود/SL/TP را با ترتیب واقعی Bid/Ask اجرا می‌کند. ۵۰۰ کندل اول پیش‌فرض فقط warm-up هستند و OBهای آن دوره قابل معامله نمی‌شوند؛ بنابراین OB تاریخی bootstrap وارد نتیجه ماه نمی‌شود.

### قیمت هدف و حالت ورود

برای کارایی replayهای بزرگ، tick CSV با index ستون‌های خوانده‌شده از header و bucket مستقیم UTC پردازش می‌شود؛ بنابراین برای هر Tick شیء datetime ساخته نمی‌شود. `run_tick_risk_comparison.py` چند ماه را پیوسته با یک replay قیمت اجرا می‌کند و مدل‌هایی را که فقط sizing را تغییر می‌دهند روی همان sequence معامله، با volume rounding و equity compounding مستقل، بازسازی می‌کند.

`fixed_rr` هدف را با فاصله stop ضربدر RR می‌سازد. `m5_liquidity_min_rr` فقط وقتی نزدیک‌ترین liquidity سطح reward حداقل RR را بدهد آن سطح را جایگزین می‌کند. `sweep_reclaim` به‌جای limit، sweep کامل OB و close برگشتی همان کندل را می‌خواهد و stop را با ATR buffer گسترش می‌دهد.

## 6. ریسک و حجم

فرمول‌ها:

```text
risk_money = equity × selected_risk_fraction
loss_per_lot = abs(entry-stop) / tick_size × tick_value
raw_volume = risk_money / loss_per_lot
volume = floor(raw_volume / volume_step) × volume_step
```

حجم سپس بین حداقل و حداکثر symbol محدود می‌شود. ریسک واقعی پس از گردکردن داخل position ثبت می‌شود.

مدل پیش‌فرض runner دو امتیاز دارد: CHoCH مخالف در لحظه fill و displacement در top quartile داده‌های گذشته. امتیاز 0/1/2 به‌ترتیب ریسک 0.75%/1%/1.25% می‌دهد، اما معامله‌ای که setup آن CHoCH است با `choch_risk_cap_fraction=0.5%` محدود می‌شود. percentile فقط از نمونه‌های **قبلی** ساخته می‌شود تا leakage رخ ندهد.

### `--fixed-risk`: یک نرخ ریسک یکسان برای BOS و CHoCH

`--fixed-risk` (هم در `run_pine_ob_paper.py` هم در `tools/run_tick_backtest.py`) مدل پیش‌فرض بالا و سقف CHoCH را کاملاً کنار می‌گذارد: هر شش مدل تطبیقی (`liquidity_risk_sizing_enabled` … `three_factor_risk_sizing_enabled`) خاموش و `choch_risk_cap_fraction=None` می‌شود؛ در نتیجه BOS و CHoCH هر دو دقیقاً از یک عدد، `BotConfig.risk_fraction`، استفاده می‌کنند. تنها اختلاف مجاز بین ریسک برنامه‌ریزی‌شده و ریسک واقعی، گردکردن رو به پایین `volume_step` هنگام lot sizing است (بخش بالا).

پیش از این اصلاح، هر دو runner صرف‌نظر از `--fixed-risk` مقدار پیش‌فرض `choch_risk_cap_fraction=0.005` را به `BotConfig` پاس می‌دادند؛ نتیجه این بود که BOS با `risk_fraction` کامل (پیش‌فرض 1%) و CHoCH همچنان با سقف 0.5% معامله می‌شد — یعنی `--fixed-risk` نه fixed بود نه uniform. resolve و اعتبارسنجی حالا در `pine_ob_bot/cli_risk.py` متمرکز است تا هر دو runner دقیقاً یک رفتار داشته باشند، نه دو پیاده‌سازی دستی موازی:

- `DEFAULT_CHOCH_RISK_CAP = 0.005`.
- `resolve_choch_risk_cap(fixed_risk, explicit_cap)`: با `--fixed-risk` همیشه `None`؛ بدون آن، `--choch-risk-cap` صریح کاربر یا در نبود آن همان 0.5% پیش‌فرض.
- `validate_fixed_risk_args(parser, args)`: اگر `--fixed-risk` با هر یک از شش فلگ مدل تطبیقی یا با `--choch-risk-cap` صریح ترکیب شود، پیش از شروع هر کار (حتی پیش از glob فایل‌های تیک) با `parser.error()`/`SystemExit` متوقف می‌شود — این تناقض هرگز به‌سکوت نادیده گرفته نمی‌شود.

هر دو runner پارسر `--choch-risk-cap` را با `default=None` (نه 0.005) می‌سازند تا «کاربر چیزی نداده» از «کاربر صریحاً 0.005 خواسته» قابل تفکیک باشد، و بلافاصله بعد از `parser.parse_args` و پیش از `resolve_swing_settings`، `validate_fixed_risk_args(parser, args)` را صدا می‌زنند.

مدل‌های آزمایشی دیگر (liquidity، CHoCH، combined، displacement و three-factor) mutually exclusive هستند و `validate()` فعال‌کردن هم‌زمان آن‌ها را رد می‌کند.

برای آزمایش ریسک بدون تغییر admission، دو override تفکیک‌شده وجود دارد: مدل سه‌عاملی مختص BOS و مدل CHoCH با سه سطح toxic/sweep/other. bonus اختیاری M15-counter فقط روی BOS اعمال و ریسک نهایی آن در ۱.۲۵٪ محدود می‌شود. این overrideها برای مقایسه تحقیقاتی‌اند و runner زنده آن‌ها را به‌صورت پیش‌فرض فعال نمی‌کند.

## 7. contextهای تحلیلی

- `M15Context`: هر سه M5 را بر اساس bucket زمانی به یک M15 تبدیل و همان Pine engine را روی M15 اجرا می‌کند؛ trend، alignment، آخرین break، range zone و OB فعال را فقط به meta اضافه می‌کند.
- `LiquidityTracker`: swing liquidityهای BSL/SSL، وضعیت active/swept/reclaimed، عمق sweep بر حسب ATR و سن رویداد را نگه می‌دارد. setup-related sweep باید در leg مربوط به همان setup رخ داده باشد.
- `ChochContext`: آخرین CHoCH هر جهت و alignment آن با جهت معامله را در formation/fill ثبت می‌کند.
- `DisplacementContext`: body/range، close-through و close location کندل شکست را ATR-normalize و رتبه causal آن را محاسبه می‌کند.

این contextها به‌جز مدل ریسک انتخاب‌شده فیلتر سخت نیستند؛ در CSV/HTML می‌مانند تا forward-test قابل اندازه‌گیری باشد.

## 8. persistence و restart

SQLite سه نوع داده را نگه می‌دارد: snapshot اتمیک state، event log و tradeهای بسته‌شده. snapshot شامل engine، broker و contextهاست. تاریخچه درون snapshot به 2000 M5 محدود می‌شود و indexها هنگام trim با offset rebasing می‌شوند؛ state فعال قدیمی در صورت نیاز حفظ می‌شود. tradeهای بسته جداگانه از جدول trades بازیابی می‌شوند تا snapshot با رشد forward test سنگین نشود.

در restart:

- timestamp تکراری توسط engine رد می‌شود.
- pendingهای ناسازگار با تنظیم جدید cancel و target pendingها با RR جدید reconcile می‌شوند.
- position باز با پارامتر قبلی دستکاری نمی‌شود.
- کندل‌های بعد از `last_time` برای بازیابی signal/context/lifecycle پردازش می‌شوند، اما هیچ fill یا خروج replay‌شده‌ای تولید نمی‌کنند.
- حلقه live کندل‌های بسته جاافتاده را پیش از tick جاری پردازش می‌کند؛ تمام اجرای معامله پس از catch-up فقط با تیک جاری انجام می‌شود.

## 9. ثبت بازار، گزارش و داشبورد

`MarketRecorder` tickهای distinct و کندل‌های بسته را در فایل روزانه CSV نگه می‌دارد. گزارش‌های اصلی:

| خروجی | کاربرد |
|---|---|
| `daily_reports/paper_report_latest.html` | وضعیت و عملکرد روز جاری |
| `daily_reports/paper_report_YYYY-MM-DD.html` | آرشیو روزانه |
| `daily_reports/forward_test_cumulative.html` | کل forward test |
| `daily_reports/forward_test_trades.csv` | هر معامله همراه تمام metaها |
| `daily_reports/forward_test_daily_metrics.csv` | KPI و تنظیمات هر روز |
| `daily_reports/forward_test_breakdown.csv` | گروه‌بندی تشخیصی فیلترها |
| `paper_report.html` | گزارش نهایی هنگام shutdown |

داشبورد HTTP فقط روی `127.0.0.1:8765` پیش‌فرض سرو می‌شود. HTML هر 30 ثانیه browser refresh دارد، اما محاسبه آن هر 5 دقیقه و breakdown هر 15 دقیقه است. `ReportWorker` صف تک‌عضوی دارد؛ اگر report قبلی هنوز در حال ساخت باشد درخواست جدید drop می‌شود تا tick loop متوقف نشود.

## 10. ارسال Demo و مرز ایمنی

`DemoExecutor` تنها بخش دارای `order_send` است. بدون `--demo-orders` ساخته نمی‌شود. در زمان اتصال، حساب غیر-Demo رد می‌شود. ورود market با volume/SL/TP مجازی mirror و ticket در meta ذخیره می‌شود؛ خروج با ticket یا comment مربوط به position پیدا و با سفارش مخالف بسته می‌شود. paper ledger منبع تصمیم است؛ slippage واقعی demo جداگانه ثبت می‌شود.

پیش از ورود Demo، حجم از مقدار برنامه‌ریزی‌شده رو به پایین و بر مبنای `volume_step` جست‌وجو می‌شود. هر کاندید باید هم `order_calc_margin <= 80% free margin` و هم `order_check` موفق داشته باشد؛ اگر حتی `volume_min` مجاز نباشد سفارش رد و Paper fill rollback می‌شود. مقدار ۸۰٪ با `demo_max_free_margin_fraction` قابل تنظیم است.

پس از پذیرش سفارش، entry، volume و risk ledger با `result.price/result.volume` واقعی بازسازی می‌شوند؛ target ارسال‌شده تغییر نمی‌کند و RR واقعی دوباره محاسبه می‌شود. هنگام خروج نیز exit، PnL، R و equity با اجرای واقعی Demo بازقیمت‌گذاری می‌شوند. اگر SL/TP قبلاً در broker بسته شده باشد، history deal همان position برای بازیابی actual exit خوانده می‌شود. قیمت‌ها و مقادیر قبل/بعد از sync در meta و CSV باقی می‌مانند.

در حالت Demo، موفقیت سفارش MT5 شرط نهایی ورود tick است. اگر server ورود را رد کند (مانند `No money`)، position مجازی فوراً rollback و رخدادهای `demo_open_failed` و `paper_open_reverted` ثبت می‌شوند؛ معامله ردشده وارد PnL Forward Test نمی‌شود.

وضعیت هماهنگی execution در meta صریح است: fill تیکی ابتدا `demo_confirmation_pending`، پس از پذیرش MT5 برابر `demo_confirmed_active` و هنگام خروج `demo_exit_confirmation_pending` می‌شود. تأیید خروج آن را به `demo_exit_confirmed` می‌برد؛ خطای خروج یا نبودن پوزیشن مورد انتظار وضعیت `reconciliation_required_*` می‌سازد تا اختلاف پنهان نماند.

در startup، `DemoExecutor.reconcile` پوزیشن‌های باز MT5 با symbol و magic ربات را به‌صورت read-only با positionهای snapshot Paper تطبیق می‌دهد. تطبیق با ticket و سپس comment پایدار انجام می‌شود. موارد matched state را ترمیم می‌کنند؛ Paperهای بدون همتای MT5 به‌عنوان `demo_position_missing` و پوزیشن‌های MT5 بدون همتای Paper به‌عنوان `demo_position_orphan` در SQLite ثبت می‌شوند. reconciliation برای ایمنی هیچ سفارش جدید یا close خودکاری ارسال نمی‌کند.

## 11. راهنمای فایل‌به‌فایل و بلوک‌به‌بلوک

شماره‌ها مطابق نسخه manifest فعلی هستند.

### `run_pine_ob_paper.py` (1–250)

- 1–19: docstring معماری دوگانه Trend Swing/Entry Pivot، importهای config/app/feed/historical و `pine_ob_bot.cli_risk` (`resolve_choch_risk_cap`, `validate_fixed_risk_args`).
- `build_parser`، 33–125: تعریف تمام CLIها شامل `--trend-swing-length`، `--entry-pivot-left/right` و `--swing-length` (deprecated alias). `--choch-risk-cap` اکنون `default=None` دارد (نه 0.005) تا «صریح نداده» از «صریح 0.005 خواسته» قابل تفکیک باشد؛ help متن `--fixed-risk` قرارداد کامل را توضیح می‌دهد (یکسان‌سازی BOS/CHoCH روی `risk_fraction`، خاموشی هر مدل تطبیقی و سقف CHoCH، و تناقض با فلگ‌های دیگر).
- `resolve_swing_settings`، 127–158: تعیین قطعی `(trend_swing_length, entry_pivot_left, entry_pivot_right)`؛ بدون فلگ → 12/5/5، فقط `--swing-length` یا فقط `--trend-swing-length` هرکدام مقدار خودش را می‌دهد، هر دو با مقدار برابر مجاز و هر دو با مقدار متفاوت خطای صریح `parser.error` است. کران پایین هر سه پارامتر اینجا اعتبارسنجی می‌شود.
- `build_config`، 161–202: تبدیل آرگومان‌ها به `BotConfig`؛ `choch_risk_cap_fraction` از `resolve_choch_risk_cap(args.fixed_risk, args.choch_risk_cap)` می‌آید (نه مستقیم از `args.choch_risk_cap`)؛ شرط انتخاب مدل تطبیقی پیش‌فرض بدون تغییر باقی مانده (زیر `--fixed-risk` طبیعتاً به False می‌رسد چون هیچ فلگ تطبیقی دیگری هم‌زمان مجاز نیست).
- `main`، 205–245: بلافاصله بعد از `parser.parse_args`، `validate_fixed_risk_args(parser, args)` صدا می‌شود (پیش از `resolve_swing_settings` و پیش از هر کار MT5/فایل)؛ سپس مسیر مشترک برای هر دو حالت: ساخت مسیر پیش‌فرض `--db` (`paper_t{trend}_p{left}x{right}.sqlite3`) و `run-label` (`t{trend}_p{left}x{right}`) وقتی صریح داده نشده باشند، چاپ تأخیر تأیید Trend Swing و Entry Pivot به دقیقه، branch بک‌تست یا live، مدیریت خطا.
- 248–250: تبدیل return code به exit code سیستم.

### `pine_ob_bot/cli_risk.py` (1–50، فایل جدید)

منبع مشترک resolve/validate برای `--fixed-risk` و `--choch-risk-cap`، تا `run_pine_ob_paper.py` و `tools/run_tick_backtest.py` دو پیاده‌سازی دستی موازی نداشته باشند که می‌توانستند از هم واگرا شوند (همان باگی که این فایل رفعش کرد).

- `DEFAULT_CHOCH_RISK_CAP`: مقدار پیش‌فرض استاندارد سقف ریسک CHoCH، 0.005.
- `ADAPTIVE_RISK_SIZING_FLAGS`: نگاشت dest→flag شش سوییچ مدل تطبیقی، برای پیام خطای یکسان در هر دو runner.
- `validate_fixed_risk_args(parser, args)`: اگر `args.fixed_risk` باشد، هر فلگ تطبیقی فعال یا `--choch-risk-cap` صریح را با `parser.error()` (یعنی `SystemExit`) رد می‌کند؛ بدون `--fixed-risk` بی‌اثر است.
- `resolve_choch_risk_cap(fixed_risk, explicit_cap)`: `--fixed-risk` → `None`؛ وگرنه `explicit_cap` اگر داده شده باشد، وگرنه `DEFAULT_CHOCH_RISK_CAP`.

### `config.py` (1–191)

- `BotConfig`: قرارداد مرکزی پارامترهای strategy، risk، persistence، capture، reporting، dashboard و سقف مصرف free-margin در Demo. `trend_swing_length` (پیش‌فرض 12) و `entry_pivot_left`/`entry_pivot_right` (پیش‌فرض 5/5) دو فیلد مستقل‌اند؛ `swing_length` فقط یک `InitVar` سازگاری قدیمی است.
- `__post_init__`، 94–99: اگر `swing_length=` صریح داده شده باشد، مقدارش را روی `trend_swing_length` می‌نشاند؛ در غیر این صورت تغییری نمی‌دهد.
- `validate`، 101–175: invariantها؛ M5-only، بازه Trend Swing و Entry Pivot، exclusivity مدل ریسک و صحت timer/port/demo.
- انتهای فایل (177–191): alias خواندنی `swing_length` عمداً **بیرون** از بدنه کلاس، به‌صورت `BotConfig.swing_length = property(...)` روی کلاس نهایی نصب می‌شود. دلیل: تعریف یک `@property` هم‌نام با `InitVar` داخل بدنه کلاس، مقدار پیش‌فرض `None` آن `InitVar` را در namespace کلاس (که `@dataclass(slots=True)` هنگام decorate کردن از همان‌جا می‌خواند) با خودِ آبجکت property جایگزین می‌کرد؛ نتیجه این بود که هر `BotConfig()` بدون `swing_length=` صریح، مقدار `trend_swing_length` را به یک آبجکت `property` (نه عدد صحیح) خراب می‌کرد. این باگ واقعی در همین نشست کشف و اصلاح شد.

### `models.py` (1–124)

- 6: نوع جهت فقط `bull|bear`.
- `Candle`/`Tick`، 10–23: ورودی‌های بازار.
- `Pivot`/`StructureBreak`، 26–40: state و رخداد ساختار.
- `OrderBlock`، 43–64: محدوده و propertyهای entry/stop جهت‌محور.
- `PendingOrder`، 66–80: سفارش مجازی و lifecycle.
- `PaperPosition`، 83–97: position باز، excursion و meta.
- `Trade`، 100–120: رکورد بسته و معیارهای R/MAE/MFE.
- `to_dict`، 123–124: serialization dataclass.

### `entry_pivot.py` (1–112, فایل جدید)

- `EntryPivot`، 42–49: dataclass فقط‌خواندنی؛ `pivot_time` زمان کاندیدا و `confirmed_at` زمان کندلی است که پنجره راست را بست (تصمیم معاملاتی همیشه باید از `confirmed_at` بیاید، نه `pivot_time`).
- `ClassicEntryPivotDetector.__init__`، 61–71: اعتبارسنجی `left_bars/right_bars >= 1`، بافر کندل‌ها و `index_offset` برای پیوستگی پس از trim.
- `process`، 73–95: کاندیدا با `>`/`<` سخت‌گیرانه (تساوی رد می‌شود) در برابر `left_bars` کندل قبل و `right_bars` کندل بعد سنجیده می‌شود؛ فقط وقتی هر دو پنجره کامل باشند پیوت منتشر می‌شود (سختگیرانه causal، بدون look-ahead، هر پیوت دقیقاً یک‌بار).
- `dump_state`/`load_state`، 97–112: trim تاریخچه و بازسازی هم‌راستا با الگوی `PineSwingOBEngine`.

### `pine_engine.py` (1–164)

- `__init__`، 12–24: آرایه‌های سری زمانی و state leg/trend/swing/OB.
- `process`، 26–71: dedupe، TR/ATR، volatility parsing، pivot update، شکست close، OB و invalidation.
- `_next_atr`، 73–82: seed SMA و سپس RMA.
- `_update_swing`، 84–102: تأیید delayed pivot (بر اساس `cfg.trend_swing_length`) و تغییر leg. این تابع همچنان فقط Trend Swing/Major BOS-CHoCH را می‌سازد؛ Entry Pivot کاملاً جدا در `entry_pivot.py` پیاده شده است.
- `_make_ob`، 104–118: extreme مسیر pivot-to-break و ساخت OB.
- `dump_state`، 120–152: trim و rebase امن indexها.
- `load_state`، 154–164: بازسازی dataclassها و state.

### `paper.py` (1–857)

- `SymbolSpec`، 22–27: قرارداد tick value/size و قیود lot.
- `PaperBroker.__init__`، 32–89: ledger، صف‌ها، context، counters و state جدید Entry Pivot (`last_pivot_high/low`, `entry_pivot_gate_active`, `pivot_events`)؛ gate به‌صورت پیش‌فرض خاموش است تا تست‌های قدیمی که مستقیم `add_ob` صدا می‌زنند تغییر نکنند.
- `position`، 90–93: compatibility view قدیمی‌ترین position.
- `add_ob`، 95–169: فیلتر break/CHoCH، بررسی Trend State (`validate_trade_direction`)، سپس اگر gate فعال باشد بررسی وجود یک Entry Pivot هم‌جهت قبل از ساخت سفارش (وگرنه `REJECTED_NO_ENTRY_PIVOT`)، RR، meta (شامل `trend_swing_length`, `entry_pivot_left/right`, `m5_trend_at_setup`, `entry_pivot_kind/price/time/confirmed_at`) و FIFO pending.
- `enable_entry_pivot_gate`، 171–181: فعال‌سازی صریح gate؛ فقط سه مسیر واقعی اجرا آن را صدا می‌زنند.
- `update_entry_pivot`، 183–204: ثبت آخرین Entry Pivot تأییدشده هر جهت؛ هرگز `current_m5_trend` را تغییر نمی‌دهد.
- `cancel_ob`، 206–210: invalidation متناظر pending.
- `update_m5_trend`، 212–246: اعمال Trend State جدید (از `PineSwingOBEngine.trend`) و لغو هر pending فعال ناسازگار.
- `reconcile_*`، 248–271: سازگاری pending پس از restart/config change.
- `process_tick`: خروج‌ها، Bid/Ask، spread guard تطبیقی، limit fill و ظرفیت position.
- `process_signal_candle`: به‌روزرسانی market index و lifecycle بدون fill/close؛ در `sweep_reclaim` فقط تأیید reclaim برای fill تیکی بعدی.
- `apply_demo_entry`/`apply_demo_exit`: بازسازی risk/PnL/R/equity با price و volume تأییدشده MT5.
- `process_candle`، 144–201: fallback OHLC، spread، no-lookahead و SL-first.
- `_try_sweep_reclaim`، 203–233: ورود تأییدی و ATR-buffered stop.
- `_active_pending`، 235–236: view سفارش‌های فعال با ترتیب موجود.
- `_open`، 238–335: target liquidity، رتبه سن، مدل ریسک، cap، lot sizing و meta نهایی.
- `_advance_lifecycle`، 337–369: formed → accepted → armed بر اساس extension/retracement.
- `_track_excursion`، 371–375: MAE/MFE خام position.
- `_close`، 377–398: PnL/R، equity/drawdown، Trade و حذف position.
- `dump_state`/`load_state`، 400–442: serialization ledger و بازیابی trades از storage.

### `app.py` (1–503)

- `PaperApp.__init__`، 31–92: validation، logging، MT5/account، ساخت تمام سرویس‌ها شامل `self.entry_pivot = ClassicEntryPivotDetector(cfg.entry_pivot_left, cfg.entry_pivot_right)` و `self.broker.enable_entry_pivot_gate()` بلافاصله بعد از ساخت broker، و dashboard.
- `restore_or_bootstrap`، 94–173: restore/replay یا warm-up بدون PnL تاریخی؛ در هر دو مسیر بلافاصله بعد از `update_m5_trend` نوبت `entry_pivot.process(candle)` و `broker.update_entry_pivot` می‌رسد و state آن هم از/به `StateStore` بازیابی/ذخیره می‌شود.
- `on_closed_candle`، 175–220: ترتیب ثابت هر کندل بسته: signal/context → `engine.process` → `update_m5_trend` (لغو pendingهای ناسازگار) → `entry_pivot.process`/`update_entry_pivot` → ساخت OB/سفارش جدید → save؛ بدون اجرای معامله (اجرای معامله فقط با tick است).
- `on_tick`: dedupe/staleness، paper execution، انتقال state تأیید Demo و ثبت trade.
- `_reconcile_demo_positions`: تطبیق read-only پوزیشن‌های Paper/MT5 در startup و ثبت missing/orphan.
- `run`: scheduler سبک با تقدم catch-up کندل بر tick، report و shutdown قطعی.
- `_refresh_daily_report`، 275–284: rollover روز و timer گزارش.
- `_export_daily_report`، 286–318: snapshot کم‌هزینه و submit به worker.
- `_write_daily_reports`، 320–345: تولید HTML/CSV روزانه و تجمعی در thread worker.
- `_save`، 347–359: trim/rebase و snapshot همه contextها.
- helperهای 361–380: update M15/liquidity و snapshotهای formation/fill/setup.

### اتصال و اجرای MT5

- `mt5_feed.py`: `connect` (22–42) initialize/login، account و symbol spec؛ `_credentials` (44–63) env/config؛ `_resolve_symbol` (65–71) suffix بروکر؛ `closed_candles` (73–92) فقط کندل بسته؛ `tick` (94–100) Bid/Ask؛ `shutdown` (102–103).
- `demo_executor.py`: `open` (25–57) market request با SL/TP؛ `close` (59–85) close-by-opposite؛ `_find_position` (87–96) ticket/comment؛ `_require_success` (98–103) retcode guard.

### contextها

- `mtf_context.py`: `process_m5` bucket سه کندلی، `_flush` ساخت M15 و engine، `snapshot` ویژگی‌های trend/alignment/range، dump/load.
- `liquidity_context.py`: `process` sweep/reclaim، `_confirm_swing` ساخت pool، `_event` تاریخچه، `setup_snapshot` نسبت sweep به setup، `_atr` RMA، `snapshot` متادیتای جهت مخالف، dump/load.
- `structure_context.py`: `displacement_snapshot` ویژگی‌های normalized؛ `ChochContext` ثبت/خواندن آخرین CHoCH؛ `DisplacementContext.observe` percentile causal و state.

### داده، state و گزارش

- `storage.py`: `__init__` schema، `load/save` snapshot اتمیک، `record_event`, `record_trade`, `event_counts`, `close`.
- `market_recorder.py`: مسیر روزانه، dedupe آخرین row و append tick/candle.
- `historical.py`: load CSV، ساخت `PineSwingOBEngine` و `ClassicEntryPivotDetector` جدا از `PaperApp` ولی از همان کلاس‌ها و با همان ترتیب (`update_m5_trend` سپس `entry_pivot.process` سپس `add_ob`)، `broker.enable_entry_pivot_gate()` بلافاصله بعد از ساخت broker، spread ثابت، شمارش `major_bos_count`/`major_choch_count`/`major_swing_highs/lows` و تولید `summary` گسترده‌شده (شامل `trend_swing_length`, `entry_pivot_left/right`, `entry_pivot_highs/lows`, `buy_setups`/`sell_setups`, `setups_rejected_trend_mismatch/neutral_trend/no_entry_pivot`, `pending_orders_created/cancelled_trend_change`, `filled_orders`, `closed_trades`, `average_r`, `max_drawdown`) و خروجی نام‌گذاری‌شده.
- `tick_historical.py`: خواندن streaming تیک‌های روزانه، ساخت M5 Bid، همان `ClassicEntryPivotDetector` بلافاصله بعد از `update_m5_trend` در `close_bar`، warm-up غیرقابل‌معامله و اجرای tick-level همان engine/context/broker؛ `summary` نیز `trend_swing_length`/`entry_pivot_left/right`/`buy_setups`/`sell_setups` را اضافه می‌کند.
- `tools/run_tick_backtest.py`: CLI روی `tick_historical.run_tick_historical`. همان `build_parser`/`resolve_swing_settings`/`build_config`/`main` الگوی `run_pine_ob_paper.py` را دارد و `--choch-risk-cap`/`--fixed-risk` را از همان `pine_ob_bot/cli_risk.py` resolve/validate می‌کند؛ `main` بلافاصله بعد از `parser.parse_args` و **پیش از** glob کردن فایل‌های تیک ماه، `validate_fixed_risk_args` را صدا می‌زند تا ترکیب متناقض پیش از هر I/O متوقف شود.
- `report_worker.py`: thread daemon و queue ظرفیت یک؛ `submit` non-blocking، `close` join، `_run` اجرای callback و log خطا.
- `reporting.py`: `export_daily_html/metrics` فیلتر timezone؛ `export_reports` trade/summary؛ `export_breakdown` گروه‌های تشخیصی؛ `export_html` داشبورد self-contained؛ helperهای group/equity/bucket.
- `dashboard_server.py`: HTTP server thread، index redirect و shutdown.
- `__init__.py`: export عمومی مدل‌ها و config.

## 12. CLI عملیاتی

```powershell
# paper live + capture + dashboard
python run_pine_ob_paper.py

# همان منطق با ارسال واقعی فقط روی حساب Demo
python run_pine_ob_paper.py --demo-orders

# catch-up، گزارش و خروج
python run_pine_ob_paper.py --once

# historical از CSV بازار
python run_pine_ob_paper.py --backtest data.csv --spread 0.20 --run-label sample

# دانلود یک یا چند ماه تیک تاریخی از همان MT5
python tools/fetch_mt5_ticks.py --month 2026-06 --month 2026-05

# بک‌تست Bid/Ask روی ماه دانلودشده
python tools/run_tick_backtest.py --month 2026-06
```

سوییچ‌های مهم: `--trend-swing-length` (جایگزین توصیه‌شده `--swing-length` که همچنان به‌عنوان alias قدیمی کار می‌کند)، `--entry-pivot-left`, `--entry-pivot-right`, `--rr`, `--bos-only`, `--include-choch`, `--fixed-risk`, `--choch-risk-cap`, `--target-mode`, `--entry-mode`, `--min-entry-wait`, `--max-positions`, `--no-market-capture`, `--no-dashboard`, `--db` (پیش‌فرض خودکار از روی Trend Swing/Entry Pivot)، `--run-label` (پیش‌فرض خودکار مشابه). `tools/run_tick_backtest.py` همین سوییچ‌های ریسک را با همان معنا می‌پذیرد؛ فقط `--once`/`--db`/`--backtest` که مخصوص اجرای زنده/OHLC است را ندارد (به‌جایش `--month`/`--data-root`/`--out`).

نکته سازگاری: اگر هم `--swing-length` و هم `--trend-swing-length` داده شوند و مقدارشان متفاوت باشد، runner با خطای صریح متوقف می‌شود؛ اگر برابر باشند مجاز است.

نکته ریسک: `--fixed-risk` با هر یک از `--liquidity-risk-sizing`, `--choch-risk-sizing`, `--combined-context-risk-sizing`, `--displacement-risk-sizing`, `--choch-displacement-risk-sizing`, `--three-factor-risk-sizing` یا با `--choch-risk-cap` صریح ترکیب شود، هر دو runner پیش از شروع هر کار با `parser.error()` متوقف می‌شوند (نگاه کنید به بخش «۶. ریسک و حجم» و `pine_ob_bot/cli_risk.py`).

## 13. تست، محدودیت و performance

تست‌ها pivot/ATR/BOS/CHoCH/OB، Bid/Ask و gap، SL-first، position sizing، restart، contextها، demo guard، recorder، dashboard و report worker را پوشش می‌دهند. `tests/test_fixed_risk_cli.py` جداگانه resolve/validate مشترک `--fixed-risk`/`--choch-risk-cap` را روی هر دو parser واقعی (`run_pine_ob_paper.py` و `tools/run_tick_backtest.py`) و اثر واقعی آن (نرخ ریسک یکسان BOS/CHoCH بدون سقف) را از مسیر واقعی `PaperBroker` بدون mock می‌سنجد. دستور پذیرش:

```powershell
python -m unittest discover -s tests -q
python tools/update_docs_manifest.py --check
```

محدودیت‌ها:

- OHLC M5 ترتیب intrabar را نمی‌داند؛ سنجش دقیق live به tick capture همان بروکر نیاز دارد.
- عمق `copy_ticks_range` به history سرور بروکر وابسته است؛ `coverage.json` تعداد و محدوده هر روز را ثبت می‌کند.
- spread historical ثابت است مگر دیتای tick استفاده شود.
- mirror demo تضمین نمی‌کند paper و server fill یکسان باشند؛ slippage برای همین ثبت می‌شود.
- گزارش causal بودن strategy را تضمین نمی‌کند؛ هر feature جدید باید جداگانه از leakage تست شود.

طراحی performance: tick polling 1s، candle polling 5s، checkpoint/report 5m، breakdown 15m؛ گزارش در worker و snapshot آن shallow است. checkpoint state محدود شده و trade history در SQLite جداست. هر تغییر در این مسیر باید latency حلقه tick و حجم state را دوباره اندازه بگیرد.

## 14. قرارداد به‌روزرسانی این سند

`tools/update_docs_manifest.py` فایل‌های اصلی را به sectionهای همین سند map می‌کند. workflow اجباری هر تغییر:

1. کد را تغییر دهید.
2. توضیح همان کلاس/تابع، منطق، CLI یا خروجی را در این سند اصلاح کنید.
3. تست‌ها را اصلاح/اضافه کنید.
4. تأیید صریح ثبت کنید:

```powershell
python tools/update_docs_manifest.py --confirm-guide-updated
python -m unittest discover -s tests -q
```

اگر مرحله 2 یا 4 فراموش شود، `DocumentationSyncTests` نام فایل تغییرکرده و anchor موردنیاز را اعلام و suite را fail می‌کند. ابزار عمداً متن فنی را خودکار بازنویسی نمی‌کند؛ تولید خودکار توضیح می‌تواند سندی ظاهراً به‌روز ولی از نظر معنایی غلط بسازد. manifest اجبار به بازبینی انسانی را فراهم می‌کند.
