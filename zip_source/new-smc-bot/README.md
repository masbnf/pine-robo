# Clean SMC XAUUSD Backtester

A modular, backtest-first implementation of `SMC_clean_logic.pine` for XAUUSD.
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
│   └── mtf_strategy.py    confirmed break → OB limit → fixed-RR execution
├── backtest/
│   └── engine.py          rolling-buffer bar loop, trade sim, reporting
├── tools/
│   └── make_sample_data.py
├── run_backtest.py        entry point
└── requirements.txt
```

## Executable Clean-SMC rules

The Pine source is an indicator, so its displayed structure is made explicit for
backtesting: a confirmed swing BOS creates a LuxAlgo-style order block; price
must retest that block and then print a same-direction internal CHoCH. The engine
enters at market after that confirmed close, sets SL at the opposite OB edge,
and targets `CLEAN_SMC["rr_ratio"]` (2R by default).
Pivots and HTF candles become visible only after confirmation/close, preventing
future leakage. Settings live under `CLEAN_SMC` in `config/settings.py`.

### H1/M15 warm-up

`smc/warmup.py` consumes each closed M15 candle once. It maintains the H1
HH/HL or LH/LL trend, block-partitioned M15 liquidity, multi-sweep zone life,
M15 block OBs and range-break CHoCH levels. A clean-SMC setup may arm when the
H1 trend agrees; OB/zone overlap is optional and disabled by default. Entry follows
the existing BOS -> retest -> internal CHoCH sequence; SL and TP use the nearest
same-side/opposite-side liquidity. Fixed-R:R execution is disabled. The run log
contains a shadow R-target table (`0.5R` through `4R`) that continues observing
price after the real liquidity exit without changing PnL.

Sweeps are episode-based: continuous multi-candle penetration counts once.
Normal zones survive 2 episodes, touched zones 3, and zones near an H1 extreme
4. These values are configurable under `WARMUP`.

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
