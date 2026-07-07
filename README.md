# SMC / MTF XAUUSD Bot

راهنمای جامع معماری، منطق، اجرای معامله و توضیح بلوک‌به‌بلوک کد در
[`docs/ROBOT_TECHNICAL_GUIDE.md`](docs/ROBOT_TECHNICAL_GUIDE.md) نگه‌داری می‌شود.
تغییر فایل‌های اصلی بدون تأیید به‌روزرسانی این راهنما، تست documentation sync را fail می‌کند.

دریافت و بک‌تست تیک تاریخی ماهانه:

```powershell
python tools/fetch_mt5_ticks.py --month 2026-06
python tools/run_tick_backtest.py --month 2026-06
```

## Standalone Pine Swing-OB paper trader

`run_pine_ob_paper.py` is a separate, read-only MT5 forward tester derived from
the supplied LuxAlgo SMC Pine indicator (CC BY-NC-SA 4.0). It never calls
`order_send`: closed M5 candles create Swing OB limit orders and live Bid/Ask
ticks fill and close them in an internal SQLite-backed paper account.

```bash
python run_pine_ob_paper.py          # keep running
python run_pine_ob_paper.py --once   # restore/catch up/export and exit
python run_pine_ob_paper.py --backtest data/XAUUSD_M5.csv
python run_pine_ob_paper.py --backtest data/XAUUSD_M5.csv --rr 2.5 --run-label rr25
```

For a non-overlapping validation set exported directly from the configured MT5:

```bash
python tools/fetch_mt5_data.py --from 2025-06-01 --to 2025-12-21 --out data/XAUUSD_M5_mt5_oos_2025.csv
python run_pine_ob_paper.py --backtest data/XAUUSD_M5_mt5_oos_2025.csv --run-label locked_validation
```

Credentials come from `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` and optionally
`MT5_TERMINAL_PATH`; otherwise the existing `config.settings.MT5` is reused.
Results are written under `pine_ob_bot_data/`.

The bot also builds a closed-candle M15 context stream (trend, last structure
break, swing range location, ATR and active M15 OB). These values are attached
to every M5 setup for CSV/HTML breakdown only; M15 does not filter entries.

Live paper entries accept M5 Swing BOS order blocks only. CHoCH remains
available for controlled comparison with `--include-choch`; no time-of-day
filter is applied. The production paper target is 1.5R.

An event-driven OB lifecycle (post-BOS extension, then retracement, then arming)
is implemented for research via `--lifecycle`. It is off by default because its
first historical comparison reduced PF and increased drawdown.

M5 and M15 swing-liquidity pools are tracked observationally as active, touched,
swept, or broken. Their distance, event age, and sweep depth are attached to
setups/fills and shown in CSV/HTML reports; liquidity does not gate entries.
An additional setup-related flag only counts an opposite-side M5 sweep occurring
inside the confirmed swing leg that terminates at the current BOS, avoiding an
arbitrary lookback window.

Live tick fills use real Bid/Ask. Any closed-candle fallback uses the configured
`fallback_spread` (0.20 by default), and every trade records its entry/exit
execution source and spread for tick-vs-replay auditing.

A modular, backtest-first Smart-Money-Concepts bot for XAUUSD M5/M15 scalping.
Built around one rule: **everything you tune lives in `config/settings.py`** — the
rest of the code is structured so you can grow it without rewrites.

## Quick start

```bash
pip install -r requirements.txt
python tools/make_sample_data.py     # writes data/XAUUSD_M5.csv (synthetic test data)
python run_backtest.py               # runs the backtest, writes trades.csv
```

Replace `data/XAUUSD_M5.csv` with your own M5 OHLC export (columns:
`time, open, high, low, close`) and re-run. M15 is resampled automatically.

## What you edit — `config/settings.py`

Grouped, commented dicts. The important ones:

| Group       | What it controls |
|-------------|------------------|
| `LOOKBACK`  | Scan windows per element (BOS 60, OB 15, liquidity 50, CHoCH 25, …) |
| `FRACTALS`  | Swing pivot sizes (M15 2/2, M5 2/2) |
| `ATR`       | ATR period + SL cap multiplier |
| `RISK`      | % risk per trade, min R:R, lot cap, TP mode |
| `FILTERS`   | Toggle FVG overlap, liquidity-sweep requirement, extension filter |
| `BACKTEST`  | Data file, spread, commission, warmup |

Change a number, save, re-run. No other file needs touching for tuning.

## Structure — built for future development

```
new-smc-bot/
├── config/
│   └── settings.py        <-- the ONE file you tune
├── smc/                   <-- self-contained detectors (swap/extend any one)
│   ├── types.py           shared dataclasses (Swing, BOS, OB, Zone, FVG, Signal…)
│   ├── structure.py       swings, BOS, CHoCH
│   ├── order_blocks.py    order-block detection
│   ├── zones.py           supply/demand zones
│   ├── liquidity.py       BSL/SSL pools + sweep detection + TP targets
│   ├── fvg.py             fair-value-gap detection + fill tracking
│   └── indicators.py      ATR (add more here)
├── data/
│   └── loader.py          CSV load + M5→M15 resample
├── strategy/
│   └── mtf_strategy.py    the 6-step BUY/SELL sequence (wires SMC + config)
├── backtest/
│   └── engine.py          rolling-buffer bar loop, trade sim, reporting
├── tools/
│   └── make_sample_data.py
├── run_backtest.py        entry point
└── requirements.txt
```

## The MTF logic (mirrors the spec)

**BUY** — M15 bullish BOS (not extended) → SSL swept → price taps demand OB
overlapping an unfilled bull FVG → M5 bullish CHoCH inside the zone → enter on the
freshest M5 FVG/OB → SL below the immediate M5 swing (ATR-capped), TP at next M15 BSL.
**SELL** is the exact mirror.

## Performance note

The backtest uses **fixed-size rolling buffers** (`iloc[-N:]` each bar), never
growing slices from index 0 — so total cost stays O(n), avoiding the O(n²) trap.

## Roadmap hooks (where to extend)

- **Live execution / MT5**: add `execution/mt5_broker.py`; the `Signal` dataclass
  is already broker-agnostic.
- **New detectors**: drop a module in `smc/`, import it in the strategy.
- **More indicators**: add to `smc/indicators.py`.
- **Optimization**: `Backtester.run()` returns a metrics dict — wrap it in a loop
  over `config` values for parameter sweeps.

> Educational tool. Backtested edge does not guarantee live results — forward-test
> on a demo account before risking capital.
# Live paper verification profile

The default runner profile is paper-only and never sends an MT5 order:

- XAUUSD M5, Swing 12
- BOS + CHoCH
- RR 1.5
- CHoCH + displacement risk sizing
- CHoCH risk capped at 0.5%
- one simultaneous paper position

Start the forward test after MT5 is connected:

```powershell
python run_pine_ob_paper.py
```

To mirror new tick-confirmed paper positions into the connected MT5 demo
account, use the explicit guarded flag:

```powershell
python run_pine_ob_paper.py --demo-orders
```

The process refuses to start order mirroring unless MT5 reports a demo account.
The standard HTML dashboard is refreshed every five minutes and archived by
Tehran calendar day under `pine_ob_bot_data/daily_reports/`. The dated files are
kept, while `paper_report_latest.html` always points to the current day.
The same folder also contains continuously refreshed cumulative HTML, trade,
summary, breakdown, and one-row-per-day metric files for later forward-test
comparison.
While the bot is running, open `http://127.0.0.1:8765/` to view the dashboard.
The pages refresh automatically. Bind to another host only on a trusted network.

State is stored in `pine_ob_bot_data/paper_swing12.sqlite3`. Live Bid/Ask ticks
are appended to daily files under `pine_ob_bot_data/market_capture/`, and each
new closed M5 candle is appended to `candles_XAUUSD_M5.csv`. Use a separate DB
for a separate experiment. Add `--no-market-capture` only when capture is not
required.
