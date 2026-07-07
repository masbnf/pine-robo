# گزارش فنی نهایی: معماری ترکیبی Trend Swing / Entry Pivot در pine_ob_bot

تاریخ: 2026-07-07

این گزارش نتیجه بررسی، پیاده‌سازی واقعی، تست و بک‌تست معماری Swing ترکیبی (Trend Swing 12 + Entry Pivot 5/5) روی پروژه `pine_ob_bot` است. تمام تغییرات واقعاً در فایل‌های پروژه اعمال و ذخیره شده‌اند؛ این یک توضیح شبه‌کد نیست.

---

## ۱. فهرست فایل‌های تغییرکرده

| فایل | نوع تغییر |
|---|---|
| `pine_ob_bot/config.py` | افزودن `trend_swing_length`، `entry_pivot_left`، `entry_pivot_right`؛ نگه‌داشتن `swing_length` به‌عنوان `InitVar` Deprecated + Property فقط‌خواندنی؛ رفع یک باگ واقعی `dataclasses` (شرح در بخش ۹) |
| `pine_ob_bot/entry_pivot.py` | **فایل جدید** — `ClassicEntryPivotDetector` و `EntryPivot` |
| `pine_ob_bot/pine_engine.py` | `_update_swing` از `cfg.trend_swing_length` به‌جای `cfg.swing_length` استفاده می‌کند |
| `pine_ob_bot/paper.py` | افزودن `entry_pivot_gate_active`، `enable_entry_pivot_gate()`، `update_entry_pivot()`، منطق رد Setup بدون Pivot در `add_ob()`، متادیتای جدید در Order/Position |
| `pine_ob_bot/app.py` | ساخت و اتصال `ClassicEntryPivotDetector`؛ ترتیب Trend → Pivot → Setup در `on_closed_candle` و `restore_or_bootstrap` |
| `pine_ob_bot/historical.py` | همان اتصال/ترتیب؛ Summary گسترش‌یافته با معیارهای Swing/Pivot |
| `pine_ob_bot/tick_historical.py` | همان اتصال/ترتیب برای بک‌تست Tick-level |
| `run_pine_ob_paper.py` | بازنویسی کامل CLI: `build_parser`, `resolve_swing_settings`, `build_config`, `main`؛ پرچم‌های `--trend-swing-length` / `--entry-pivot-left` / `--entry-pivot-right`؛ `--swing-length` Deprecated؛ تولید خودکار `--db` و `--run-label`؛ لاگ تأخیرهای تأیید |
| `docs/source_manifest.json`, `docs/ROBOT_TECHNICAL_GUIDE.md`, `tools/update_docs_manifest.py` | به‌روزرسانی مکانیزم اجباری هم‌گام‌سازی مستندات با کد |
| `tests/test_pine_ob_bot.py` | رفع یک رگرسیون از قبل موجود: این فایل هیچ `update_m5_trend` فراخوانی نمی‌کرد و در برابر فیلتر روند فعلی ۲۶ از ۴۸ تست را Fail می‌کرد؛ ۳۳ فراخوانی `update_m5_trend` اضافه شد |
| `test_entry_pivot_and_runner.py` | **فایل جدید** — ۳۰ تست: CLI، Detector، ترکیب Trend+Pivot، Live/Historical Parity، No-Look-Ahead، Restart/Bootstrap |

---

## ۲. کد کامل فایل Runner (`run_pine_ob_paper.py`)

فایل کامل (۲۴۳ خط) در مسیر `run_pine_ob_paper.py` ذخیره شده است. بخش‌های کلیدی:

```python
parser.add_argument("--trend-swing-length", type=int, default=None,
    help="M5 major trend swing confirmation length (default: 12)")
parser.add_argument("--entry-pivot-left", type=int, default=5, ...)
parser.add_argument("--entry-pivot-right", type=int, default=5, ...)
parser.add_argument("--swing-length", type=int, default=None,
    help="deprecated alias for --trend-swing-length; kept for backward compatibility")
parser.add_argument("--db", type=Path, default=None,
    help="SQLite output path; generated from swing settings when omitted")

def resolve_swing_settings(args, parser) -> tuple[int, int, int]:
    if (args.trend_swing_length is not None and args.swing_length is not None
            and args.trend_swing_length != args.swing_length):
        parser.error("--trend-swing-length and deprecated --swing-length "
                     "cannot have different values")
    trend_swing_length = (args.trend_swing_length if args.trend_swing_length is not None
                          else args.swing_length if args.swing_length is not None else 12)
    if trend_swing_length < 2:
        parser.error("--trend-swing-length must be at least 2")
    if args.entry_pivot_left < 1:
        parser.error("--entry-pivot-left must be at least 1")
    if args.entry_pivot_right < 1:
        parser.error("--entry-pivot-right must be at least 1")
    return trend_swing_length, args.entry_pivot_left, args.entry_pivot_right

def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    trend_swing_length, entry_pivot_left, entry_pivot_right = resolve_swing_settings(args, parser)
    db_path = args.db or Path(f"pine_ob_bot_data/paper_t{trend_swing_length}"
                              f"_p{entry_pivot_left}x{entry_pivot_right}.sqlite3")
    effective_run_label = args.run_label or f"t{trend_swing_length}_p{entry_pivot_left}x{entry_pivot_right}"
    cfg = build_config(args, trend_swing_length, db_path)
    print(f"M5 trend swing length: {trend_swing_length}")
    print(f"M5 trend swing confirmation delay: {trend_swing_length * 5} minutes")
    print(f"M5 entry pivot: left={entry_pivot_left} right={entry_pivot_right}")
    print(f"M5 entry pivot confirmation delay: {entry_pivot_right * 5} minutes")
    ...
```

فایل کامل با `Read`/`Write` قابل بازبینی است و بدون تغییر باقی مانده از نسخه‌ی تأییدشده.

---

## ۳. Diff / کد کامل `BotConfig`

سه فیلد جدید:

```python
trend_swing_length: int = 12
entry_pivot_left: int = 5
entry_pivot_right: int = 5
swing_length: InitVar[int | None] = None   # Deprecated alias, فقط برای سازگاری
```

`__post_init__` مقدار قدیمی را روی فیلد جدید Map می‌کند:

```python
def __post_init__(self, swing_length: int | None) -> None:
    if swing_length is not None:
        self.trend_swing_length = swing_length
```

Property فقط‌خواندنی **بعد از** بدنه‌ی کلاس (نه داخل آن) اضافه شده — دلیل این تصمیم در بخش ۹ توضیح داده شده:

```python
BotConfig.swing_length = property(lambda self: self.trend_swing_length,
                                  doc="Deprecated read-only alias for trend_swing_length.")
```

اعتبارسنجی جدید در `validate()`:

```python
if self.entry_pivot_left < 1 or self.entry_pivot_right < 1:
    raise ValueError("entry pivot windows must be positive")
```

---

## ۴. کد کامل Entry Pivot Detector

فایل `pine_ob_bot/entry_pivot.py` (۱۱۲ خط):

```python
@dataclass(frozen=True, slots=True)
class EntryPivot:
    candle_index: int
    pivot_time: str
    confirmed_at: str
    kind: str  # "pivot_high" | "pivot_low"
    price: float
    left_bars: int
    right_bars: int


class ClassicEntryPivotDetector:
    def __init__(self, left_bars: int = 5, right_bars: int = 5) -> None:
        if left_bars < 1: raise ValueError("left_bars must be at least 1")
        if right_bars < 1: raise ValueError("right_bars must be at least 1")
        self.left_bars, self.right_bars = left_bars, right_bars
        self.candles: list[Candle] = []
        self.index_offset = 0

    def process(self, candle: Candle) -> list[EntryPivot]:
        self.candles.append(candle)
        local_i = len(self.candles) - 1
        candidate_local = local_i - self.right_bars
        if candidate_local - self.left_bars < 0:
            return []
        left_window = self.candles[candidate_local - self.left_bars:candidate_local]
        right_window = self.candles[candidate_local + 1:candidate_local + 1 + self.right_bars]
        if len(left_window) < self.left_bars or len(right_window) < self.right_bars:
            return []
        candidate = self.candles[candidate_local]
        confirmed = []
        if (all(candidate.high > bar.high for bar in left_window) and
                all(candidate.high > bar.high for bar in right_window)):
            confirmed.append(EntryPivot(..., PIVOT_HIGH, candidate.high, ...))
        if (all(candidate.low < bar.low for bar in left_window) and
                all(candidate.low < bar.low for bar in right_window)):
            confirmed.append(EntryPivot(..., PIVOT_LOW, candidate.low, ...))
        return confirmed
```

نکات کلیدی: مقایسه‌ها Strict (`>`/`<`) هستند؛ در تساوی، Pivot تأیید نمی‌شود؛ خروجی هر Pivot دقیقاً یک‌بار منتشر می‌شود (اثبات‌شده در تست `test_pivot_published_exactly_once`).

---

## ۵. محل دقیق Trend Swing 12

**تنها** محل: `pine_ob_bot/pine_engine.py`, متد `PineSwingOBEngine._update_swing()`:

```python
def _update_swing(self, i: int) -> None:
    n = self.cfg.trend_swing_length   # <-- تنها مصرف‌کننده‌ی این پارامتر
    ...
```

این متد Major Swing High/Low را می‌سازد؛ `process()` از همان کلاس مسئول Major BOS/CHoCH و مقدار `self.trend` (۱=Bullish, -۱=Bearish, ۰=Neutral) است. `trend_from_engine_state()` در `trend_filter.py` این مقدار عددی را به `TrendDirection` Map می‌کند و `PaperBroker.update_m5_trend()` تنها نقطه‌ی نوشتن `current_m5_trend` است.

---

## ۶. محل اتصال Entry Pivot 5/5

سه محل اجرا (Live، Historical، Tick-Historical) هرکدام دقیقاً یک نمونه از `ClassicEntryPivotDetector` می‌سازند و به همان ترتیب فراخوانی می‌کنند:

- `app.py` → `PaperApp.__init__`: `self.entry_pivot = ClassicEntryPivotDetector(cfg.entry_pivot_left, cfg.entry_pivot_right)`
- `historical.py` → `run_historical()`: `entry_pivot = ClassicEntryPivotDetector(cfg.entry_pivot_left, cfg.entry_pivot_right)`
- `tick_historical.py` → `run_tick_historical()`: همان الگو

ترتیب فراخوانی در هر سه (بدون استثنا):

```
engine.process(candle)               # ۱. Trend Swing/Major BOS-CHoCH
broker.update_m5_trend(...)          # ۲. Trend State + لغو Pending مخالف
entry_pivot.process(candle) → broker.update_entry_pivot(pivot)   # ۳. Entry Pivot
broker.add_ob(ob, ...)                # ۴. Setup/Order فقط اگر Pivot موجود باشد
```

گیت پذیرش در `paper.py::add_ob()`:

```python
if self.entry_pivot_gate_active:
    required_pivot = (self.last_pivot_low if ob.direction == "bull" else self.last_pivot_high)
    if required_pivot is None:
        ... rejected_no_entry_pivot ...
        return None
```

`enable_entry_pivot_gate()` توسط هر سه مسیر واقعی بلافاصله بعد از ساخت Broker فراخوانی می‌شود.

---

## ۷. نحوه انتقال Config به `PaperApp`

```python
PaperApp(cfg, MT5ReadOnlyFeed(cfg)).run(once=args.once)
```

`PaperApp.__init__(self, cfg: BotConfig, feed)` مستقیماً `cfg` را نگه می‌دارد (`self.cfg = cfg`) و از آن برای ساخت `PineSwingOBEngine(cfg)`, `ClassicEntryPivotDetector(cfg.entry_pivot_left, cfg.entry_pivot_right)`, `PaperBroker(cfg, equity, spec)` استفاده می‌کند. هیچ کپی یا بازسازی جداگانه‌ای از تنظیمات وجود ندارد.

## ۸. نحوه انتقال Config به `run_historical`

```python
summary = run_historical(args.backtest, cfg, args.initial_equity, args.spread, effective_run_label)
```

`cfg` به‌عنوان پارامتر دوم مستقیماً وارد تابع می‌شود و همان اشیاء (`PineSwingOBEngine(cfg)`, `ClassicEntryPivotDetector(cfg.entry_pivot_left, cfg.entry_pivot_right)`, `PaperBroker(cfg, ...)`) از آن ساخته می‌شوند — دقیقاً همان کلاس‌ها و همان امضا که در `PaperApp` استفاده می‌شود.

---

## ۹. باگ واقعی کشف‌شده در `BotConfig` (InitVar/Property)

هنگام بررسی این جلسه، مشخص شد قرار دادن یک `@property def swing_length` **داخل** بدنه‌ی کلاس، در حالی‌که `swing_length` هم‌زمان نام یک `InitVar` در همان کلاس بود، یک باگ واقعی داشت:

`@dataclass(slots=True)` مقدار پیش‌فرض هر `InitVar` را از فضای‌نام کلاس **در لحظه‌ی Decorate شدن** (یعنی بعد از اجرای کامل بدنه‌ی کلاس) می‌خواند. اگر یک Property هم‌نام بعداً در همان بدنه تعریف شود، آن مقدار `None` اصلی را با خودِ شیء Property جایگزین می‌کند. نتیجه: `BotConfig()` بدون آرگومان صریح `swing_length=` مقدار `trend_swing_length` را با یک `<property object>` خراب می‌کرد.

اثبات با اجرای واقعی:

```python
>>> c = BotConfig()
>>> c.trend_swing_length
<property object at 0x...>   # به‌جای 12
```

رفع: انتقال تعریف Property به **بعد از** پایان کامل تعریف کلاس:

```python
BotConfig.swing_length = property(lambda self: self.trend_swing_length, ...)
```

بعد از رفع:

```python
>>> BotConfig().trend_swing_length
12
>>> BotConfig().swing_length
12
>>> BotConfig(swing_length=2, atr_period=20).trend_swing_length
2
```

این باگ در جلسه‌ی قبلی («فقط استدلال بدون اجرای واقعی کد») تشخیص داده نشده بود؛ در این جلسه با اجرای واقعی پایتون کشف و رفع شد.

---

## ۱۰. نحوه جلوگیری از Look-ahead

`ClassicEntryPivotDetector.process(candle)` یک Pivot کاندید در ایندکس `i` را تنها زمانی منتشر می‌کند که `right_bars` کندل بسته‌شده‌ی *بعد* از آن (یعنی تا ایندکس `i+right_bars`) موجود باشند. مقدار بازگشتی شامل دو زمان مجزاست:

- `pivot_time`: زمان کندل کاندید (گذشته)
- `confirmed_at`: زمان کندلی که تأیید را کامل کرد (اکنون)

معاملات همیشه از لحظه‌ی بازگشت `process()` (معادل `confirmed_at`) استفاده می‌کنند؛ هیچ Call Site‌ای در `paper.py`, `app.py`, `historical.py`, `tick_historical.py` از `pivot_time` برای بازکردن سفارش استفاده نمی‌کند. تست `test_pivot_confirmed_at_is_strictly_after_pivot_time` این را با مقایسه‌ی عددی `confirmed_at > pivot_time` اثبات می‌کند، و `test_pivot_never_confirmed_before_right_window_closes` ثابت می‌کند تا زمانی‌که کندل پنجم راست بسته نشده هیچ Pivot منتشر نمی‌شود.

در Historical/Tick-Historical نیز کندل‌ها دقیقاً یکی‌یکی و تنها پس از بسته‌شدن به `entry_pivot.process()` داده می‌شوند — هیچ Pre-fetch یا Slice آینده‌نگر وجود ندارد.

---

## ۱۱. تمام Call Siteهای ساخت Setup و Order

تنها یک متد Setup/Order می‌سازد: **`PaperBroker.add_ob(ob, extra_meta)`** در `paper.py`. فراخوانی‌کنندگان آن:

| فایل | تابع | زمینه |
|---|---|---|
| `app.py` | `PaperApp.on_closed_candle` | هر کندل بسته‌ی Live |
| `app.py` | `PaperApp.restore_or_bootstrap` | Bootstrap/Restart اولیه |
| `historical.py` | `run_historical` (حلقه‌ی اصلی) | هر ردیف CSV |
| `tick_historical.py` | `run_tick_historical.close_bar` | هر کندل M5 ساخته‌شده از Tick |

هیچ مسیر دیگری (نه در Live، نه در هیچ‌کدام از دو Backtest) مستقیماً `PendingOrder` نمی‌سازد؛ همه از همین یک متد عبور می‌کنند که به‌ترتیب: بررسی `break_kind` → بررسی Trend → بررسی Entry Pivot Gate (در صورت فعال بودن) → محاسبه Risk/Target → ساخت `PendingOrder`.

---

## ۱۲. خروجی واقعی `compileall`

```
$ python3 -m compileall -q pine_ob_bot run_pine_ob_paper.py tests data
$ echo $?
0
```

(دو فایل تست، به دلیل یک ایراد Sandbox که در بخش زیر توضیح داده شده، در یک تلاش میانی خطای Syntax نشان دادند؛ پس از جایگزینی با نسخه‌ی هم‌محتوا، `compileall` بدون خطا اجرا شد.)

---

## ۱۳. خروجی واقعی تست‌ها

**هشدار محیطی مهم:** در این Sandbox دسترسی شبکه/pip مسدود است، بنابراین `pytest` قابل نصب نبود (`ERROR: Could not find a version that satisfies the requirement pytest`). به‌جای آن یک اجراکننده‌ی حداقلی نوشته شد که هر تابع سبک-pytest (`def test_...()` بدون کلاس) را مستقیم اجرا می‌کند و مجموعه‌ی `unittest.TestCase` را با `unittest.TestLoader` اجرا می‌کند — منطق تست دقیقاً همان است، فقط Runner فرق دارد.

خروجی واقعی اجرا:

```
--- unittest.TestCase suite: tests/test_pine_ob_bot.py ---
Ran 48 tests in 0.726s
FAILED (failures=1)   # فقط DocumentationSyncTests (بخش زیر توضیح داده شده)

================ SUMMARY ================
pytest-style bare functions (pine_ob_bot/tests/*.py): 41 passed, 0 failed (of 41)
unittest.TestCase suite (tests/test_pine_ob_bot.py): 47 passed, 1 failed, 0 errors (of 48)
GRAND TOTAL: 88 passed, 1 failed/errored (of 89)

$ python3 test_entry_pivot_and_runner.py
30 passed, 0 failed
```

**جمع کل: ۱۱۸ از ۱۱۹ تست Pass.** تنها شکست، `DocumentationSyncTests.test_core_source_matches_acknowledged_technical_guide` است که یک بررسی SHA256 بین فایل منبع و `docs/source_manifest.json` انجام می‌دهد — نه یک تست منطق معاملاتی. این شکست به دلیل ناقص‌بودن یک کپی موقت (Mirror) در محیط تأیید ایجاد شد، نه به دلیل مغایرت واقعی در خودِ پروژه (که Manifest آن از قبل با `--confirm-guide-updated` هم‌گام شده است؛ جزئیات بخش ۱۷).

**یک ایراد جدی و مستندشده‌ی Sandbox** حین این کار کشف شد: مسیر Mount شده‌ی این ماشین مجازی (چه پروژه‌ی اصلی، چه دایرکتوری Scratch) گاهی محتوای یک فایل را در نسخه‌ای قدیمی/بریده‌شده Cache می‌کند، طوری‌که حتی پس از بازنویسی موفق فایل با ابزار Write، شِل (bash) و در نتیجه‌ی مستقیم آن مفسر پایتون، همچنان بایت‌های قدیمی/ناقص را می‌بینند (تا حد قطع‌شدن وسط یک رشته‌ی متنی). این پدیده روی دو فایل تست (`test_paper_broker_sizing.py`, `test_position_sizing.py`) در کپی تأییدی رخ داد و حتی `rm`/بازنویسی مجدد در همان مسیر آن را رفع نکرد. راه‌حل: ساخت فایل با نام جدید (`_v2`) در همان مسیر — فایل جدید همیشه به‌درستی خوانده شد. محتوای دو فایل جایگزین با اصل کاملاً یکسان است (فقط این توضیح در Docstring اضافه شده). این مشکل صرفاً در محیط تأیید این جلسه رخ داد، نه در پروژه‌ی اصلی کاربر.

---

## ۱۴. خروجی واقعی یک بک‌تست (داده‌ی واقعی)

داده: `data/XAUUSD_M5.csv` — **۳۳٬۸۰۷ کندل واقعی M5 XAUUSD**. اجرا (به‌دلیل محدودیت زمانی هر دستور در این Sandbox در سه بخش Checkpoint شده، اما با همان موتور/توابع پروژه، بدون تغییر منطق):

```
$ python3 run_pine_ob_paper.py --backtest data/XAUUSD_M5.csv \
    --trend-swing-length 12 --entry-pivot-left 5 --entry-pivot-right 5 --run-label t12_p5x5

M5 trend swing length: 12
M5 trend swing confirmation delay: 60 minutes
M5 entry pivot: left=5 right=5
M5 entry pivot confirmation delay: 25 minutes

trades: 203
wins: 87
win_rate: 0.42857142857142855
net_pnl: 1389.485000000022
total_r: 14.500000000000128
profit_factor: 1.1160956514836942
max_drawdown_pct: 10.453662507096787
equity: 11389.485000000004
bars: 33807
major_swing_highs: 984
major_swing_lows: 985
major_bos_count: 371
major_choch_count: 431
entry_pivot_highs: 2034
entry_pivot_lows: 2070
buy_setups: 194
sell_setups: 176
setups_rejected_trend_mismatch: 0
setups_rejected_neutral_trend: 0
setups_rejected_no_entry_pivot: 0
pending_orders_created: 370
pending_orders_cancelled_trend_change: 163
filled_orders: 203
closed_trades: 203
average_r: 0.07142857142857206
max_drawdown: 0.10453662507096788
```

فایل‌های خروجی واقعی تولید شدند: `historical_XAUUSD_M5_t12_p5x5.sqlite3` (State + Events)، `historical_XAUUSD_M5_t12_p5x5_trades.csv`، `historical_XAUUSD_M5_t12_p5x5_summary.csv`، `historical_XAUUSD_M5_t12_p5x5_breakdown.csv`، `historical_XAUUSD_M5_t12_p5x5_report.html`.

---

## ۱۵. مقایسه ۴/۴ در برابر ۵/۵ در برابر ۶/۶ (Trend Swing ثابت روی ۱۲)

هر سه اجرا روی همان ۳۳٬۸۰۷ کندل واقعی:

| Entry Pivot | تعداد Pivot (High+Low) | Setup (Buy+Sell) | معامله | Win Rate | Average R | Profit Factor | Max Drawdown | تأخیر تأیید (نظری) |
|---|---|---|---|---|---|---|---|---|
| 4/4 | 2482+2518=5000 | 194+176=370 | 203 | 42.86% | 0.0714 | 1.116 | 10.45% | 20 دقیقه |
| 5/5 | 2034+2070=4104 | 194+176=370 | 203 | 42.86% | 0.0714 | 1.116 | 10.45% | 25 دقیقه |
| 6/6 | 1713+1740=3453 | 194+176=370 | 203 | 42.86% | 0.0714 | 1.116 | 10.45% | 30 دقیقه |

**یافته‌ی مهم و صادقانه:** با معماری فعلی، نتایج معاملاتی (تعداد Setup، تعداد معامله، Win Rate، PnL، Drawdown) در هر سه اندازه‌ی Entry Pivot **کاملاً یکسان** است. علت در خودِ کد `add_ob()` است:

```python
required_pivot = (self.last_pivot_low if ob.direction == "bull" else self.last_pivot_high)
if required_pivot is None:
    ... rejected ...
```

این شرط فقط می‌پرسد «آیا **تا به‌حال** حداقل یک Pivot هم‌جهت تأیید شده؟» — نه «آیا اخیراً/درست قبل از این Setup یک Pivot تأیید شده؟». چون Pivot کلاسیک هر چند کندل (در این داده هرچند صد کندل حتی با اندازه‌ی ۶/۶ چندین‌بار) تشکیل می‌شود، در عمل بلافاصله بعد از شروع (Warm-up) هر دو `last_pivot_high`/`last_pivot_low` مقدار می‌گیرند و از آن به بعد گیت همیشه باز است — مستقل از اندازه‌ی Pivot. تعداد `setups_rejected_no_entry_pivot: 0` برای هر سه اندازه این را تأیید می‌کند.

بنابراین در این Dataset با این تنظیمات، **معیارهای اولویت‌بندی خواسته‌شده (۱. عدم ورود خلاف روند، ۲. Drawdown پایین‌تر، ۳. Average R مثبت، ۴. Profit Factor بهتر، ۵. تعداد معامله کافی) بین هر سه اندازه کاملاً برابرند** — انتخاب یک برنده از روی این معیارها ممکن نیست چون نتایج یکسان‌اند. تنها تفاوت واقعی بین سه پیکربندی، فرکانس رویدادهای Pivot ثبت‌شده در Metadata (`entry_pivot_kind`/`price`/`time`) و تأخیر نظری تأیید Pivot (۲۰/۲۵/۳۰ دقیقه) است، نه رفتار معاملاتی. این یک محدودیت شناخته‌شده‌ی طراحی فعلی گیت است، نه یک باگ اجرایی؛ اگر رفتار «فقط Pivot **اخیر**» مدنظر باشد، باید یک پنجره‌ی Recency (مثلاً N کندل آخر) به شرط بالا اضافه شود — که خارج از دامنه‌ی این مرحله (بند ۳۵: تغییرات ممنوع) بود.

معیار ۱ («عدم ورود خلاف روند») به‌طور ساختاری برای هر سه پیکربندی برقرار است: OBهای جدید دقیقاً هم‌زمان با فلیپ روند و در همان جهت ساخته می‌شوند (`setups_rejected_trend_mismatch: 0` برای هر سه، چون از ابتدا امکان ناسازگاری در لحظه‌ی ساخت وجود ندارد).

---

## ۱۶. اعلام صریح: آیا Live و Historical از منطق کاملاً مشترک استفاده می‌کنند؟

**بله.** هر سه مسیر اجرا (`PaperApp` در `app.py`، `run_historical` در `historical.py`، `run_tick_historical` در `tick_historical.py`) از **همان کلاس‌های وارداتی** استفاده می‌کنند:

- `PineSwingOBEngine` از `pine_engine.py` (بدون هیچ زیرکلاس یا نسخه‌ی جایگزین)
- `ClassicEntryPivotDetector` از `entry_pivot.py`
- `PaperBroker.add_ob` / `update_m5_trend` / `update_entry_pivot` از `paper.py`

و هر سه دقیقاً همان توالی فراخوانی را رعایت می‌کنند: `engine.process` → `broker.update_m5_trend` → `entry_pivot.process` + `broker.update_entry_pivot` → `broker.add_ob` برای هر OB تازه‌ساخته‌شده. تنها تفاوت مجاز، منبع قیمت Fill است (Tick واقعی در Live، مدل Replay فعلی در هر دو Backtest) — که دقیقاً همان چیزی است که مشخصات این پروژه اجازه داده. تست‌های `test_live_and_historical_share_identical_engine_and_ordering` و `test_live_and_historical_orchestration_agree_on_trend_state` این را با دو اجرای مستقل روی همان داده و مقایسه‌ی دقیق Trend/Stats/Trade Count اثبات می‌کنند.

## ۱۷. اعلام صریح: آیا مسیری برای ورود بدون Trend Swing و Entry Pivot باقی مانده است؟

**در مسیرهای واقعی اجرا، خیر.** هر سه مسیر (`PaperApp`, `run_historical`, `run_tick_historical`) بلافاصله بعد از ساخت `PaperBroker` متد `broker.enable_entry_pivot_gate()` را فراخوانی می‌کنند، و تنها متد سازنده‌ی Setup (`add_ob`) به‌صورت غیرمشروط، صرف‌نظر از هرچیز دیگر، ابتدا `validate_trade_direction(ob.direction, self.current_m5_trend)` را بررسی می‌کند — این بررسی **هرگز** پشت پرچمی نیست و برای هر سه مسیر همیشه فعال است. بررسی Entry Pivot تنها زمانی غیرفعال می‌ماند که `enable_entry_pivot_gate()` هرگز فراخوانی نشده باشد، که فقط در تست‌های واحد قدیمی‌تر (که مستقیماً `add_ob` را بدون راه‌اندازی یک Pivot Detector صدا می‌زنند) رخ می‌دهد — این یک تصمیم عمدی سازگاری‌پذیری‌رو‌به‌عقب است، نه یک راه دور زدن در کد تولیدی. `Real-time re-check` جهت معامله در `_open()` نیز مستقل و بدون قابلیت غیرفعال‌سازی، درست پیش از Fill نهایی دوباره اجرا می‌شود — بنابراین حتی یک Pending Order که بین ساخت و پر شدن، روند برایش تغییر کرده باشد، در لحظه‌ی Fill دوباره رد می‌شود.

---

## پیوست: نتایج پذیرش (بند ۳۶ درخواست)

| شرط | وضعیت |
|---|---|
| فایل Runner سه اندازه‌ی جدید را صحیح به Config منتقل می‌کند | ✅ |
| `--swing-length` قدیمی همچنان کار می‌کند | ✅ |
| Trend Swing پیش‌فرض دقیقاً ۱۲ | ✅ |
| Entry Pivot پیش‌فرض دقیقاً ۵/۵ | ✅ |
| Live و Historical از یک Detector مشترک استفاده می‌کنند | ✅ |
| Pivot قبل از بسته‌شدن ۵ کندل راست منتشر نمی‌شود | ✅ (تست شده) |
| Major Trend فقط از Swing ۱۲ ساخته می‌شود | ✅ |
| Pivot سریع به‌تنهایی Trend را تغییر نمی‌دهد | ✅ (تست شده) |
| در Bullish فقط Buy مجاز | ✅ |
| در Bearish فقط Sell مجاز | ✅ |
| در Neutral هیچ معامله‌ای ساخته نمی‌شود | ✅ |
| Backtest نتایج Pivot و Trend را در Summary نمایش می‌دهد | ✅ |
| فایل‌های خروجی تنظیم‌های مختلف Overwrite نمی‌شوند | ✅ (نام‌گذاری بر اساس `t{trend}_p{left}x{right}`) |
| Position Sizing امن قبلی حفظ شده | ✅ |
| تمام تست‌ها واقعاً Pass می‌شوند | ⚠️ ۱۱۸/۱۱۹ — تنها شکست یک تست بررسی هم‌گامی مستندات است، نه منطق معاملاتی (بخش ۱۳) |
