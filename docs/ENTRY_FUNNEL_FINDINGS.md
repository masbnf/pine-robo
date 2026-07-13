# Entry-funnel findings — sweep_reclaim, XAUUSD M5 (2026-03..06)

Status: analysis-only. No strategy change was made based on sections 2-3;
this document records WHY, so the same dead ends are not re-explored.

Baseline throughout: swing 9 / pivot 3x3 / buffer 0.20 / rr 1.5 / fixed risk.

## 1. CHoCH order blocks were a silent default and lost money (FIXED)

`run_tick_backtest.py` and `run_pine_ob_paper.py` traded `("BOS", "CHoCH")`
unless `--bos-only` was passed, contradicting their own `--include-choch`
help text and the `BotConfig` default. Across 2026-03..06 (mixed runs,
186 trades): BOS 94 trades +25.5R (50% WR) vs CHoCH 92 trades -9.3R
(36% WR); CHoCH was negative in every single month. The worst slice was
CHoCH counter to the M15 trend: -10.8R over 56 trades.

Fixed in commit 46cb5a7: BOS-only is now the real default on both runners.
Confirmed BOS-only rerun: 103 trades, +21.5R actual / +19.5R clean,
expectancy per trade 0.087 -> 0.209R, every month positive on clean R.

## 2. Multi-bar reclaim window: implemented, measured, INERT

Hypothesis: the 123-swept -> 45-confirmed funnel loss comes from reclaims
that arrive 1-2 bars after the sweep, discarded by the same-bar rule.

Implementation (commit 9d222e6): `sweep_reclaim_max_bars` (default 1 =
bit-identical old rule) + lag0/lag1/lag2plus/window_expired telemetry.

Result: user reran 2026-03 and 2026-05 — outputs bit-identical to the
N=1 baselines; every filled trade has `sweep_reclaim_lag_bars = 0`.

Why it HAD to be inert (adjacency argument): on continuous M5 prices the
bar that finally closes back over the edge opens below it (open ~= prior
close), so its own low sweeps the edge too and the SAME-BAR rule already
fires. N>1 only adds inter-bar gap cases, which XAUUSD M5 essentially
never produces. Unconfirmed sweeps are sweeps that NEVER close back over
the edge, not late reclaims. Keep the flag for other markets/timeframes.

## 3. Reviving trend-cancelled setups: simulated, NOT worth it

Hypothesis: 62-66% of setups die to `cancelled_trend_change`; if the M5
trend flips back while the OB is still alive, re-arming them should add
many trades.

Method (`tools/analyze_trend_revival.py`, candle-level estimate): replay
the month with the exact live ordering (process_signal_candle -> engine ->
update_m5_trend -> entry pivot -> add_ob -> cancel_ob), collect every
trend-cancelled BOS order from `broker.trend_events`, then simulate:
first re-alignment window while the OB is uninvalidated, same-bar
sweep+reclaim inside aligned windows only, fill at close(+0.2 spread for
longs), stop = OB edge -/+ 0.20 ATR, fixed 1.5R target, SL-first walk.

Results:

| month | trend-cancelled (BOS) | OB died before realign | got revival window | confirmed fills | outcome |
|-------|----------------------|------------------------|--------------------|-----------------|---------|
| 2026-06 | 71 | 34 | 37 | 3 | 2W/1L, +2.0R |
| 2026-03 | 53 | 34 | 19 | 1 | 0W/1L, -1.0R |

Net: +1.0R over two months for ~2 extra trades/month. ~48-64% of the
cancelled pool dies with its OB before the trend ever realigns, and even
inside a revival window the sweep+reclaim trigger fires at its usual low
base rate. The "62% cancelled" pool is NOT a reservoir of missed trades.

Caveats: candle-level fills (no tick precision), entry-pivot gate treated
as always-open (it empirically always is), max-positions ignored, chunked
M5 reconstruction drops ~6 boundary candles per month.

## 4. Where the trade count actually is

BOS + in-trend + swept + same-bar reclaimed is simply rare (~1 fill/day).
Remaining levers that do NOT relax entry quality: more symbols, a second
timeframe, or a hybrid second entry-mode (limit at edge) A/B'd against
sweep_reclaim. Levers already validated but not yet run to completion:
`--breakeven-at-r 1.0` (MFE data suggested up to +18R/4mo on the mixed
runs) and session filtering (Asia 02-07 UTC was -14.5R on mixed data;
recheck on BOS-only before acting).
