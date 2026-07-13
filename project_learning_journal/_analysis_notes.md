# _analysis_notes.md — pine_ob_bot ground-truth analysis

Internal working file for the Persian educational documentation project. English,
dense, code-grounded. Every claim below is checked against the actual source
read in full (see file list). Anything not directly observable in code is
marked **AMBIGUOUS**, **TECH DEBT**, **NEEDS REVIEW**, or **EXPERIMENTAL**.

Cross-check note: `docs/ROBOT_TECHNICAL_GUIDE.md` (Persian, pre-existing) cites
line ranges (`paper.py` "1–857", `app.py` "1–503") that are **stale** relative
to the current code (`paper.py` = 1209 lines, `app.py` = 535 lines, confirmed
by direct read and matching `project_learning_journal/PLAN.md`'s own line
counts). Treat that guide as directionally correct but not authoritative on
line numbers or exact current behavior; this document is grounded in the
actual current source.

Package scope confirmed: `pine_ob_bot/` + root `run_pine_ob_paper.py`. The
root-level legacy bot (`config/settings.py`, `backtest/engine.py`,
`data/loader.py`, `smc/`, `strategy/`) is a **separate, unrelated project** and
is NOT analyzed here, except where `pine_ob_bot` code itself imports from it
(flagged explicitly below — `historical.py` imports `data.loader.load_m5`, and
`mt5_feed.py` optionally imports `config.settings` for credential fallback).

---

## Module: `pine_ob_bot/__init__.py`

**Purpose:** Package export surface. Marks the package as a derivative of a
LuxAlgo Pine SMC indicator (CC BY-NC-SA 4.0), for non-commercial research.

**Exports:** `BotConfig`, `Candle`, `Tick`, `PineSwingOBEngine`, `PaperBroker`
via `__all__`.

**Used by:** nothing internal depends on the package `__init__` (all internal
modules import directly from submodules, e.g. `from .config import BotConfig`).
External consumers (if any) would use `from pine_ob_bot import ...`.

No classes/functions/constants of its own beyond the re-exports.

---

## Module: `pine_ob_bot/models.py`

**Purpose:** All shared plain-data dataclasses used across the whole package —
market data, structure events, order-block/pending-order/position/trade
records. No behavior beyond two computed properties and a generic `to_dict`.
Everything is `@dataclass(slots=True)` for memory efficiency (this project
processes potentially millions of candles/ticks in backtests).

### `Direction = Literal["bull", "bear"]`
Type alias used everywhere for trade direction. `"bull"` = buy/long,
`"bear"` = sell/short (confirmed by `demo_executor.py`'s
`is_buy = position.direction == "bull"`).

### `Candle` (dataclass, slots)
Fields: `time: str` (ISO-8601), `open: float`, `high: float`, `low: float`,
`close: float`. Represents one closed M5 (or M15-aggregated) bar.

### `Tick` (dataclass, slots)
Fields: `time: str`, `bid: float`, `ask: float`. Live/replayed tick quote.

### `Pivot` (dataclass, slots)
Fields: `level: float`, `bar_index: int`, `time: str`, `crossed: bool = False`.
Represents a confirmed Trend-Swing pivot (major swing high or low) inside
`PineSwingOBEngine`. `crossed` flags whether price has already broken through
it (consumed exactly once for a BOS/CHoCH).

### `StructureBreak` (dataclass, slots)
Fields: `direction: Direction`, `kind: Literal["BOS","CHoCH"]`,
`pivot_level: float`, `bar_index: int`, `time: str`. One structural break
event (BOS or CHoCH) emitted by `PineSwingOBEngine.process`.

### `OrderBlock` (dataclass, slots)
Fields: `id: str`, `direction: Direction`, `high: float`, `low: float`,
`source_index: int`, `source_time: str`, `formed_index: int`,
`formed_time: str`, `active: bool = True`, `break_kind: str = "unknown"`,
`atr_at_formation: float | None = None`.
- Property `entry`: `high` if bull else `low` — the OB's tradeable edge.
- Property `stop`: `low` if bull else `high` — the OB's invalidation edge.

### `PendingOrder` (dataclass, slots)
Fields: `id`, `ob_id`, `direction`, `entry: float`, `stop: float`,
`target: float`, `created_index: int`, `created_time: str`,
`active: bool = True`, `meta: dict = {}` (default_factory), `lifecycle_state:
str = "formed"`, `accepted_index: int | None = None`,
`armed_index: int | None = None`.
This is the not-yet-filled virtual order sitting in `PaperBroker.pending`.
`lifecycle_state` is the real state-machine field (see Pending Order lifecycle
section below) — its default is `"formed"` but `PaperBroker.add_ob` actually
sets it to `"armed"` unless `entry_lifecycle_enabled` is on (see below).

### `PaperPosition` (dataclass, slots)
Fields: `id`, `order_id`, `direction`, `entry: float`, `stop: float`,
`target: float`, `volume: float`, `risk_money: float`, `opened_time: str`,
`opened_index: int | None = None`, `max_adverse: float = 0.0`,
`max_favorable: float = 0.0`, `meta: dict = {}`.
An open, filled paper trade. `max_adverse`/`max_favorable` are raw price
excursions (not yet R-normalized — that happens at close).

### `Trade` (dataclass, slots)
Fields: `id`, `direction`, `entry`, `stop`, `target`, `exit_price: float`,
`volume`, `risk_money`, `pnl: float`, `r_multiple: float`, `opened_time`,
`closed_time: str`, `result: Literal["win","loss"]`, `order_id: str = ""`,
`ob_id: str = ""`, `opened_index`, `closed_index: int | None = None`,
`max_adverse_r: float = 0.0`, `max_favorable_r: float = 0.0`, `meta: dict = {}`.
A closed trade record — the unit of output for all reporting.

### `to_dict(value) -> dict`
Thin wrapper over `dataclasses.asdict`. Used incidentally; most modules call
`asdict` directly rather than this helper.

**Used by:** every other module in the package. `Candle`/`Tick` flow in from
feeds/CSV; `OrderBlock`/`StructureBreak`/`Pivot` are produced by
`pine_engine.py`; `PendingOrder`/`PaperPosition`/`Trade` are produced/consumed
by `paper.py`.

**Edge cases:** none of these classes validate their own fields (e.g. no check
that `high >= low`); validation happens at the call sites (e.g.
`PaperBroker.add_ob` checks `ob.high <= ob.low`).

---

## Module: `pine_ob_bot/config.py`

**Purpose:** Single `BotConfig` dataclass holding every tunable parameter for
the whole system (strategy windows, risk models, execution mode, persistence
paths, dashboard, demo-execution guard rails), plus a `validate()` method that
is the sole gatekeeper for internal consistency. This is the "one file to
tune" analog of the legacy bot's `config/settings.py`.

### `BotConfig` (dataclass, slots)
Key fields (grouped; defaults as declared):

*Symbol/timeframe:* `symbol="XAUUSD"`, `timeframe="M5"`, `timeframe_minutes=5`
(both are validated to be exactly M5 — "v1 supports closed M5 candles only").

*Trend Swing (major structure, feeds `current_m5_trend`):*
`trend_swing_length=12` — bars each side for `PineSwingOBEngine`'s
state-based swing/BOS/CHoCH confirmation (~60 min delay at M5).
`swing_length: InitVar[int|None] = None` — deprecated alias resolved in
`__post_init__` into `trend_swing_length`; a **read-only property** of the
same name is attached to the class *after* the class body (see TECH DEBT
below) so `cfg.swing_length` still reads back the effective value.
`atr_period=200` — ATR lookback for the Trend Swing engine.

*Entry Pivot (independent local trigger timing, never feeds trend):*
`entry_pivot_left=5`, `entry_pivot_right=5` (~25 min confirmation delay,
right-side only).

*M15 context (observation only):* `m15_swing_length=10`, `m15_atr_period=200`.

*Target/RR:* `rr=1.5`, `target_mode="fixed_rr"` (other allowed value:
`"m5_liquidity_min_rr"`).

*Risk fraction & adaptive risk-sizing models* (mutually exclusive, enforced in
`validate()`): `risk_fraction=0.01` (base/default), then six independently
toggle-able experimental sizing models, each with its own fraction knobs:
`liquidity_risk_sizing_enabled`, `choch_risk_sizing_enabled`,
`combined_context_risk_sizing_enabled`, `displacement_risk_sizing_enabled`,
`choch_displacement_risk_sizing_enabled`, `three_factor_risk_sizing_enabled`
— plus non-exclusive override knobs `bos_three_factor_risk_enabled`,
`choch_split_risk_enabled`, `choch_risk_cap_fraction`,
`bos_m15_counter_bonus_fraction`. Every fraction has a bounds check in
`validate()` (`0 < x <= 1`).

*Entry filters:* `max_open_positions=1`, `allowed_break_kinds=("BOS",)`,
`strong_choch_only=False`, `min_entry_wait_bars=0`, `entry_mode="limit"`
(other: `"sweep_reclaim"`), `sweep_reclaim_atr_buffer=0.20`,
`sweep_reclaim_max_bars=1` (**EXPERIMENTAL** for >1, gated behind
`--sweep-reclaim-max-bars`), `revival_shadow_audit=False` (**EXPERIMENTAL**,
observation-only, `--revival-shadow-audit`), `max_entry_pivot_age_bars=None`.

*Breakeven:* `breakeven_trigger_r: float | None = None` — R multiple of MFE
that triggers a one-time stop-to-entry move; `None` disables it.

*Lifecycle:* `entry_lifecycle_enabled=False` — gates the optional
formed→accepted→armed state machine in `PaperBroker._advance_lifecycle`.

*Live/poll:* `poll_seconds=1.0`, `fallback_spread=0.20`, `history_bars=5000`,
`state_history_bars=2000`.

*Persistence paths:* `db_path`, `trades_csv`, `summary_csv`, `log_path` (all
default under `pine_ob_bot_data/`).

*Market capture:* `market_capture_enabled=True`, `market_capture_dir`.

*Live safety:* `max_live_tick_age_seconds=30.0`, `entry_spread_guard_enabled=True`,
`max_entry_spread_atr_fraction=0.10`, `spread_median_window=300`,
`spread_median_min_samples=20`, `spread_median_multiplier=3.0`.

*Reporting/dashboard:* `daily_reports_dir`, `report_timezone="Asia/Tehran"`,
`report_refresh_seconds=300.0`, `breakdown_refresh_seconds=900.0`,
`candle_poll_seconds=5.0`, `dashboard_enabled=True`, `dashboard_host`,
`dashboard_port=8765`.

*Demo execution:* `demo_orders_enabled=False`, `demo_magic=120512`,
`demo_deviation_points=100`, `demo_max_free_margin_fraction=0.80`.

### `__post_init__(self, swing_length)`
If the deprecated `swing_length=` kwarg was passed, overwrites
`trend_swing_length` with it. Otherwise does nothing (new field stays at
whatever was explicitly set or defaulted).

### `validate(self) -> None`
Raises `ValueError` on: non-M5 timeframe; non-positive lookback windows;
non-positive entry-pivot windows; invalid `max_entry_pivot_age_bars`;
`state_history_bars` shorter than warm-up needs; non-positive `rr`/
`risk_fraction`; invalid `target_mode`; invalid base/liquidity/CHoCH/combined
risk fractions; **more than one** adaptive risk-sizing model enabled
simultaneously (checked via `sum(...)` of six booleans); invalid displacement
rank periods/fractions; invalid `choch_risk_cap_fraction`; invalid entry-age
rank periods; invalid CHoCH split fractions; invalid BOS/M15 counter bonus;
invalid entry filters (`min_entry_wait_bars<0` or empty
`allowed_break_kinds`); invalid `entry_mode`; negative
`sweep_reclaim_atr_buffer`; `sweep_reclaim_max_bars<1`; non-positive
`breakeven_trigger_r` (if set); `max_open_positions<1`; negative
`fallback_spread`; non-positive `max_live_tick_age_seconds`; invalid spread
guard thresholds/window; non-positive `report_refresh_seconds`; breakdown
refresh faster than report refresh; non-positive `candle_poll_seconds`;
invalid dashboard port; invalid demo magic/deviation; invalid demo margin
fraction.

### Module-level tail: `BotConfig.swing_length = property(...)`
Read-only alias attached to the class **after** the class body / decorator,
with an unusually long explanatory comment.

**TECH DEBT (documented in-code, worth a dedicated callout in the docs):**
the comment explains that defining `swing_length` as an in-body `@property`
with the same name as the `InitVar` field would be captured by
`@dataclass(slots=True)` as that InitVar's *default value* (because dataclass
reads the class namespace at decoration time, which happens after the whole
class body executes) — silently replacing `trend_swing_length` with a
`<property object>` on every `BotConfig()` call that didn't pass
`swing_length=` explicitly. This was a real bug discovered and fixed by
attaching the property post-hoc instead. Worth a chapter aside: a subtle
Python dataclass/property interaction pitfall, already fixed, not currently
live but instructive.

**Used by:** everything. `run_pine_ob_paper.py` builds one `BotConfig` from
CLI args; `PaperApp`, `PaperBroker`, `PineSwingOBEngine`, `M15Context`,
`LiquidityTracker`, historical/tick runners all take `cfg: BotConfig` in their
constructors.

---

## Module: `pine_ob_bot/trend_filter.py`

**Purpose:** The single source of truth for whether a trade direction is
currently allowed, given the M5 structural trend from `PineSwingOBEngine`.
Deliberately isolated so every execution path (live, historical OHLC replay,
tick replay, sweep/reclaim) calls the same functions instead of
re-implementing the check.

### `TrendDirection(str, Enum)`
`BULLISH = "bullish"`, `BEARISH = "bearish"`, `NEUTRAL = "neutral"`.

### Constants
`TRADE_REJECTED_M5_TREND_MISMATCH`, `TRADE_REJECTED_M5_TREND_NEUTRAL`,
`ORDER_CANCELLED_M5_TREND_CHANGED`, `ORDER_CANCELLED_M5_TREND_NEUTRAL` — string
reason codes used throughout `paper.py`'s stats/events/meta.
`_BUY_SIDE = {"bull","buy"}`, `_SELL_SIDE = {"bear","sell"}` — both this
project's own vocabulary and a buy/sell alias are accepted.

### `trend_from_engine_state(value: int) -> TrendDirection`
Maps `PineSwingOBEngine.trend` (`1`/`-1`/`0`) to the enum (`1`→BULLISH,
`-1`→BEARISH, else NEUTRAL).

### `TrendValidationResult` (frozen dataclass)
Fields: `is_allowed: bool`, `current_trend: TrendDirection`, `side: str`,
`rejection_reason: str | None`.

### `is_trade_allowed_by_m5_trend(side: str, current_trend) -> bool`
`NEUTRAL` → always `False`. Buy-side → `True` iff BULLISH. Sell-side → `True`
iff BEARISH. Unrecognized side → `False`.

### `validate_trade_direction(side, current_trend) -> TrendValidationResult`
Wraps the above with a rejection reason (`..._NEUTRAL` or `..._MISMATCH`).
This is "the one function every order-creation and every fill path must
call" per the module docstring — and indeed `PaperBroker.add_ob` and
`PaperBroker._open` both call it directly.

**Used by:** `paper.py` (`PaperBroker.add_ob`, `PaperBroker._open`,
`PaperBroker.update_m5_trend`), and every runner (`app.py`, `historical.py`,
`tick_historical.py`) via `trend_from_engine_state(engine.trend)`.

**Edge case:** any non-`"bull"/"buy"/"bear"/"sell"` side string, or `None`,
or a non-string, is always rejected (verified in `test_trend_filter.py`
`test_invalid_side_is_rejected`).

---

## Module: `pine_ob_bot/entry_pivot.py`

**Purpose:** A second, deliberately **independent** classic symmetric pivot
detector, used only to gate/time entries on OBs that Trend Swing already
formed. Never influences `current_m5_trend`. Strictly causal: a pivot at
index `i` is only published once `right_bars` closed bars after it exist.

### Constants: `PIVOT_HIGH = "pivot_high"`, `PIVOT_LOW = "pivot_low"`

### `EntryPivot` (frozen dataclass, slots)
Fields: `candle_index: int`, `pivot_time: str`, `confirmed_at: str`,
`kind: str`, `price: float`, `left_bars: int`, `right_bars: int`.
`pivot_time` = candidate bar's own timestamp (must never be used to
"backdate" a trading decision); `confirmed_at` = the later timestamp of the
bar that closed the right-side window — this is what trading logic must key
off.

### `ClassicEntryPivotDetector`
- `__init__(self, left_bars=5, right_bars=5)`: validates both `>=1`; keeps
  a rolling `self.candles` buffer and `self.index_offset` (for continuity
  across trimmed state round-trips).
- `process(self, candle: Candle) -> list[EntryPivot]`: appends the candle,
  computes `candidate_local = local_i - right_bars`. If there aren't yet
  `left_bars` bars before the candidate, returns `[]`. Otherwise slices the
  left and right windows and checks **strict** inequality (`>`/`<`, ties
  never count) for both a pivot-high (`candidate.high` beats every bar in
  both windows) and a pivot-low (`candidate.low` beats every bar in both
  windows) — a single candle *can* emit both a confirmed high and low in the
  same call (rare but structurally possible). Publishes at most once per
  candidate bar (append-once buffer, single pass).
- `dump_state(self, max_candles=None) -> dict`: trims the candle buffer if
  requested, shifting `index_offset` accordingly.
- `load_state(self, data: dict) -> None`: restores `left_bars`/`right_bars`/
  candles/`index_offset`.

**Used by:** `app.py` (`self.entry_pivot`), `historical.py`,
`tick_historical.py` — all three construct one
`ClassicEntryPivotDetector(cfg.entry_pivot_left, cfg.entry_pivot_right)` and
feed it every closed candle right after `update_m5_trend`, then call
`broker.update_entry_pivot(...)` for each confirmed pivot, before any
`add_ob` call for that candle. This ordering is asserted by the module
docstring and exercised in `test_trend_engine_integration.py`.

**Edge case verified by tests:** `test_entry_pivot_freshness.py`
`test_asymmetric_left_never_adds_confirmation_delay` confirms the left window
size only affects *which* bar can be a candidate, never the confirmation
delay (which is purely `right_bars`).

---

## Module: `pine_ob_bot/structure_context.py`

**Purpose:** Three independent, causal, **observation-only** helpers that
enrich OB/fill metadata for reporting and (for CHoCH/Displacement) optional
adaptive risk sizing — none of them ever block/allow a trade by themselves
except through the specific risk-sizing paths in `paper.py` that
explicitly read their output.

### `displacement_snapshot(candle, ob, breaks) -> dict`
Finds the `StructureBreak` matching `ob.direction`/`ob.break_kind`. Computes,
ATR-normalized where possible: `bos_body_atr`, `bos_range_atr`,
`bos_body_to_range`, `bos_close_through_atr` (how far close pushed past the
pivot level, signed by direction), `bos_close_location` (0..1 position of
close within the bar's range, direction-aware), `bos_directional_body`
(bool: candle closed in the break's direction).

### `ChochEvent` (dataclass, slots): `direction`, `bar_index`, `time`, `pivot_level`.

### `ChochContext`
- `__init__`: `last_by_direction: dict[str, ChochEvent]`, `last: ChochEvent|None`.
- `process(self, breaks: list[StructureBreak]) -> None`: for every `CHoCH`-kind
  break, updates `last_by_direction[direction]` and `last`.
- `snapshot(self, trade_direction, bar_index) -> dict`: returns
  `m5_last_choch_direction`, `m5_last_choch_alignment`
  ("aligned"/"opposite"/"none"), `m5_last_choch_age_bars`,
  `m5_same_choch_age_bars`, `m5_opposite_choch_age_bars`.
- `dump_state`/`load_state`: index-offset-aware persistence.

### `DisplacementContext`
Rolling, causal percentile rank of BOS/CHoCH close-through strength.
- `__init__(self, window=100, min_samples=20)`.
- `observe(self, value: float|None) -> dict`: computes the percentile of
  `value` against **only prior** history (appended *after* computing the
  percentile — no leakage), returns `bos_close_through_percentile`,
  `bos_displacement_top_quartile` (percentile >= .75),
  `bos_displacement_history_samples`.
- `dump_state`/`load_state`: trims to `window`.

**Used by:** `app.py`, `historical.py`, `tick_historical.py` all instantiate
one `ChochContext` and **two** `DisplacementContext`s (one for BOS breaks,
one for CHoCH breaks — `ranker = displacement if break_kind=="BOS" else
choch_displacement`), calling `.observe()` on each newly formed OB's
`bos_close_through_atr` and attaching results to the OB's meta via
`PaperBroker.add_ob`'s `extra_meta`. `PaperBroker._open` later reads
`order.meta.get("bos_close_through_percentile")` /
`bos_displacement_top_quartile` for several adaptive risk-sizing models.

---

## Module: `pine_ob_bot/liquidity_context.py`

**Purpose:** Observation-only swing-liquidity (BSL/SSL pool) tracker — never
gates a trade. Tracks pools formed at confirmed swing points, their
active/touched/swept/broken lifecycle, and exposes nearest-pool distance and
"opposite-side sweep" context for reporting and one specific risk-sizing model.

### `LiquidityPool` (dataclass, slots)
Fields: `id`, `kind` ("BSL" above a swing high / "SSL" below a swing low),
`level: float`, `created_index: int`, `created_time: str`,
`state: str = "active"`, `event_index`, `event_time`, `event_depth`,
`event_atr: float | None = None`.

### `LiquidityTracker`
- `__init__(self, swing_length=10, atr_period=200)`: own independent ATR/TR
  series and swing-leg state (`self.leg`), separate from `PineSwingOBEngine`.
- `process(self, candle: Candle) -> None`: computes TR/ATR; for every
  `active`/`touched` pool, checks this candle's high/low against the pool
  level: BSL pool — `high > level and close < level` → `"swept"`;
  `close > level` → `"broken"`; else if merely touched → `"touched"`.
  Mirror logic for SSL. Then calls `_confirm_swing(i)` to possibly create a
  new pool. Caps `self.pools` at 500 (drops oldest).
- `_confirm_swing(self, i)`: same delayed-pivot pattern as
  `PineSwingOBEngine._update_swing` but independent state (`self.leg`);
  creates a new BSL pool at a confirmed swing high, SSL at a confirmed swing
  low.
- `_event(self, pool, state, index, time, depth)`: records the event on the
  pool and updates `self.last_event[pool.kind]`.
- `setup_snapshot(self, direction, leg_start_index, bos_index) -> dict`:
  "opposite sweep bounded by the structural leg that produced this BOS" —
  looks for an **opposite**-kind pool swept strictly within
  `[leg_start_index, bos_index]`. Returns `setup_m5_opposite_sweep` (bool),
  leg/sweep index/depth/position metrics. This is the "setup-related sweep"
  flag used by `liquidity_risk_sizing_enabled`.
- `_atr(self, tr) -> float | None`: standard seed-SMA-then-RMA ATR, same
  pattern as `pine_engine.py`.
- `snapshot(self, direction, price, prefix) -> dict`: nearest opposing-side
  pool distance/level, and "opposite last event" info (state/age/depth/
  swept-bool) for the *opposite* kind — attached with the given `prefix`
  (`"liq_m5_"` or `"liq_m15_"`).
- `dump_state`/`load_state`: index-offset-aware trimming, retaining pools
  still active/touched or referenced by `last_event`.

**Used by:** `app.py`/`historical.py`/`tick_historical.py` each instantiate
**two** trackers (`liq_m5` at `cfg.swing_length`/`cfg.atr_period` — NOTE this
reads `cfg.swing_length`, the deprecated property alias, effectively
`trend_swing_length`; and `liq_m15` at `cfg.m15_swing_length`/
`cfg.m15_atr_period`), feed them every M5 (and flushed M15) candle, and
attach both `.snapshot()` and `.setup_snapshot()` output into OB/fill meta.
`PaperBroker._open` reads `setup_m5_opposite_sweep` for
`liquidity_risk_sizing_enabled` and the combined/three-factor models.

**NEEDS REVIEW:** `liq_m5 = LiquidityTracker(cfg.swing_length, cfg.atr_period)`
uses the deprecated `cfg.swing_length` property alias rather than
`cfg.trend_swing_length` directly. Functionally identical (the property just
forwards), but stylistically inconsistent with the rest of the code, which
was migrated to prefer `trend_swing_length`. Not a bug, just legacy-name
residue worth flagging if a doc reader asks "why does this still say
swing_length".

---

## Module: `pine_ob_bot/mtf_context.py`

**Purpose:** Builds a closed-candle M15 context stream by aggregating three
M5 bars into one M15 candle and running a **second, independent**
`PineSwingOBEngine` instance on it — purely for observational
trend/alignment/range/OB metadata attached to every M5 setup. Explicitly
never gates M5 trades (module docstring: "it never gates M5 trades").

### `M15Context`
- `__init__(self, cfg: BotConfig)`: builds a `replace()`d copy of `cfg` with
  `swing_length=cfg.m15_swing_length` and `atr_period=cfg.m15_atr_period`
  (note: `timeframe`/`timeframe_minutes` stay `"M5"`/`5` even though this
  models M15 bars — since `PineSwingOBEngine` doesn't otherwise use the
  timeframe field for its math, this is harmless but slightly confusing
  naming — **AMBIGUOUS/minor**, flag as "the M15 engine internally still
  thinks its timeframe label is M5; only the *input* candles are pre-
  aggregated to 15-minute bars").
- `process_m5(self, candle) -> Candle | None`: buckets M5 candles into
  15-minute buckets by wall-clock minute (`minute // 15 * 15`). Flushes
  (returns) a completed M15 candle when `minute % 15 == 10` (the M5 bar
  whose *open* timestamp is the 3rd/final bar of the bucket, since M5
  timestamps are bar-opens). Partial startup buckets (not exactly 3 parts)
  are silently discarded — no M15 bar, no `last_break` update.
- `_flush(self) -> Candle | None`: builds the OHLC M15 candle from exactly 3
  M5 parts (open=first.open, high=max, low=min, close=last.close), runs
  `self.engine.process(bar)`, updates `self.last_break` if any break fired.
- `snapshot(self, m5_direction, price) -> dict`: `m15_trend`,
  `m15_alignment` ("aligned"/"counter"/"unknown"), `m15_last_break_kind/
  direction`, `m15_break_age_bars`, `m15_swing_high/low`,
  `m15_range_position` (0..1 within swing range),
  `m15_range_zone` ("discount"/"premium"/"equilibrium", thirds split),
  `m15_atr`, `m15_active_ob_direction`, `m15_active_ob_width_atr`.
- `dump_state`/`load_state`: wraps the inner engine's state plus bucket-in-
  progress parts and last break.

**Used by:** all three runners; feeds `m15.snapshot(...)` into OB/fill meta
for reporting and the `bos_m15_counter_bonus_fraction` risk-sizing bonus
(read via `fill_context.get("m15_alignment") == "counter"` in
`PaperBroker._open`).

---

## Module: `pine_ob_bot/position_sizing.py`

**Purpose:** The single, deterministic, broker-safe lot-sizing function used
by both Paper and Demo. Central invariant: realized risk must never exceed
the configured risk percentage — an undersized lot is **rejected**, never
rounded up to the broker minimum (which would silently multiply risk).

### Constants
`CALCULATED_VOLUME_BELOW_BROKER_MINIMUM`, `ACTUAL_RISK_EXCEEDS_ALLOWED_RISK`,
`INVALID_STOP_DISTANCE`, `INVALID_SYMBOL_SPEC`, `INVALID_RISK_INPUT` —
rejection reason codes. `_STEP_EPSILON = 1e-9` (float slack only, never used
to justify extra risk). `_RISK_TOLERANCE = 1.01` (1% float-dust tolerance on
the "actual risk must not exceed allowed risk" check).

### `PositionSizeResult` (frozen dataclass, slots)
Fields: `volume: float`, `allowed_risk: float`, `actual_risk: float`,
`is_valid: bool`, `rejection_reason: str | None`.

### `calculate_safe_position_size(equity, risk_percent, entry_price,
stop_loss, tick_size, tick_value, volume_min, volume_max, volume_step) ->
PositionSizeResult`
Logic, in order:
1. `equity<=0 or risk_percent<=0` → `INVALID_RISK_INPUT`.
2. Any of `tick_size/tick_value/volume_step/volume_min/volume_max <= 0` →
   `INVALID_SYMBOL_SPEC`.
3. `allowed_risk = equity * risk_percent`; `distance =
   abs(entry_price - stop_loss)`; `distance<=0` → `INVALID_STOP_DISTANCE`.
4. `loss_per_lot = distance / tick_size * tick_value`;
   `raw_volume = allowed_risk / loss_per_lot`.
5. `capped_volume = min(raw_volume, volume_max)` (cap can only shrink).
6. `normalized_volume = floor((capped_volume + epsilon) / volume_step) *
   volume_step` (floor can only shrink).
7. If `normalized_volume < volume_min - epsilon` →
   `CALCULATED_VOLUME_BELOW_BROKER_MINIMUM`, `is_valid=False`, but the
   **attempted** (too-small) volume/risk are still returned for logging.
8. Else compute `actual_risk`; if it exceeds `allowed_risk * 1.01` →
   `ACTUAL_RISK_EXCEEDS_ALLOWED_RISK` (defensive; should be rare given the
   floor/cap logic, but present as a belt-and-suspenders check).
9. Otherwise `is_valid=True`, `rejection_reason=None`.

**Used by:** exclusively `PaperBroker._open` (see below). Also directly unit
tested in `test_position_sizing.py` and cross-checked against
`PaperBroker._open`'s output in `test_paper_broker_sizing.py`.

**No AMBIGUOUS/TECH DEBT here** — this module is unusually tightly specified
and tested (it was clearly the subject of a bug-fix pass; the module
docstring and tests reference "the fix request").

---

## Module: `pine_ob_bot/pine_engine.py`

**Purpose:** Bar-close port of the original Pine "Swing Structure / Swing
Order Block" indicator logic. This is **the** Trend Swing engine — the sole
source of Major Swing High/Low, Major BOS/CHoCH, and the single M5 Trend
State (`self.trend`: 1 bullish / -1 bearish / 0 neutral).

### `PineSwingOBEngine`
- `__init__(self, cfg: BotConfig)`: `candles`, `parsed_highs`/`parsed_lows`
  (volatility-adjusted highs/lows used for swing/OB math — see below),
  `true_ranges`, `atr_values`, `leg=0` ("Pine initializes leg() to bearish
  leg" — comment), `trend=0`, `swing_high`/`swing_low: Pivot|None`,
  `order_blocks: list[OrderBlock]`, `last_time: str|None`.

- `process(self, candle: Candle) -> tuple[list[StructureBreak],
  list[OrderBlock], list[str]]` (breaks, newly formed OBs, invalidated OB
  ids). Steps, in order:
  1. **Dedupe guard:** if `candle.time <= self.last_time`, return
     `([], [], [])` — no reprocessing of an already-seen or out-of-order bar.
  2. True Range = `max(high-low, |high-prev_close|, |low-prev_close|)`;
     `atr = self._next_atr(tr)`.
  3. **Volatility filter:** if `atr is not None and (high-low) >= 2*atr`,
     the candle is "volatile" and its **parsed** high/low are *swapped*
     (`parsed_highs.append(candle.low if volatile else candle.high)` and
     vice versa) — this suppresses a single outsized candle from creating a
     false pivot extreme (mirrors the Pine original's noise handling).
  4. `self._update_swing(i)` — may set a new `swing_high`/`swing_low` Pivot
     (delayed by `trend_swing_length` bars, see below).
  5. **Break detection (close-only):** if `swing_high` exists, is not yet
     `crossed`, and `prev_close <= swing_high.level < candle.close` → a
     **bullish** break. `kind = "CHoCH" if self.trend == -1 else "BOS"`.
     Marks `swing_high.crossed = True`, sets `self.trend = 1`, builds an OB
     via `_make_ob`, inserts it at the *front* of `order_blocks` (most
     recent first). Symmetric logic for `swing_low` → bearish break,
     `self.trend = -1`.
     Note both branches can fire on the same candle (a close simultaneously
     crossing both a still-open swing high and swing low) — order in code is
     bull-check first, then bear-check, so a same-candle double break is
     possible in principle (**NEEDS REVIEW**: not explicitly tested; would
     require an extreme single-candle range crossing both pivots).
  6. **OB invalidation:** for every OB in `order_blocks`, if still `active`
     and price crosses through its stop edge (`bear` OB invalidated when
     `candle.high > ob.high`; `bull` OB invalidated when `candle.low <
     ob.low`), set `active=False` and record its id in `invalidated`.
  7. `self.order_blocks = self.order_blocks[:100]` — hard cap at 100 most
     recent OBs (oldest silently dropped, even if still active —
     **TECH DEBT/EDGE CASE**: an OB that's still `active` could in theory be
     evicted purely by list length if 100+ newer OBs have formed since,
     though price would very likely have invalidated it long before that in
     practice; flag as "not causally wrong, just a resource cap").
  8. `self.last_time = candle.time`.

- `_next_atr(self, tr) -> float | None`: `None` until `count == atr_period`
  bars exist, then seeds with the simple average, then standard Wilder/RMA
  recursion `(prev*(n-1)+tr)/n`.

- `_update_swing(self, i)`: `n = cfg.trend_swing_length`. No-op until
  `i >= n`. `candidate = i - n`; compares `candles[candidate]`'s high/low
  against the window `candles[candidate+1 : i+1]` (i.e. the `n` bars after
  it, up to and including the current bar). If the candidate's high beats
  every high in that window → `new_leg = 0` (bullish/high leg); if its low
  beats every low → `new_leg = 1` (bearish/low leg). If the leg actually
  changed, sets `swing_low` (leg==1) or `swing_high` (leg==0) to a new
  `Pivot` from the candidate bar. This is the delayed-pivot pattern shared
  conceptually with `LiquidityTracker._confirm_swing` and structurally
  similar to `ClassicEntryPivotDetector`, but with `left_bars==right_bars==
  trend_swing_length` implicitly (asymmetric-vs-symmetric distinction is
  the whole point of having `entry_pivot.py` separately).

- `_make_ob(self, pivot, direction, i, formed_time, break_kind) ->
  OrderBlock | None`: "Pine array.slice(start,end) excludes the
  structure-break candle" — comment. If `pivot.bar_index >= i` return
  `None` (defensive). Takes the segment of `parsed_lows` (bull) or
  `parsed_highs` (bear) from `pivot.bar_index` to `i` (exclusive of `i`);
  finds the extreme (min for bull, max for bear) — this is the true "Order
  Block" source candle in classic SMC terms (the last opposite-direction
  candle before the impulsive break). Builds an `OrderBlock` using that
  source candle's *actual* (non-parsed) `parsed_highs[source]`/
  `parsed_lows[source]` as its high/low box, `atr_at_formation =
  atr_values[i]` (ATR **at the break bar**, not the source bar).

- `dump_state(self, max_candles=None) -> dict` / `load_state(self, data)`:
  trims history to `max_candles`, always keeping enough for any still-active
  OB or the current swing pivots, rebasing all bar indices by the trim
  `start` offset.

**Used by:** `app.py` (`self.engine`), `historical.py`, `tick_historical.py`
(`engine`), and `M15Context` (a *second* instance configured for
M15-aggregated bars). `PaperBroker` never imports this module directly —
it only receives already-built `OrderBlock`/`StructureBreak` objects and the
scalar `engine.trend` via `trend_from_engine_state`.

**Confirmed by tests:** `test_trend_engine_integration.py` hand-verifies an
exact 9-bar sequence producing Neutral→bullish BOS (bar 7)→bearish CHoCH
(bar 8), used as the canonical worked example for "how a break becomes an
OB becomes a trend flip."

---

## Module: `pine_ob_bot/paper.py` (1209 lines — the core execution engine)

**Purpose:** `PaperBroker` — the in-process, no-network execution simulator
that owns the entire Pending Order → Fill → Position → Trade lifecycle,
across three parallel execution paths (live/tick, OHLC replay, sweep-reclaim
tick and candle variants), plus every risk-sizing model, the M5 trend gate,
the Entry Pivot admission gate, breakeven, and an experimental observation-
only "revival shadow audit." Explicitly documented as containing **no MT5
order-send method** — it is pure simulation.

### Module-level constant
`REJECTED_NO_ENTRY_PIVOT = "REJECTED_NO_ENTRY_PIVOT"`.

### `SymbolSpec` (dataclass, slots)
`tick_size=0.01`, `tick_value=1.0`, `volume_min=0.01`, `volume_max=100.0`,
`volume_step=0.01`. Broker/symbol contract, normally supplied by
`MT5ReadOnlyFeed.connect()` live, or hand-set for backtests.

### `PaperBroker`

#### `__init__(self, cfg, equity, spec=None)`
Sets up: `initial_equity`/`equity`/`peak_equity`/`max_drawdown`; `spec`;
`pending: list[PendingOrder]`; `positions: list[PaperPosition]`;
`trades: list[Trade]`; `market_index: int|None`; `market_context: dict`;
`entry_age_values: list[int]` (rolling percentile-rank sample of fill wait
times); `recent_spreads: deque(maxlen=cfg.spread_median_window)`;
`rejected_sizing`/`trend_events`/`pivot_events`/`breakeven_events`: lists
**drained by the caller** after each `process_tick`/`process_candle`/
`process_signal_candle` call, so every event is logged/persisted exactly
once; `current_m5_trend: TrendDirection = NEUTRAL`; `last_pivot_high`/
`last_pivot_low: EntryPivot|None`; `entry_pivot_gate_active: bool = False`
(off by default so pre-existing unit tests that call `add_ob` directly are
unaffected — every *real* execution path calls `enable_entry_pivot_gate()`);
`revival_shadow`/`shadow_positions: list[dict]` (pure observation, never
touches real orders); a large `self.stats: dict[str,int|float]` counter
dictionary (dozens of named counters — setups seen/rejected by reason, fills
by mode, sweep-reclaim funnel counters, revival-shadow funnel counters,
entry-pivot counters, breakeven count, buy/sell setup counts).

#### `position` (property)
Returns `positions[0]` if any — "backward-compatible view of the oldest open
position" (most code paths now use the plural `positions` list, gated to one
by `max_open_positions` in practice, but the data model supports >1).

#### `add_ob(self, ob: OrderBlock, extra_meta=None) -> PendingOrder | None`
The **admission funnel** for turning a freshly formed OB into a tradeable
pending order. In order:
1. `stats["setups_seen"] += 1`.
2. Break-kind filter: if `ob.break_kind` is not `"unknown"` and not in
   `cfg.allowed_break_kinds` → reject (`rejected_break_kind`).
3. `strong_choch_only`: if set and this is a CHoCH OB without
   `bos_displacement_top_quartile` in `extra_meta` → reject
   (`rejected_weak_choch`).
4. Sanity: `ob.high <= ob.low` or an order already exists for this
   `ob.id` → reject (silently, no counter, returns `None`).
5. **M5 trend gate:** `validate_trade_direction(ob.direction,
   current_m5_trend)` — reject with `rejected_trend_neutral` or
   `rejected_trend_mismatch`, logging a `trend_events` entry
   (`event_type="order_rejected_m5_trend"`).
6. **Entry Pivot gate** (only if `entry_pivot_gate_active`): the matching-
   direction (`last_pivot_low` for bull, `last_pivot_high` for bear) most
   recently confirmed pivot must exist (else `REJECTED_NO_ENTRY_PIVOT`,
   counter `rejected_no_entry_pivot`); its confirmation bar
   (`candle_index + right_bars`) must be `<= ob.formed_index` (a negative
   age is treated exactly like "missing pivot" — defensive no-lookahead
   guard, same counter, no separate reason code); if
   `cfg.max_entry_pivot_age_bars` is set and age exceeds it → reject
   (`rejected_stale_entry_pivot`, logs `order_rejected_stale_entry_pivot`).
   Otherwise accept and record extensive telemetry
   (`accepted_fresh_entry_pivot`, age sum/max for average reporting).
7. Compute `risk = ob.high - ob.low`; `target = ob.entry ± rr*risk`.
8. `lifecycle = "formed" if cfg.entry_lifecycle_enabled else "armed"` — if
   the optional lifecycle feature is off (the default), orders start
   directly in the fillable `"armed"` state.
9. Build the `PendingOrder` with an extremely rich `meta` dict (break kind,
   ATR at formation, OB width in price and ATR units, source index, trend at
   creation, all the Entry-Pivot provenance fields, plus whatever
   `extra_meta` the caller supplied — M15/liquidity/CHoCH/displacement
   snapshots).
10. Appends to `self.pending`, re-sorts by `(created_index, created_time)`
    (oldest first — FIFO fill priority), increments `buy_setups_created`/
    `sell_setups_created`.

#### `enable_entry_pivot_gate(self) -> None`
Sets `entry_pivot_gate_active = True`. Documented as: must be called once by
every real execution path immediately after constructing the broker.

#### `update_entry_pivot(self, pivot: EntryPivot) -> None`
Records the latest confirmed pivot of its kind into `last_pivot_high`/
`last_pivot_low`, increments the confirmed-pivot counters, appends a
`pivot_events` entry. Never touches `current_m5_trend`.

#### `cancel_ob(self, ob_id: str) -> None`
Deactivates every pending order tied to `ob_id`
(`lifecycle_state = "invalidated"`); if in `sweep_reclaim` mode, also bumps
`sweep_reclaim_invalidated`. Also updates any matching `revival_shadow`
record still `suspended`/`awaiting` to `"dead_ob"` (observation-only side
effect).

#### `update_m5_trend(self, new_trend: TrendDirection, when: str) -> int`
Applies the new trend; if unchanged, no-op (`return 0`). Otherwise, for
every currently-active pending order whose direction is no longer allowed
under the new trend: deactivates it, sets `lifecycle_state =
"cancelled_trend"`, records `cancellation_reason`/`trend_at_cancellation` in
meta, bumps `cancelled_trend_neutral`/`cancelled_trend_change`, appends a
`trend_events` entry, and — **only** if `revival_shadow_audit` and
`entry_mode == "sweep_reclaim"` and `market_index is not None` — appends a
`revival_shadow` observation record (`state="suspended"`) and bumps
`trend_suspended`. Returns the count cancelled. Documented as: must be
called once per closed M5 candle, right after the structure engine
processes it and before any new order or fill evaluation.

#### `reconcile_entry_filters(self) -> int`
Used only on restart/config-change: cancels any still-active pending order
whose `break_kind` is no longer in `cfg.allowed_break_kinds` (i.e. config
changed since the order was created).

#### `reconcile_pending_targets(self) -> int`
Also restart-only: recomputes every active pending order's `target` using
the *current* `cfg.rr`, in case RR changed since restart.

#### `set_market_context(self, context: dict) -> None`
Stores the latest per-direction (`"bull"`/`"bear"`) context snapshot dict
(M15/liquidity/CHoCH), consumed later by `_open` for the
`m5_liquidity_min_rr` target mode and several risk models via
`self.market_context.get(order.direction, {})`.

#### `process_tick(self, tick: Tick) -> list[Trade]`
**The live/tick execution path.** If any position(s) open:
- For each open position: `px = bid if bull else ask`; tracks excursion,
  applies breakeven, checks stop/target hit (`stop` wins if both somehow
  true in the same call — but ticks are single-price so this is really "SL
  checked first" only in the sense of code order, not simultaneity); closes
  via `_close` if hit. Observes the spread. Returns closed trades — **no new
  entries are evaluated while any position is open** (implicit single-slot-
  at-a-time behavior layered on top of the `max_open_positions` capacity
  check below, though note: if positions exist, this branch returns early
  before ever reaching the pending-order loop — meaning **new fills never
  happen in the same `process_tick` call that already has open positions**,
  regardless of `max_open_positions`; only when `self.positions` is empty
  does it fall through to evaluate pending orders — see NEEDS REVIEW below).

- If no positions open: `slots = max_open_positions - len(positions)`
  (which, given the branch above only runs when positions is empty, is
  always `max_open_positions`). For each active, `lifecycle_state=="armed"`
  pending order (oldest first): honors `min_entry_wait_bars`; in
  `sweep_reclaim` mode, requires `meta["sweep_reclaim_confirmed"]` already
  set by a prior closed-candle confirmation; checks whether price has
  already invalidated the order past its stop (`invalid` → deactivate, no
  event logged here); for `sweep_reclaim`: checks the adaptive spread guard,
  computes distance/target fresh from the **live fill price** and the
  (already ATR-buffered) stop, opens via `_open(...)`; for plain `limit`
  mode: checks whether price has reached `order.entry` (`bid<=entry` for
  bull i.e. wait for price to come *down* to a bull limit, `ask>=entry` for
  bear), and if so opens via `_open`.

**NEEDS REVIEW (architectural note, not necessarily a bug):** because the
"any open positions" branch returns immediately without considering pending
orders, `process_tick` can only ever open a *new* position when
`self.positions` is completely empty. Combined with `max_open_positions`
defaulting to `1` in `BotConfig`, this matches the documented single-
position-at-a-time production profile, but if someone sets
`max_open_positions > 1` expecting simultaneous multi-position pyramiding,
the current tick path would still only add new fills on ticks where zero
positions are open — i.e. it cannot open a 2nd position while a 1st is still
running, even though the `slots` calculation superficially suggests it
could. Only `process_candle` (see below) has a comparable but not
identical structure — same issue there too (see its own note). **This
should be raised explicitly in the "risk sizing / max positions" chapter as
a discovered nuance, not asserted as a bug without further testing** — no
existing test exercises `max_open_positions > 1`, so this is genuinely
unverified/AMBIGUOUS behavior beyond what the code visibly does.

#### `process_signal_candle(self, candle: Candle, bar_index: int) -> None`
**The live closed-candle path — signal/state only, never fills or closes.**
Sets `market_index`, advances the optional entry-lifecycle state machine
(`_advance_lifecycle`). If `entry_mode == "sweep_reclaim"`: optionally runs
the revival shadow audit (`_revival_shadow_on_candle`), then for every
active `"armed"` pending order not yet `sweep_reclaim_confirmed` and old
enough (`bar_index - created_index >= max(1, min_entry_wait_bars)`), checks
`_sweep_reclaim_window`; on confirmation, applies the ATR buffer to the stop
and marks `sweep_reclaim_confirmed=True` in meta (fill still deferred to the
next tick).

#### `process_candle(self, candle, bar_index, spread=0.0, execution_source="candle_replay") -> list[Trade]`
**The OHLC-replay fallback path** (used by `run_historical` and, for
context-only advancement, indirectly compared against in tests). If
positions open: skips positions opened *after* this candle
(`_is_after(p.opened_time, candle.time)` guard against replaying a stale
candle onto a newer position — relevant on restart/reordering); tracks
excursion, applies breakeven, then checks SL and TP — **"If both boundaries
occur in one OHLC candle, SL wins"** (explicit code comment and behavior:
`if hit_sl: ... elif hit_tp: ...`), the conservative intrabar-ambiguity
resolution documented project-wide. Then `_advance_lifecycle` and returns.
If no positions: for each active `"armed"` pending order, in age order: if
too young (`age < max(1, min_entry_wait_bars)`), checks for and flags
`early_touch_blocked` (telemetry only, doesn't open); for `sweep_reclaim`
mode delegates to `_try_sweep_reclaim`; otherwise plain limit fill check
(`candle.low + spread <= entry` for bull, mirrored for bear) — opens via
`_open`, then **immediately** (same candle) checks SL/TP against the same
candle's high/low with the same SL-first tie-break, so a fill and its exit
can occur within a single simulated OHLC bar. `_advance_lifecycle` runs
unconditionally at the end.

**Same `slots`-only-decrements-not-truly-multi-position nuance as
`process_tick`** applies here too, though `process_candle`'s slots
calculation is `cfg.max_open_positions` unconditionally (not adjusted by
current `len(positions)`, since this branch is only reached when
`self.positions` is empty) — effectively identical practical behavior to
`process_tick`.

#### `reject_open(self, position_id, reason) -> PaperPosition | None`
Demo-mirroring rollback: if the required MT5 confirmation is rejected,
removes the paper position, tags meta, bumps `demo_entry_reverted`.

#### `apply_demo_entry(self, position, price, volume) -> None`
Rewrites `position.entry`/`volume`/`risk_money` from the confirmed broker
fill, recording before/after values and the "actual" fields in meta,
`execution_state = "demo_confirmed_active"`.

#### `apply_demo_exit(self, trade, price, volume=None) -> None`
Reprices a just-closed trade's `exit_price`/`pnl`/`r_multiple`/`result` from
the confirmed broker exit, and adjusts `equity`/`peak_equity`/
`max_drawdown` by the PnL delta (old vs new).

#### `_entry_spread_allowed(self, order, spread) -> bool`
If `entry_spread_guard_enabled`: builds up to two ceiling candidates — ATR-
relative (`atr_at_formation * max_entry_spread_atr_fraction`) and, once
enough recent-spread samples exist, `median(recent_spreads) *
spread_median_multiplier`. Allowed iff spread is within the **stricter**
(minimum) of whichever limits apply; if no limits apply, always allowed.

#### `_observe_spread(self, spread) -> None`
Appends finite non-negative spreads to `recent_spreads` (bounded deque).

#### `_revival_shadow_on_candle(self, candle, bar_index) -> None`
**EXPERIMENTAL, observation-only** — see the full state-machine writeup
under "Trading concept separation" / revival audit below. Advances existing
`shadow_positions` (hypothetical fills) toward SL/TP, and advances
`revival_shadow` records through `suspended → awaiting → filled_shadow` (or
various terminal dead-end states), per the extensive in-code comment
documenting the exact rules (trend must return within 12 bars, bucketed
3/6/12; fresh same-bar sweep+reclaim only *after* the return; one revival
attempt per OB).

#### `_sweep_reclaim_window(self, order, candle, bar_index) -> tuple[bool,int]`
Shared core of the sweep/reclaim confirmation logic for both the tick-signal
path and the candle-replay path. Sweep is measured against the **OB entry
edge**, explicitly never the stop (comment explains: a stop-break is exactly
the OB engine's own invalidation condition, so a stop-based sweep could
never survive to fill anyway). With `sweep_reclaim_max_bars==1` (default):
bit-identical original same-bar rule (documented as verified inert for
XAUUSD M5 in `docs/ENTRY_FUNNEL_FINDINGS.md` — see below). With N>1: opens
an N-bar window on first sweep, any bar inside it closing back beyond the
edge confirms (returns `(True, lag_bars)`), a deeper sweep restarts the
window, an expired window resets to not-swept and bumps
`sweep_reclaim_window_expired`. Telemetry: `sweep_reclaim_entry_swept`
(episode count, not bar count), `sweep_reclaim_lag0/1/2plus_confirms`.

#### `_try_sweep_reclaim(self, order, candle, bar_index, spread, execution_source) -> bool`
Candle-replay-path sweep/reclaim entry: on confirmation, fill price =
`close + spread` (bull) or `close` (bear) — "OHLC is Bid data... a long
therefore enters at close + spread while a short enters at close"; computes
the ATR-buffered stop and fixed-RR target from that fill, opens via
`_open(...)`.

#### `_active_pending(self) -> list[PendingOrder]`
Filter helper: `[x for x in self.pending if x.active]`.

#### `_open(self, order, fill, when, bar_index, execution_source,
execution_spread, stop=None, target=None) -> PaperPosition | None`
**The single, shared fill/open path used by every execution mode.** In
order:
1. Resolve `stop`/`target` (order's own unless overridden, e.g. by
   sweep_reclaim's buffered stop).
2. Compute `wait_bars` and its **causal** percentile rank against
   `entry_age_values` history (only samples *before* this one — appended
   after computing the percentile, same no-leakage pattern as
   `DisplacementContext`).
3. **Target mode:** if `m5_liquidity_min_rr`, and the nearest same-direction
   BSL/SSL level from `fill_context` gives reward/risk >= configured `rr`,
   replaces the fixed-RR target with that liquidity level
   (`target_source = "m5_liquidity"`).
4. **Risk-fraction selection:** a long `if/elif` chain over the mutually-
   exclusive adaptive models (`liquidity_risk_sizing_enabled`,
   `choch_risk_sizing_enabled`, `combined_context_risk_sizing_enabled`,
   `displacement_risk_sizing_enabled`, `choch_displacement_risk_sizing_enabled`,
   `three_factor_risk_sizing_enabled`), each picking a `risk_fraction` from
   context (opposite sweep? opposite CHoCH at fill? displacement top
   quartile? entry-age top quartile? — 0/1/2/3-factor scoring). Then two
   **non-exclusive overrides** layered on top: `bos_three_factor_risk_enabled`
   (BOS-only 3-factor override) and `choch_split_risk_enabled` (CHoCH-only
   toxic/sweep/other 3-tier split). Then an optional additive
   `bos_m15_counter_bonus_fraction` bonus for BOS setups counter to M15,
   capped at `.0125`. Finally, if this is a CHoCH setup and
   `choch_risk_cap_fraction` is set, `risk_fraction = min(risk_fraction,
   cap)`.
5. Calls `calculate_safe_position_size(...)` with the resolved
   `risk_fraction`. If invalid: deactivates the order
   (`lifecycle_state="rejected_sizing"`), records rejection meta, bumps the
   appropriate counter, appends a `rejected_sizing` event, returns `None` —
   **no retry, no partial fill**.
6. **Final live trend recheck** (documented as a standalone safety net,
   independent of `update_m5_trend`'s cancellation, and covered explicitly
   by `test_final_trend_recheck_inside_open_is_a_standalone_safety_net`):
   `validate_trade_direction(order.direction, self.current_m5_trend)` — if
   now disallowed, deactivates the order (`lifecycle_state="rejected_trend"`),
   bumps counter, appends `trend_events`, returns `None`.
7. Otherwise: deactivates the order (`lifecycle_state="filled"`), builds the
   `PaperPosition` with an extremely rich meta dict (everything from
   `order.meta` plus wait-bars/age-percentile, execution source/spread,
   `original_stop` (needed later for breakeven-safe MAE/MFE-in-R), the
   demo-pending/paper-active execution state, target source, planned RR,
   applied risk fraction, allowed risk, CHoCH cap, context risk score, and
   every `fill_...` context key), appends to `self.positions`.

#### `_advance_lifecycle(self, candle, bar_index) -> None`
No-op unless `cfg.entry_lifecycle_enabled`. For each active pending order:
`"formed"` → `"accepted"` once price extends beyond the OB's formation-bar
extreme in the trade direction (bumps `lifecycle_accepted`); `"accepted"` →
`"armed"` once price *retraces* (close moves back against the extension)
strictly after the acceptance bar (bumps `lifecycle_armed`, records
bars-to-accept/bars-to-arm). Legacy orders lacking the needed formation
meta are skipped defensively ("do not infer readiness from incomplete
state"). **This whole feature is off by default**
(`entry_lifecycle_enabled=False`) and README notes its first historical
comparison reduced profit factor and increased drawdown — **EXPERIMENTAL,
research-only, not part of the production profile.**

#### `_apply_breakeven(self, p: PaperPosition, when: str) -> None`
No-op if `breakeven_trigger_r is None` or already armed. Computes
`distance = |entry - original_stop|` (from meta, so a later breakeven move
doesn't corrupt the reference distance); if `max_favorable >= trigger *
distance`, arms once: sets `breakeven_armed=True` in meta (and records
`breakeven_armed_time`/`breakeven_trigger_r`), and — only if moving to entry
actually **improves** the stop (`entry > stop` for bull, i.e. entry is above
the original stop, which it always is for a real trade) — sets `p.stop =
p.entry` exactly (no offset). Bumps `stats["breakeven_armed"]`, appends a
`breakeven_events` record. Called from every excursion-tracking call site in
both `process_tick` and `process_candle`, so it's identical across live,
demo-mirrored, and both backtest paths — the module docstring/tests
emphasize this shared-code guarantee explicitly.

#### `_track_excursion(self, p, low, high) -> None`
Updates `p.max_adverse`/`p.max_favorable` (raw price units) — monotonic
max, never decreases.

#### `_close(self, p, price, when, result, bar_index=None,
execution_source="unknown", execution_spread=0.0) -> Trade`
Computes `pnl = move / tick_size * tick_value * volume`; `r =
pnl / risk_money`; MAE/MFE-in-R computed against the **original** (pre-
breakeven) stop distance (comment: "after a breakeven move, entry-to-
current-stop would be zero" — so it deliberately does NOT use the live
`p.stop`). Builds the `Trade`, appends to `self.trades`, updates
`equity`/`peak_equity`/`max_drawdown`, removes the position from
`self.positions`. Trade meta includes `breakeven_armed`, `breakeven_exit`
(`breakeven_armed and result=="loss"` — i.e. a scratch-at-breakeven counted
as a loss result but flagged separately in reporting), `original_stop`,
`final_stop`, and the demo-exit-pending/paper-closed execution state.

#### `dump_state(self, index_offset=0) -> dict` / `load_state(self, data) -> None`
Full ledger persistence: pending orders (with index fields rebased),
positions, `stats`, `current_m5_trend`, entry-pivot gate state, last
confirmed pivots (index-rebased), revival-shadow state, recent spreads,
entry-age history. **Trades are explicitly NOT included in `dump_state`**
(`"trades": []`) — they're persisted separately via
`StateStore.record_trade` per-trade, and reloaded from the SQLite `trades`
table in `StateStore.load()`, "so snapshot doesn't get heavy as the forward
test grows" (per the technical guide). `load_state` has extensive
`setdefault` calls for backward-compatible loading of older snapshots
missing newer stat keys.

### Module-level helpers
`_dump_entry_pivot(pivot, index_offset) -> dict | None`: shifts
`candle_index` for persistence. `_is_after(left, right) -> bool`: ISO
datetime comparison with a string-comparison fallback on parse failure.

**Used by:** `app.py` (`PaperApp.broker`), `historical.py`,
`tick_historical.py` — the three orchestrators. Heavily unit/integration
tested (7 of the 10 test files touch `PaperBroker` directly).

---

## Module: `pine_ob_bot/historical.py`

**Purpose:** `run_historical(csv_path, cfg, initial_equity, spread,
run_label) -> dict` — replays a CSV of prior M5 Bid OHLC through the exact
same engine/broker/context classes as live, writing a full set of historical
outputs (SQLite snapshot, trades CSV, summary CSV, breakdown CSV, HTML
report) without touching the live-forward-test state/DB.

**Imports `data.loader.load_m5` from the ROOT-level legacy package** —
`from data.loader import load_m5`. This is the one place `pine_ob_bot`
crosses into the sibling legacy-bot codebase, purely to reuse its CSV
loading utility (columns `time, open, high, low, close`). **NEEDS REVIEW/
cross-project coupling**: worth a one-line callout in the docs ("this backtest
runner borrows a CSV loader from the older sibling project; it's not
otherwise related").

### `run_historical(csv_path, cfg, initial_equity=10_000.0, spread=0.20, run_label="") -> dict`
Constructs one `PaperBroker` (with a hardcoded XAUUSD-like `SymbolSpec`),
calls `broker.enable_entry_pivot_gate()`, one `PineSwingOBEngine`, one
`ClassicEntryPivotDetector`, `M15Context`, two `LiquidityTracker`s (M5/M15),
`ChochContext`, two `DisplacementContext`s (BOS/CHoCH). For each CSV row (in
order): builds a `Candle`; calls `broker.process_candle(...)` **first**
(fills/closes against this candle using the *previous* candle's structural
state — the module docstring explains the shared ordering: "trend update ->
entry pivot -> setups"); drains `rejected_sizing`/`trend_events`; feeds
M15/liquidity trackers; runs `engine.process(candle)`; `choch.process(breaks)`;
`broker.update_m5_trend(...)` (cancelling now-opposing pending orders
*before* this candle's own new OBs are considered — explicit ordering
rationale in comments); runs `entry_pivot.process(candle)` and
`broker.update_entry_pivot(...)` for confirmed pivots; tallies major
BOS/CHoCH/swing-high/swing-low counts; for each newly formed OB, computes
displacement snapshot+rank, builds the full context-snapshot `extra_meta`
dict (M15 + liquidity M5/M15 + setup-liquidity + CHoCH + displacement +
formation OHLC + swing levels) and calls `broker.add_ob(...)`; cancels
invalidated OBs; updates `broker.set_market_context(...)`.

After the loop: writes a `StateStore` snapshot to a **separate**
`historical_{label}.sqlite3` (label = CSV stem + optional run_label,
sanitized to alnum/-/_), records trades and rejection/trend events; calls
`export_reports`/`export_breakdown`/`export_html` from `reporting.py` to
`historical_{label}_*` files; builds an extensive `summary` dict (mirrors
`broker.stats` plus swing/pivot/setup counts, average entry-pivot age,
config echo) and writes it as a one-row CSV; returns the summary dict.

**Used by:** `run_pine_ob_paper.py`'s `--backtest` flag exclusively.

**Note on execution realism:** OHLC-replay fill/close ordering happens
*before* the same-candle engine/trend/OB update for that bar — i.e. a
position opened on bar N is checked for SL/TP using bar N's OHLC, but a
brand-new OB *formed* on bar N cannot be turned into a fillable order until
`add_ob` runs later in the same iteration, and even then `process_candle`
already ran for that candle — so a same-candle OB cannot fill on its own
formation candle (no look-ahead). This matches the module's explicit
"Only the fill source differs (real ticks live, OHLC replay here)" claim.

---

## Module: `pine_ob_bot/tick_historical.py`

**Purpose:** Streaming Bid/Ask tick-level backtest with M5 bar-close signal
generation — the highest-fidelity backtest mode (real intrabar tick fills
rather than OHLC approximation).

### `iter_ticks(paths: Iterable[Path])` (generator)
Reads one or more CSV (optionally `.gz`) tick files in sorted path order;
parses `time`/`bid`/`ask` columns by header index (not fixed position);
skips malformed rows, duplicate consecutive `(time,bid,ask)` tuples, and
non-positive bid/ask.

### `M5Builder` (dataclass)
Fields: `bucket`, `time`, `open`, `high`, `low`, `close`.
- `push(self, tick: Tick) -> Candle | None`: buckets ticks into 5-minute
  UTC windows using **string slicing** of the ISO timestamp
  (`tick.time[14:16]` for minute) rather than parsing a full `datetime`, for
  performance on large tick files — falls back to full `datetime.fromisoformat`
  parsing only on a slicing exception. Returns the just-closed `Candle` when
  a new bucket starts (bid-only OHLC, since MT5 tick capture here is Bid-
  based).

### `run_tick_historical(paths, cfg, initial_equity, output_root, label, warmup_bars=500) -> dict`
Same broker/engine/context construction as `run_historical`. Defines a
local `close_bar(candle)` closure mirroring `PaperApp.on_closed_candle`'s
exact ordering: `broker.process_signal_candle` (signal/lifecycle only, no
fill) → M15/liquidity → `engine.process` → `choch.process` →
`broker.update_m5_trend` → `entry_pivot.process`/`update_entry_pivot` → for
each formed OB, displacement snapshot/rank, and (only once past
`warmup_bars`) `broker.add_ob(...)` → cancel invalidated OBs →
`set_market_context`. Once `len(engine.candles) >= warmup_bars` for the
first time, **clears `broker.pending`** (`trading_started = True`) — so any
OB formed *during* warm-up that's still pending gets discarded rather than
becoming a tradeable order; this is the "bootstrap OB doesn't leak into the
month's result" guarantee mentioned in `ROBOT_TECHNICAL_GUIDE.md`.

Main loop: for each tick, `builder.push(tick)`; if a bar closed, call
`close_bar`; if `trading_started`, call `broker.process_tick(tick)`
(fills/closes); else just `broker._observe_spread(...)` (spread-median
warm-up without trading).

After the loop: `export_reports`/`export_breakdown`/`export_html` to
`tick_{safe_label}_*` files under `output_root`; builds and returns a
summary dict (ticks/bars/files/warmup + entry-pivot config +
buy/sell-setup counts + full `broker.stats`).

**No SQLite snapshot is written here** (unlike `run_historical`) — this
runner is purely for producing report files and a summary dict; state
persistence is not part of its contract.

**Used by:** `tools/run_tick_backtest.py` (not read — out of scope per task
instructions, only `run_pine_ob_paper.py` is the in-scope CLI), and directly
by `test_sweep_reclaim_tick_runner.py`'s
`test_run_tick_historical_drives_process_signal_candle` regression test.

---

## Module: `pine_ob_bot/reporting.py`

**Purpose:** All CSV/HTML report generation — trade exports, summary
statistics, diagnostic breakdowns, and a self-contained (no external JS/CSS
dependency) dark-themed HTML dashboard with an inline SVG equity curve.

### `export_daily_html(broker, day, timezone_name, path, context=None, refresh_seconds=None) -> None`
Filters `broker.trades` to those closed on the given local calendar day
(via `pandas.Timestamp` + `ZoneInfo` conversion); builds a **shallow copy**
of the broker (`copy.copy`) with `trades` replaced by just that day's slice
and `initial_equity`/`equity`/`peak_equity`/`max_drawdown` recomputed
relative to that day's starting equity (prior days' PnL rolled into the
day's starting `initial_equity`) — so the daily report shows an intraday
equity curve, not the whole-history one. Delegates to `export_html`.

### `export_daily_metrics(broker, day, timezone_name, path, extra=None) -> dict`
Computes a rich one-row-per-day KPI dict (trade count, win rate, net PnL,
total R, profit factor, BOS/CHoCH counts, context-risk-score histogram,
opposite-CHoCH-at-fill count, strong-displacement count, liquidity-sweep
count, M15 alignment counts, risk-fraction stats, tick-vs-replay entry
counts, average entry spread, demo-order count/slippage). **Upserts**: reads
existing CSV, drops any existing row for the same date, appends the new row,
re-sorts by date, rewrites the whole file — so re-running for the same day
is idempotent (no duplicate rows), and the CSV grows one column-superset
row-set over time (`extrasaction="ignore"` on write).

### `export_reports(broker, trades_path, summary_path) -> dict`
Writes every trade (flattening `meta` into top-level CSV columns) to
`trades_path`; computes and writes a compact summary (trades/wins/win_rate/
net_pnl/total_r/profit_factor/max_drawdown_pct/equity) to `summary_path`.
Returns the summary dict. This is the workhorse called by every runner.

### `export_breakdown(broker, path) -> None`
Builds long-form per-trade diagnostic rows (entry hour/weekday/month/UTC
session bucket, OB-width-in-ATR bucket, wait-bars bucket, plus all the
context flags), then **groups by ~20+ dimensions** (direction, break kind,
session, weekday, month, M15 alignment/break/zone, liquidity sweep flags,
CHoCH alignment, context risk score, target source, breakeven flags, BOS
displacement/body/range/close-location **quartile buckets** via
`pd.qcut`), writing one row per (dimension, group) pair with
trades/wins/win_rate/net_pnl/total_r/avg_r/profit_factor/avg_mae_r/
avg_mfe_r. Uses `pandas`.

### `export_html(broker, path, title, context=None, refresh_seconds=None) -> None`
Builds the full self-contained HTML dashboard: KPI cards (trades, win rate,
net PnL, total R, profit factor, max drawdown, equity), an inline SVG
equity curve (`_equity_svg`), then per-dimension grouped tables (same
dimension list philosophy as `export_breakdown`, rendered as
`pandas.DataFrame.to_html`), and a "latest 200 trades" table (most recent
first). Dark theme, responsive grid, all CSS inlined in a `<style>` block —
zero external dependencies, fully offline-viewable. Optional
`<meta http-equiv="refresh">` tag if `refresh_seconds` given.

### `_html_group(df, column) -> str`
Groups a trades DataFrame by one column, computes Trades/Win%/Net PnL/Total
R/Avg R/PF, sorted by Avg R descending, rendered as an HTML table.

### `_equity_svg(values: list[float]) -> str`
Hand-built inline SVG polyline + filled area for the equity curve — no
charting library dependency.

### `_bucket(value, limits) -> str`
Generic bucketer: given `[(limit, label), ...]` ascending, returns the first
label whose limit exceeds `value`, or `"unknown"` for `None`/NaN.

**Used by:** `historical.py`, `tick_historical.py`, `app.py` (daily +
cumulative + shutdown reports).

**Edge case:** `export_html`/`export_breakdown` both import `pandas`
lazily inside the function body (not at module top) — likely to keep
`pandas` an optional/lazy dependency for code paths that never call these
functions (**minor stylistic note, not a bug**).

---

## Module: `pine_ob_bot/report_worker.py`

**Purpose:** A tiny daemon-thread + bounded queue (`maxsize=2`) that moves
expensive CSV/HTML export work off the live trading loop, so report
generation never blocks tick processing.

### `ReportWorker`
- `__init__(self, logger)`: starts a daemon thread running `_run`.
- `submit(self, callback, *args) -> bool`: non-blocking `put_nowait`; if the
  queue is full, logs a warning and **drops** the job ("a newer snapshot
  will be attempted on the next refresh") — returns `False`.
- `close(self) -> None`: enqueues a `None` sentinel and joins the thread
  (60s timeout) — "the final shutdown report always waits" (i.e. `_save`/
  shutdown-time reports go through `submit`, but the final one is expected
  to actually complete before `close()` returns, since `close()` is called
  after the last `submit()` in `PaperApp.run`'s `finally` block... though
  note `close()` itself doesn't guarantee the *specific* last job ran if the
  queue was already full when submitted — **AMBIGUOUS edge case**: if the
  queue is saturated at shutdown, the very last report submission could
  still be silently dropped rather than guaranteed; not verified by any
  test).
- `_run(self) -> None`: pulls jobs forever; `None` sentinel exits; any
  exception from a callback is caught and logged (`self.log.exception`) so
  a bad report never kills the worker thread.

**Used by:** `app.py` (`self.report_worker`), for `_write_daily_reports`.

---

## Module: `pine_ob_bot/storage.py`

**Purpose:** SQLite-backed persistence: one atomic JSON `state` snapshot row,
an append-only `trades` table, and an append-only `events` table (audit
log of every notable decision: rejections, cancellations, breakeven arms,
demo actions, etc.).

### `StateStore`
- `__init__(self, path)`: opens SQLite with WAL journal mode; creates
  `state(key TEXT PRIMARY KEY, value TEXT)`, `trades(id TEXT PRIMARY KEY,
  payload TEXT, closed_time TEXT)`, `events(id INTEGER PRIMARY KEY
  AUTOINCREMENT, time TEXT, kind TEXT, payload TEXT)` if not already present.
- `load(self) -> dict | None`: reads the `snapshot` row (`None` if absent),
  parses JSON, then **overwrites** `payload["broker"]["trades"]` with the
  full `trades` table content (ordered by `closed_time,id`) — this is why
  `PaperBroker.dump_state` deliberately omits trades: they live in the
  `trades` table and are reattached here on load.
- `save(self, engine, broker, context=None) -> None`: serializes
  `{engine, broker, context}` as one JSON blob, upserts it as the single
  `snapshot` row (`INSERT ... ON CONFLICT DO UPDATE`) — atomic single-row
  replace.
- `record_event(self, time, kind, payload) -> None`: appends one row to
  `events`.
- `record_trade(self, trade: Trade) -> None`: `INSERT OR IGNORE` (id is
  primary key) — idempotent against replays.
- `event_counts(self, start_time, end_time) -> dict[str,int]`: `GROUP BY
  kind` count in a half-open time range — used for daily report event
  tallies.
- `close(self) -> None`.

**Used by:** `app.py` (`self.store`), `historical.py`.
`tick_historical.py` does **not** use `StateStore` at all.

---

## Module: `pine_ob_bot/app.py`

**Purpose:** `PaperApp` — the live orchestrator that wires together the MT5
feed, the structure engine, every context tracker, the broker, persistence,
reporting, and the dashboard into the actual run loop. This is the class
`run_pine_ob_paper.py` instantiates for non-`--backtest` runs.

### `PaperApp`

#### `__init__(self, cfg: BotConfig, feed: MT5ReadOnlyFeed)`
Validates config; sets up logging (console + file); opens `StateStore`;
`feed.connect()` → equity/spec; logs an MT5 account snapshot event;
constructs `PineSwingOBEngine`, `M15Context`, two `LiquidityTracker`s,
`ChochContext`, two `DisplacementContext`s, `ClassicEntryPivotDetector`,
`PaperBroker` (then `enable_entry_pivot_gate()` immediately), `MarketRecorder`,
optionally `DemoExecutor` (only if `cfg.demo_orders_enabled`), `ReportWorker`,
optionally `DashboardServer` (catches `OSError` — e.g. port in use — and
logs an error rather than crashing startup).

#### `restore_or_bootstrap(self) -> None`
If a snapshot exists: loads engine/broker state; **immediately** re-applies
`update_m5_trend` from the restored engine's trend (documented rationale:
an older snapshot predating this filter, or one with zero missed candles,
would otherwise leave the broker thinking Neutral); drains trend events;
`reconcile_entry_filters`/`reconcile_pending_targets`; loads context (M15/
liquidity/CHoCH/displacement/entry-pivot) — with **three fallback shapes**
for backward compatibility with older snapshot formats (full context dict →
partial M15-only context → no context, replaying observation context from
history in the last two cases); replays every candle strictly newer than
`engine.last_time` via `on_closed_candle(candle, replay=True)`; reconciles
demo positions; saves; logs summary counts.
If no snapshot: "warm detector without inventing historical PnL" — replays
`cfg.history_bars` of history purely to build up engine/context/pending-order
state (still calling `add_ob`, so historically-formed OBs *do* become
pending orders, just never filled/PnL'd since no tick execution runs during
bootstrap), then saves.

#### `on_closed_candle(self, candle, replay=False) -> None`
The exact per-candle orchestration order (also mirrored by the two backtest
runners): record to `MarketRecorder` (unless replaying? — actually always,
guarded only by `cfg.market_capture_enabled`, including during replay —
**minor note**: replay candles during restore also get market-recorded,
which is intentional per the recorder's own dedupe-by-last-row logic, so
no duplicate rows result) → `broker.process_signal_candle` (lifecycle/
market-index only, no fill) → drain rejected-sizing → M15/liquidity
observation update → `engine.process` → `choch.process` → `update_m5_trend`
→ `entry_pivot.process`/`update_entry_pivot` (persisted as an event) →
record each break as an event → for each formed OB: displacement snapshot/
rank, `broker.add_ob(...)` (persisted as `ob_formed` + either an info log
or an `ob_rejected` event) → cancel invalidated OBs (event) →
`set_market_context` → drain trend events → `_save()`.

#### `on_tick(self) -> None`
Dedupes identical ticks; records to `MarketRecorder`; **staleness guard**:
if the tick's age exceeds `cfg.max_live_tick_age_seconds`, ignored (debug
log only); snapshots `positions` before/after; calls `broker.process_tick`;
drains rejected-sizing/trend/breakeven events; for every trade that just
closed, if Demo is active and the original position was demo-mirrored,
calls `self.demo.close(...)`, reprices via `apply_demo_exit`, records
`demo_exit_slippage`; always records the trade and logs it. For every
*newly opened* position, if Demo is active and the position's entry came
from a tick (not replay), calls `self.demo.open(...)`; on success reprices
via `apply_demo_entry`; on **any exception**, calls
`broker.reject_open(position.id, str(exc))` to roll back the paper fill and
records `demo_open_failed`/`paper_open_reverted` events — so a rejected
live MT5 order never leaves a "ghost" paper position with no real backing.
Saves state.

#### `_drain_rejected_sizing`/`_drain_trend_events`/`_drain_breakeven_events(self, when)`
Each pops and logs+persists exactly one batch of the corresponding
`PaperBroker` event list, clearing it after. `_drain_breakeven_events`
additionally mirrors the SL move to MT5 via `self.demo.modify_stop(...)` if
the position was demo-mirrored, recording `demo_breakeven_status`/error.

#### `_reconcile_demo_positions(self) -> None`
No-op if Demo inactive. Calls `self.demo.reconcile(self.broker.positions)`
(read-only matching against live MT5 positions by ticket then comment
prefix), records the result plus per-item `missing`/`orphan` events, logs a
summary.

#### `run(self, once=False) -> None`
`restore_or_bootstrap()`; then loops: every `candle_poll_seconds`, fetches
up to 3 recent closed candles and processes any newer than `last`
(closed-candle catch-up always precedes the current-tick evaluation, per
comment); calls `on_tick()`; refreshes the daily report; if `once`, breaks
after one iteration; else sleeps `poll_seconds`. In `finally`: forces a
final daily report, closes the report worker (waiting for it to drain),
exports the shutdown `paper_report.html`/CSVs, saves, closes the store,
shuts down the feed, closes the dashboard.

#### `_refresh_daily_report(self, force=False) -> None`
Detects local-day rollover (exports the *previous* day's final report when
the day changes); otherwise refreshes on the `report_refresh_seconds`
interval or when forced.

#### `_export_daily_report(self, day) -> None`
Builds the report `context` dict (config echo + live status + event
counts); takes a **shallow** `copy.copy` of the broker with `trades`/
`positions`/`pending` explicitly re-listed (to avoid deep-copying full
history, which the comment says "can pause tick processing as the
forward-test grows"); submits the actual file-writing work
(`_write_daily_reports`) to the `ReportWorker`, including a breakdown export
only every `breakdown_refresh_seconds`.

#### `_write_daily_reports(self, broker, day, context, counts, include_breakdown) -> None`
Runs (in the worker thread) `export_daily_html`, copies it to a dated
archive file, `export_daily_metrics` (CSV upsert), `export_reports`
(cumulative trades/summary), and conditionally `export_html`
(cumulative HTML) + `export_breakdown`.

#### `_save(self) -> None`
Trims and persists engine/M15/liquidity/CHoCH/displacement/entry-pivot
state plus the broker ledger, all rebased by the trimmed `index_offset`.

#### `_process_observation_context`/`_observation_snapshot`/`_fill_context`/`_setup_liquidity_snapshot`
Small helpers wiring M15/liquidity/CHoCH snapshots into the meta dicts
attached to OBs and fills — same shape as the equivalent inline code in
`historical.py`/`tick_historical.py`, just as bound methods here.

**Used by:** `run_pine_ob_paper.py` (non-`--backtest` path) constructs
`PaperApp(cfg, MT5ReadOnlyFeed(cfg)).run(once=args.once)`.

**SAFETY-RELEVANT:** `PaperApp` itself never calls `order_send` directly,
but it *orchestrates* `DemoExecutor` (`self.demo`), which does. See the
dedicated Safety section below.

---

## Module: `pine_ob_bot/cli_risk.py`

**Purpose:** Shared `--fixed-risk`/`--choch-risk-cap` CLI flag resolution
and validation, factored out so `run_pine_ob_paper.py` and (per its own
docstring) `tools/run_tick_backtest.py` (out of scope here, not read) cannot
drift into two different interpretations of the same flags — the module
docstring explicitly references a prior bug where they *did* drift (BOS got
full `risk_fraction` while CHoCH kept the 0.5% cap even under
`--fixed-risk`, defeating the flag's purpose).

### `DEFAULT_CHOCH_RISK_CAP = 0.005`

### `ADAPTIVE_RISK_SIZING_FLAGS: dict[str,str]`
Maps each of the six adaptive risk-sizing CLI `dest` names to their flag
spelling, purely for generating a consistent error message.

### `validate_fixed_risk_args(parser, args) -> None`
No-op unless `args.fixed_risk`. Otherwise: `parser.error(...)` (raises
`SystemExit`) if any adaptive-sizing flag is set, or if `args.choch_risk_cap
is not None` (i.e. the user explicitly passed `--choch-risk-cap` alongside
`--fixed-risk`) — the conflict is never silently ignored.

### `resolve_choch_risk_cap(fixed_risk, explicit_cap) -> float | None`
`fixed_risk=True` → always `None` (uncaps CHoCH). Otherwise: `explicit_cap`
if given, else `DEFAULT_CHOCH_RISK_CAP`.

**Used by:** `run_pine_ob_paper.py`'s `main()`, called immediately after
`parser.parse_args()` and before any MT5/file I/O.

---

## Module: `pine_ob_bot/dashboard_server.py`

**Purpose:** A tiny read-only local HTTP file server (stdlib
`ThreadingHTTPServer` + `SimpleHTTPRequestHandler`) that serves the
generated report directory, plus a redirect `index.html`.

### `_QuietHandler(SimpleHTTPRequestHandler)`
Overrides `log_message` to suppress per-request console spam.

### `DashboardServer`
- `__init__(self, root, host="127.0.0.1", port=8765)`: resolves/creates the
  report directory, binds a `ThreadingHTTPServer`, computes a display URL
  (`0.0.0.0`/`::` bind normalized to `127.0.0.1` for the printed URL only),
  writes the redirect `index.html`.
- `start(self) -> None`: starts the daemon serving thread.
- `close(self) -> None`: `server.shutdown()` + `server_close()` + joins the
  thread (2s timeout).
- `_write_index(self) -> None`: writes a small static HTML page with a
  30-second meta-refresh redirect to `paper_report_latest.html`, plus links
  to the cumulative report and the metrics CSV.

**Used by:** `app.py` (`self.dashboard`), only if `cfg.dashboard_enabled`.

**SAFETY-RELEVANT:** this is a real, bindable HTTP server. In the
educational lab, it must never actually be started (it's read-only for
*files* but is still a live network listener) — see Safety section.

---

## Module: `pine_ob_bot/market_recorder.py`

**Purpose:** Append-only CSV capture of every distinct live tick and closed
M5 candle, for later offline tick-backtest reproduction / audit.

### `MarketRecorder`
- `__init__(self, root, symbol)`: sanitizes `symbol` to alnum/-/_; on
  construction, scans existing files to recover `last_tick`/
  `last_candle_time` (so a restart doesn't duplicate the last row) by
  reading only the **last ~8KB** of the relevant file (`_last_row`) rather
  than the whole file — cheap dedupe state recovery even for very large
  capture files.
- `record_tick(self, tick) -> bool`: no-op (`False`) if identical to the
  last recorded tick; else appends `(time, bid, ask, spread)` to a
  **daily** file `ticks_{symbol}_{YYYYMMDD}.csv`, flushing after every
  write.
- `record_candle(self, candle) -> bool`: no-op if same timestamp as last;
  else appends to a single **cumulative** file `candles_{symbol}_M5.csv`
  (not split by day, unlike ticks).
- `_append(path, header, row)` (static): writes the header only on first
  creation, then the row, with an explicit `handle.flush()` after every row
  (durability over a possible crash, at some I/O cost).
- `_last_row(path)` (static): tail-read helper described above.

**Used by:** `app.py` (`self.recorder`), gated by `cfg.market_capture_enabled`.

**SAFETY-RELEVANT (mild):** writes files to disk continuously; not a network
risk, but the interactive lab must not let this accumulate unbounded files
if any live-adjacent demo were ever attempted (it should not be — see
Safety section, this module is describable but must never actually run
against a live feed in the lab).

---

## Module: `pine_ob_bot/mt5_feed.py` — **LIVE/MT5, DESCRIBE-ONLY**

**Purpose:** The only read-side bridge to the MetaTrader5 terminal. Its own
docstring: "Read-only MT5 adapter. Deliberately exposes no order_send
operation."

### `MT5ReadOnlyFeed`
- `__init__(self, cfg)`: imports `MetaTrader5` (raises a clear
  `RuntimeError` with an install hint if missing — this package is Windows/
  MT5-terminal-only and will not import in a generic sandbox).
- `connect(self) -> tuple[float, SymbolSpec]`: resolves credentials, calls
  `mt5.initialize(...)`, fetches account info, resolves the actual tradable
  symbol name (exact match, or first symbol containing `"XAU"` as a
  fallback for broker-specific suffixes), fetches `symbol_info` and builds
  a `SymbolSpec` from it. Returns `(account.equity, spec)`.
- `_credentials(self) -> dict`: prefers `MT5_LOGIN`/`MT5_PASSWORD`/
  `MT5_SERVER`/`MT5_TERMINAL_PATH` env vars; falls back to importing
  `config.settings.MT5` from the **root-level legacy package**
  (`from config import settings`) if env vars are absent — another
  deliberate cross-project coupling, guarded by a broad
  `except (ImportError, KeyError, TypeError, ValueError): pass`.
- `_resolve_symbol(self, wanted) -> str`: exact-name select, else scans all
  broker symbols for one containing `"XAU"`.
- `closed_candles(self, count) -> list[Candle]`: retries progressively
  smaller `copy_rates_from_pos` windows (terminal history caps vary),
  requires at least `min(202, size)` bars to accept a window (enough for
  `atr_period=200` warm-up); **drops the last returned bar** (`rates[:-1]`)
  since it's still-forming/live; dedupes by integer epoch time.
- `tick(self) -> Tick | None`: reads `symbol_info_tick`; rejects non-positive
  bid/ask; prefers `time_msc` (millisecond) over `time` (second) precision.
- `shutdown(self) -> None`: `mt5.shutdown()`.

**SAFETY-RELEVANT — MUST NEVER BE EXECUTED by the interactive lab.** This
module requires a live MT5 terminal connection and real credentials; it is
purely descriptive material for the documentation, never an executable demo
target.

---

## Module: `pine_ob_bot/demo_executor.py` — **LIVE/DEMO ORDER-SEND, DESCRIBE-ONLY**

**Purpose:** `DemoExecutor` — the **only** class in the entire package that
calls `mt5.order_send`. Mirrors tick-opened paper positions into a connected
MT5 **demo** account, guarded at construction time against any non-demo
account.

### `DemoExecutor`
- `__init__(self, mt5, symbol, magic, deviation_points,
  max_free_margin_fraction=0.80)`: **hard refuses** to construct unless
  `account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO`, the terminal is
  connected and `trade_allowed`, and the account itself is `trade_allowed`
  — raises `RuntimeError` otherwise. This is the project's core "never
  trade real money" guard rail, enforced structurally (no live account can
  ever construct this object) rather than by convention alone.
- `open(self, position: PaperPosition) -> dict`: idempotent (`already_sent`
  short-circuit); builds a `TRADE_ACTION_DEAL` market request with SL/TP
  from the paper position; **fits the volume to available margin** via
  `_fit_open_request` (see below) before sending; requires success via
  `_require_success`; records ticket/price/slippage/retcode into the
  position's meta.
- `reconcile(self, paper_positions) -> dict`: **read-only** — matches
  MT5-managed positions (filtered by `magic`) against the paper ledger by
  ticket then comment prefix (`pineob:{id[:12]}`); returns
  `matched`/`missing` (paper positions with no live counterpart, flagged
  `reconciliation_required_demo_missing`) /`orphans` (live positions with no
  paper counterpart) — never creates or closes anything itself.
- `modify_stop(self, position, new_stop) -> dict`: sends
  `TRADE_ACTION_SLTP` with the **unchanged TP re-sent** (comment explains:
  MT5's SLTP action replaces both levels, so omitting TP would clear it) —
  this mirrors `PaperBroker._apply_breakeven`'s stop-only move.
- `close(self, position_id, direction, ticket, volume) -> dict`: if the
  position is already gone from MT5 (already closed by broker-side SL/TP),
  looks up the closing deal from history instead of erroring; otherwise
  sends an opposite-side `TRADE_ACTION_DEAL` to close.
- `_fit_open_request(self, request) -> tuple[dict, float|None]`: starts at
  the paper-calculated volume (capped to `volume_max`, floored to
  `volume_step`), then **decrements by one step at a time** until
  `order_calc_margin(...) <= max_free_margin_fraction * free_margin` **and**
  `order_check(...)` succeeds, or gives up below `volume_min` (raises
  `RuntimeError`). Documented and tested as **narrowing only** — it never
  recomputes risk or raises volume, matching `position_sizing.py`'s own
  "never bump toward minimum" invariant end-to-end across Paper→Demo.
- `_find_exit_deal`/`_find_position`: lookup helpers by ticket then
  magic+comment.
- `_require_success(self, result, action) -> None`: raises `RuntimeError`
  if `result is None` or `retcode` isn't in the accepted done/done-partial
  set.

**SAFETY-RELEVANT — MUST NEVER BE EXECUTED by the interactive lab, under
any circumstance.** This is the one place in the entire codebase capable of
sending a real order to a broker (even if restricted to demo accounts by
construction). The documentation lab may describe its request-building
logic, show code excerpts, and even unit-test it against the **fake MT5**
objects already used in `test_breakeven.py`/`test_paper_broker_sizing.py`
(those are safe — pure Python fakes, no real MT5 module, no network), but
must never import the real `MetaTrader5` package or attempt a live/demo
connection.

---

## Trading concept separation

Strictly grounded in code — file/class/function references:

- **M5 Trend detection** ("Trend Swing"): `pine_engine.py`,
  `PineSwingOBEngine._update_swing` (delayed-pivot leg detection) +
  `PineSwingOBEngine.process` (break→`self.trend` assignment, 1/-1/0). The
  single derived trend value is consumed everywhere via
  `trend_filter.trend_from_engine_state(engine.trend)` and applied to the
  broker via `PaperBroker.update_m5_trend`.

- **Swing detection:** two independent implementations exist, deliberately
  separated:
  - Major/Trend Swing: `PineSwingOBEngine._update_swing` (window =
    `cfg.trend_swing_length`, symmetric via same-length look-back/look-
    forward, sets `self.swing_high`/`self.swing_low`).
  - Local/Entry-timing swing (Entry Pivot): `entry_pivot.py`,
    `ClassicEntryPivotDetector.process` (asymmetric `left_bars`/
    `right_bars` allowed, strict `>`/`<`, never touches Trend State).
  - A **third**, independent swing-liquidity detector also exists for
    reporting only: `liquidity_context.py`,
    `LiquidityTracker._confirm_swing` (own `swing_length` window, own
    `leg` state, creates `LiquidityPool` objects, never touches trend or
    entries).

- **Entry Pivot:** `entry_pivot.py` (`ClassicEntryPivotDetector`,
  `EntryPivot`), gating logic in `paper.py`,
  `PaperBroker.add_ob` (steps under "Entry Pivot gate" above) and
  `PaperBroker.enable_entry_pivot_gate`.

- **Order Block:** construction in `pine_engine.py`,
  `PineSwingOBEngine._make_ob` (called from within `.process` on each
  confirmed break); data shape in `models.py`, `OrderBlock`; admission into
  a tradeable order in `paper.py`, `PaperBroker.add_ob`; invalidation in
  `pine_engine.py`, `PineSwingOBEngine.process` (the "OB invalidation" step)
  feeding `paper.py`, `PaperBroker.cancel_ob`.

- **BOS:** `pine_engine.py`, `PineSwingOBEngine.process` — a break is `"BOS"`
  when it's in the *same* direction as the current `self.trend` (or trend is
  still 0/neutral). `StructureBreak.kind == "BOS"`.

- **CHoCH:** same location — a break is `"CHoCH"` when it's *opposite* the
  current `self.trend`. No separate "strong vs weak CHoCH" **type** exists
  in `StructureBreak`/`OrderBlock` (`break_kind` is just `"BOS"`/`"CHoCH"`/
  `"unknown"`). However, a **derived** strong/weak distinction is applied at
  admission time only: `paper.py`, `PaperBroker.add_ob`, the
  `cfg.strong_choch_only` branch — a CHoCH-formed OB is rejected
  (`rejected_weak_choch`) unless its `extra_meta` carries
  `bos_displacement_top_quartile == True`, which is computed by
  `structure_context.py`'s `DisplacementContext.observe` (top-quartile
  causal percentile rank of the break candle's close-through-ATR strength,
  fed the CHoCH-specific `choch_displacement` ranker instance in each
  runner). So "strong CHoCH" = CHoCH + top-quartile displacement; "weak
  CHoCH" = CHoCH without it. This is purely an *admission filter*, not a
  field stored on the `OrderBlock`/`StructureBreak` themselves.

- **Sweep and Reclaim:** `paper.py`, `PaperBroker._sweep_reclaim_window`
  (shared core), `PaperBroker._try_sweep_reclaim` (candle-replay entry),
  `PaperBroker.process_signal_candle` (live closed-candle confirmation) +
  `PaperBroker.process_tick` (the sweep_reclaim branch that fills on the
  next tick after confirmation). Gated by `cfg.entry_mode ==
  "sweep_reclaim"`.

- **Pending Order:** data shape `models.py`, `PendingOrder`; lifecycle
  entirely inside `paper.py` (`PaperBroker.add_ob` creates it,
  `PaperBroker.update_m5_trend`/`cancel_ob`/`_advance_lifecycle`/`_open`
  transition it — full state machine below).

- **Fill:** `paper.py`, `PaperBroker._open` (the single shared fill path
  for every mode), invoked from `process_tick`, `process_candle`,
  `_try_sweep_reclaim`.

- **Stop Loss:** set at OB formation (`OrderBlock.stop` property, copied
  into `PendingOrder.stop`/`PaperPosition.stop`), optionally widened by the
  ATR buffer in sweep_reclaim mode (`_sweep_reclaim_window`/
  `_try_sweep_reclaim`/`process_signal_candle`), optionally moved to
  breakeven by `PaperBroker._apply_breakeven`. Checked in
  `process_tick`/`process_candle` (`_close(..., "loss")`).

- **Take Profit:** computed in `PaperBroker.add_ob` (`fixed_rr` default) and
  possibly overridden in `PaperBroker._open` (`m5_liquidity_min_rr` target
  mode, reading `LiquidityTracker` snapshot data). Checked alongside SL in
  `process_tick`/`process_candle` (`_close(..., "win")`).

- **Risk Sizing:** `position_sizing.py`,
  `calculate_safe_position_size` (the deterministic lot-sizing math);
  risk-*fraction selection* (which % to risk) lives in `paper.py`,
  `PaperBroker._open`'s long if/elif model-selection chain, driven by
  `BotConfig`'s risk-sizing-model flags.

- **Breakeven:** `paper.py`, `PaperBroker._apply_breakeven`, driven by
  `cfg.breakeven_trigger_r`; called from every excursion-tracking site in
  `process_tick`/`process_candle`.

- **Backtest:** `historical.py` (`run_historical`, OHLC-driven) and
  `tick_historical.py` (`run_tick_historical`, tick-driven).

- **Paper Trading:** `paper.py`'s `PaperBroker` as orchestrated live by
  `app.py`'s `PaperApp` (tick execution against a real live feed, but a
  purely in-memory/SQLite ledger — no real order ever sent).

- **Demo Execution:** `demo_executor.py`'s `DemoExecutor`, invoked only from
  `app.py`'s `PaperApp.on_tick`/`_drain_breakeven_events`/
  `_reconcile_demo_positions`, only when `cfg.demo_orders_enabled`.

---

## Full trade lifecycle

Traced end-to-end for the **live** path (`app.py`), the most complete
orchestration; historical/tick-replay paths follow the identical
engine→trend→pivot→OB ordering (explicit in both modules' docstrings/
comments) with only the fill mechanism differing. Call order below is
**explicit in code** except where marked inferred.

1. **Candle ingestion (live):** `mt5_feed.py`,
   `MT5ReadOnlyFeed.closed_candles`/`.tick()` supply `Candle`/`Tick` objects.
   `app.py`, `PaperApp.run` polls closed candles every `candle_poll_seconds`
   and calls `on_closed_candle` for each new one, then calls `on_tick()`
   every loop iteration (tick catch-up always follows candle catch-up,
   explicit).

2. **Per closed candle** (`PaperApp.on_closed_candle`, explicit order in
   code):
   a. `MarketRecorder.record_candle` (capture).
   b. `PaperBroker.process_signal_candle` — advances `market_index`,
      the optional lifecycle state machine, and (in sweep_reclaim mode)
      checks pending orders for sweep+reclaim confirmation.
   c. Drain rejected-sizing events (from any earlier tick-driven `_open`
      call).
   d. M15/liquidity observation update (`_process_observation_context`).
   e. **Structure detection:** `PineSwingOBEngine.process(candle)` — TR/ATR,
      volatility-parsed highs/lows, `_update_swing` (Trend Swing pivot),
      close-based break detection (BOS/CHoCH), `_make_ob` (Order Block
      construction), OB invalidation check. Returns `(breaks, formed_obs,
      invalidated_ids)`.
   f. `ChochContext.process(breaks)` — records the CHoCH event if any.
   g. **Trend update:** `PaperBroker.update_m5_trend(trend_from_engine_state
      (engine.trend), candle.time)` — cancels any now-opposing pending
      order (transitions `PendingOrder.lifecycle_state` to
      `"cancelled_trend"`). Explicitly must run before new same-candle OBs
      become orders.
   h. **Entry Pivot:** `ClassicEntryPivotDetector.process(candle)` — any
      newly confirmed pivot is passed to
      `PaperBroker.update_entry_pivot(pivot)` (records
      `last_pivot_high`/`last_pivot_low`; persisted as an event).
   i. **Order Block admission:** for each `formed` OB,
      `structure_context.displacement_snapshot` +
      `DisplacementContext.observe` build displacement metadata; M15/
      liquidity/CHoCH snapshots are merged in; `PaperBroker.add_ob(ob,
      extra_meta)` runs the full admission funnel (break-kind filter,
      strong-CHoCH filter, trend gate, Entry Pivot gate) and either creates
      a `PendingOrder` (`lifecycle_state` starts `"armed"` by default, or
      `"formed"` if `entry_lifecycle_enabled`) or rejects it (event logged).
   j. Invalidated OBs → `PaperBroker.cancel_ob(ob_id)` (deactivates the
      matching pending order, `lifecycle_state="invalidated"`).
   k. `PaperBroker.set_market_context(...)` — snapshot for later fill-time
      context lookups.
   l. Drain trend events; `_save()` (persist state).

3. **Per tick** (`PaperApp.on_tick`, explicit order):
   a. Dedupe/staleness checks (`max_live_tick_age_seconds`).
   b. `MarketRecorder.record_tick`.
   c. `PaperBroker.process_tick(tick)`:
      - If any position open: track excursion, apply breakeven, check
        SL/TP, close via `_close` if hit (**this candle's/tick's fill
        evaluation for new orders is skipped entirely while any position is
        open** — see NEEDS REVIEW in the `paper.py` section).
      - Else: for each active `"armed"` pending order (oldest first,
        respecting `min_entry_wait_bars`): plain-limit mode checks price
        reaching `order.entry`; sweep_reclaim mode requires
        `sweep_reclaim_confirmed` (set in step 2b) and computes a fresh
        target from the live fill price; either way, on a fill condition,
        calls `PaperBroker._open(...)`.
   d. **Fill (`PaperBroker._open`)**, in order: resolve stop/target →
      compute wait-bars percentile → resolve target mode
      (fixed_rr/liquidity) → **select risk fraction** (adaptive model
      chain) → `calculate_safe_position_size` (Risk Sizing,
      `position_sizing.py`) → reject on invalid sizing (order deactivated,
      `lifecycle_state="rejected_sizing"`) → **final live trend recheck**
      (`validate_trade_direction`) → reject on mismatch
      (`lifecycle_state="rejected_trend"`) → otherwise deactivate the order
      (`lifecycle_state="filled"`) and create a `PaperPosition`.
   e. Back in `on_tick`: for newly opened positions, if Demo mode and the
      fill came from a live tick, `DemoExecutor.open(...)` sends the real
      demo order; on failure, `PaperBroker.reject_open(...)` rolls the
      paper fill back entirely.
   f. Every subsequent tick: excursion tracking (`_track_excursion`) and
      **Breakeven** (`PaperBroker._apply_breakeven`) run before the SL/TP
      check, so a breakeven arm and an SL/TP hit can occur within the same
      `process_tick` call (arm first, since `_apply_breakeven` is called
      before the stop/target comparison in the code).
   g. **Close:** SL or TP hit → `PaperBroker._close(...)` — computes PnL/R,
      MAE/MFE-in-R (against the *original* pre-breakeven stop distance),
      builds a `Trade`, updates equity/drawdown, removes the position. If
      Demo-mirrored, `DemoExecutor.close(...)` is called and
      `PaperBroker.apply_demo_exit` reprices the trade from the real fill.
   h. `StateStore.record_trade`/`record_event`; log; `_save()`.

4. **Reporting (asynchronous, off the trading loop):**
   `PaperApp._refresh_daily_report` → `_export_daily_report` (builds a
   shallow broker snapshot + context) → `ReportWorker.submit(
   _write_daily_reports, ...)` → (in the worker thread)
   `reporting.export_daily_html`/`export_daily_metrics`/`export_reports`/
   optionally `export_html`+`export_breakdown`. At shutdown,
   `PaperApp.run`'s `finally` block forces a final report, closes the
   worker (waiting for the queue to drain), writes the final
   `paper_report.html`/CSVs, and closes the SQLite store, feed, and
   dashboard.

**Inference note:** the exact interleaving of steps 2 and 3 across multiple
loop iterations (i.e., "how many ticks are processed between two candle
closes") is runtime-dependent (real wall-clock polling), not something the
code fixes deterministically — only the *ordering guarantee* ("closed-candle
catch-up always precedes the current tick's fill evaluation," explicit
comment in `PaperApp.run`) is code-guaranteed. The historical/tick-replay
runners reproduce the same *per-candle* ordering deterministically since
they're not wall-clock-driven.

---

## Order Block lifecycle state machine

Grounded in `pine_engine.py` (`PineSwingOBEngine`) for creation/invalidation,
and `paper.py` (`PaperBroker`) for admission consequences. `OrderBlock`
itself only has one boolean field (`active`), not a rich state enum — the
richer state lives one level up, on the derived `PendingOrder`.

States (as tracked by `OrderBlock.active` plus admission outcome):

1. **Formed** — created inside `PineSwingOBEngine.process` (via
   `_make_ob`) the instant a BOS/CHoCH break is confirmed on a closed
   candle. `active=True` at creation. Inserted at the front of
   `engine.order_blocks`.
2. **Active** — `active=True`, sitting in `engine.order_blocks`, eligible
   both for future invalidation-checking and (once, on its formation
   candle) for a single `PaperBroker.add_ob(...)` admission attempt.
3. **Admitted vs Rejected** (a `PaperBroker`-side branch, not an
   `OrderBlock` field): `add_ob` either creates a `PendingOrder` (see next
   state machine) or returns `None` — the `OrderBlock.active` flag itself
   is unaffected by admission rejection; a rejected-at-admission OB simply
   has no corresponding pending order, but the OB itself remains `active`
   in the engine and *could* still be invalidated later like any other
   active OB (it just never got a chance to trade).
4. **Invalidated** — inside `PineSwingOBEngine.process`, on every
   subsequent candle, if still `active` and price crosses the OB's stop
   edge (`bear`: `candle.high > ob.high`; `bull`: `candle.low < ob.low`),
   sets `active=False` and returns its id in the `invalidated` list. This
   propagates to `PaperBroker.cancel_ob(ob_id)`, which deactivates the
   matching `PendingOrder` (`lifecycle_state="invalidated"`) if one exists.
5. **Evicted (resource cap)** — `engine.order_blocks =
   engine.order_blocks[:100]` truncates the list to the 100 most recent OBs
   every `process()` call, regardless of `active` state (see TECH DEBT note
   in the `pine_engine.py` section — an old but still-active OB could in
   principle be silently dropped from the engine's own bookkeeping if 100+
   newer OBs have formed since; its corresponding `PendingOrder`, if any,
   is unaffected by this eviction since `PaperBroker.pending` is a
   completely separate list).

There is no "filled"/"closed" state on `OrderBlock` itself — that
information lives entirely on the derived `PendingOrder`/`PaperPosition`/
`Trade` chain.

---

## Pending Order lifecycle state machine

Grounded entirely in `paper.py`. The real state field is
`PendingOrder.lifecycle_state: str`, observed values (exact strings from
code, not inferred): `"formed"`, `"accepted"`, `"armed"`, `"invalidated"`,
`"cancelled_trend"`, `"rejected_sizing"`, `"rejected_trend"`, `"filled"`.
(`"rejected_sizing"` is set via `_open`'s meta but the code also assigns the
literal `order.lifecycle_state = "rejected_sizing"`; confirmed in
`paper.py` line ~904.)

1. **Creation → `"armed"` (default) or `"formed"` (if
   `cfg.entry_lifecycle_enabled`)** — `PaperBroker.add_ob`, after passing
   every admission filter (break-kind, strong-CHoCH, trend gate, Entry
   Pivot gate). This is the only creation path for a `PendingOrder`.

2. **`"formed"` → `"accepted"`** (only reachable if the optional lifecycle
   feature is on) — `PaperBroker._advance_lifecycle`, triggered when a
   later closed candle's price extends beyond the OB's own formation-bar
   extreme in the trade direction. Condition: `candle.high > formation_high`
   (bull) or `candle.low < formation_low` (bear), where `formation_high`/
   `formation_low` were captured in `add_ob`'s meta at creation time.

3. **`"accepted"` → `"armed"`** (lifecycle feature only) — same function,
   triggered when a subsequent candle's close *retraces* against the
   extension direction, strictly after the acceptance bar
   (`bar_index > order.accepted_index`). Condition:
   `candle.close < last_close` (bull) or `candle.close > last_close` (bear).

4. **`"armed"` → `"filled"`** — the only path to a live position. Reached
   via `PaperBroker._open`, called from `process_tick` (limit or
   sweep_reclaim live tick fill), `process_candle` (OHLC replay limit or
   sweep_reclaim fill), or `_try_sweep_reclaim` (candle-replay
   sweep_reclaim). Precondition for `sweep_reclaim` mode specifically:
   `order.meta["sweep_reclaim_confirmed"]` must already be `True`, set by
   `_sweep_reclaim_window` (invoked from `process_signal_candle` live, or
   directly from `_try_sweep_reclaim`/`process_candle` in replay).

5. **`"armed"` → `"invalidated"`** — triggered two ways: (a)
   `PaperBroker.cancel_ob(ob_id)` when the parent `OrderBlock` is
   invalidated by the structure engine; (b) `PaperBroker.reconcile_entry_filters`
   on restart if the order's `break_kind` is no longer in
   `cfg.allowed_break_kinds`.

6. **Any active state → `"cancelled_trend"`** — `PaperBroker.update_m5_trend`,
   whenever the M5 trend changes to something incompatible with the order's
   direction (including to Neutral). Sets `order.meta["cancellation_reason"]`
   to either `ORDER_CANCELLED_M5_TREND_CHANGED` or
   `ORDER_CANCELLED_M5_TREND_NEUTRAL`.

7. **Any active state → `"rejected_sizing"`** — inside `PaperBroker._open`,
   when `calculate_safe_position_size` returns `is_valid=False` (either
   `CALCULATED_VOLUME_BELOW_BROKER_MINIMUM` or
   `ACTUAL_RISK_EXCEEDS_ALLOWED_RISK`).

8. **Any active state → `"rejected_trend"`** — inside `PaperBroker._open`,
   the final live trend recheck (`validate_trade_direction`) fails even
   though the order wasn't cancelled by `update_m5_trend` first (the
   "standalone safety net" case — e.g. `current_m5_trend` mutated directly
   in tests, or in principle any code path that skips `update_m5_trend`).

9. **Implicit terminal "invalid" (no explicit lifecycle_state change but
   `active=False`)** — inside `process_tick`, if price has already moved
   past the order's stop before ever reaching entry (`invalid = px <=
   order.stop` for bull, mirrored for bear), the order is deactivated
   (`order.active = False`) **without** a `lifecycle_state` reassignment —
   it silently stays at whatever state it was in (typically `"armed"`).
   **AMBIGUOUS/minor inconsistency**: this is the one deactivation path
   that doesn't also update `lifecycle_state` to a matching terminal label,
   unlike every other rejection/cancellation path. Similarly, inside
   `process_signal_candle`'s and `_sweep_reclaim_window`'s "distance <= 0"
   defensive check in the tick sweep_reclaim branch
   (`if distance <= 0: order.active = False; continue`), no
   `lifecycle_state` update occurs either. Worth flagging to documentation
   readers as "not every deactivation path also sets a matching
   `lifecycle_state` label — `order.active` is the authoritative
   'still tradeable' flag; `lifecycle_state` is a best-effort descriptive
   label, not fully exhaustive."

Note `active: bool` is the single authoritative gate everywhere
(`_active_pending()` filters purely on `active`, not on
`lifecycle_state`) — `lifecycle_state` is a richer, mostly-but-not-fully
consistent descriptive label layered on top for reporting/debugging.

---

## Test-to-feature map

All 10 files live in `pine_ob_bot/tests/`.

### `test_trend_filter.py`
**Verifies:** the standalone `trend_filter.py` pure functions
(`is_trade_allowed_by_m5_trend`, `validate_trade_direction`,
`trend_from_engine_state`) in isolation, no `PaperBroker` involved.
**A/A/A:** Arrange — a `TrendDirection` value and a side string; Act — call
the function; Assert — boolean/`TrendValidationResult` fields match the
documented truth table (buy+bullish=allow, sell+bearish=allow, mismatches
and neutral=reject, unrecognized side=reject, buy/sell spelling aliases
work). **Why it matters:** this is the single gate every order-creation/fill
path relies on; a regression here would silently let trades through in the
wrong direction or block all trades. **Breaking it signals:** the core
buy/sell vs bullish/bearish truth table itself is wrong — the most
foundational safety property in the whole system.

### `test_paper_broker_trend.py`
**Verifies:** the trend gate wired into the real `PaperBroker`, using
`update_m5_trend` directly (not the real engine) to drive scenarios:
order creation allowed/rejected by trend at creation time; pending-order
cancellation on a trend flip; the *fill-time* recheck across every fill
path (`process_tick` limit, `process_candle` limit, sweep_reclaim via both
candle and tick). **A/A/A:** Arrange a broker + a bull/bear OB; Act
`update_m5_trend`/`add_ob`/`process_tick`/`process_candle`; Assert
`order.active`, `lifecycle_state`, rejection reason, and (for the fill-path
tests) that no position/trade is ever created despite price reaching the
entry. **Why it matters:** proves the trend gate is enforced not just at
creation but redundantly at every possible fill entry point — "defense in
depth." **Breaking it signals:** a specific fill path (tick vs candle vs
sweep_reclaim) has stopped checking trend before opening a position — a
trade could open counter to the M5 trend.

### `test_trend_engine_integration.py`
**Verifies:** the trend gate driven by the **real** `PineSwingOBEngine`
(not hand-set), on a hand-verified 9-bar sequence producing Neutral→bullish
BOS→bearish CHoCH; also verifies live-style (`process_signal_candle` +
`process_tick`) and historical-style (`process_candle`) orchestration reach
identical trend state/stats for the same candle sequence. **A/A/A:** Arrange
the 9 fixed OHLC bars; Act feed them through `engine.process` +
`update_m5_trend` (+ `add_ob` at bars 7/8); Assert the trend value at each
bar index, the cancellation of a pending buy on the CHoCH flip, admission of
the new bear OB, rejection of a fresh buy attempt post-flip, and that both
orchestration styles produce identical final stats. **Why it matters:** this
is the only test connecting the *real* structure engine's BOS/CHoCH output
to the trend gate end-to-end, and the only one proving live vs historical
orchestration genuinely agree. **Breaking it signals:** either the engine's
break-classification (BOS vs CHoCH) changed, or live/historical
orchestration order diverged from each other.

### `test_position_sizing.py`
**Verifies:** `calculate_safe_position_size` in complete isolation — every
rejection reason, the floor-never-round behavior, the volume_max cap, and
the specific "equity 10k / risk 1% / distance 1000" regression scenario that
motivated the whole module (explicitly contrasts against the "old buggy
behavior" of bumping to volume_min). **A/A/A:** Arrange numeric inputs; Act
call the function; Assert `PositionSizeResult` fields (`is_valid`,
`rejection_reason`, `volume`, `actual_risk` vs `allowed_risk`). **Why it
matters:** this is the single hard invariant "never risk more than
configured" for the whole trading system. **Breaking it signals:** realized
risk could silently exceed the user's configured percentage — the most
financially dangerous possible regression.

### `test_paper_broker_sizing.py`
**Verifies:** that `PaperBroker._open` actually **uses**
`calculate_safe_position_size` (not a parallel/duplicate calculation), that
sizing rejection leaves no position/pending-order residue, and that
`DemoExecutor._fit_open_request` never diverges from what Paper already
computed (via fake MT5 objects, no real MT5 dependency). **A/A/A:** Arrange
a broker + pending order sized to be below broker minimum, or normally
sized; Act call `_open` directly; Assert `position is None`/matches
`calculate_safe_position_size`'s own independent computation, and (in the
Demo test) that `_fit_open_request`'s output volume equals Paper's volume
exactly. **Why it matters:** proves Paper and Demo can never silently
diverge on risk — a core cross-cutting safety guarantee spanning two
modules. **Breaking it signals:** `PaperBroker._open` started computing
sizing differently from the shared function, or Demo's margin-fitting logic
started resizing beyond narrowing.

### `test_breakeven.py`
**Verifies:** `_apply_breakeven`'s one-time-arm-no-backward-move behavior
across both the tick path and the candle-replay path (shared code
guarantee), the exact report fields it stamps (`breakeven_armed`,
`breakeven_armed_time`, `breakeven_exit`, `original_stop`, `final_stop`),
that a win after arming is *not* flagged as a breakeven exit, bear-direction
mirroring, the disabled-by-default no-op case, and that Demo's
`modify_stop` sends `TRADE_ACTION_SLTP` with TP re-sent unchanged (via a
fake MT5). **A/A/A:** Arrange a broker with `breakeven_trigger_r` set +
an opened position; Act feed ticks/candles crossing the trigger then
retracing to entry or continuing to target; Assert stop value, meta fields,
trade result, and (Demo test) the exact MT5 request contents. **Why it
matters:** breakeven is a stop-mutation feature — any bug here directly
changes realized PnL and could move a stop the wrong way or re-arm
repeatedly. **Breaking it signals:** the stop moved backward, re-armed, or
desynced between the two fill paths, or a Demo SL-modify request dropped
the TP.

### `test_entry_pivot_freshness.py`
**Verifies:** the `max_entry_pivot_age_bars` gate inside `add_ob`: inclusive
boundary, age measured from confirmation bar (not candidate bar), no
fallback to an older pivot once a newer one of the same kind exists,
unconfirmed-pivot-acts-as-missing (no lookahead), `None` reproduces the
legacy "ever seen" behavior exactly, dump/load round-trip keeps age causally
correct across an index-offset trim, and telemetry (`entry_pivot_age_samples`
/`sum_bars`/`max_bars`) accumulates correctly including the zero-sample
division-by-zero-avoidance case. Also directly tests
`ClassicEntryPivotDetector` asymmetric left/right timing.
**A/A/A:** Arrange a broker + a manually-fed `EntryPivot`; Act call `add_ob`
with OBs at varying `formed_index` offsets; Assert acceptance/rejection and
the exact recorded age. **Why it matters:** this is a not-fully-covered-
elsewhere feature (off by default, `max_entry_pivot_age_bars=None`) that
changes admission behavior subtly; the inclusive-boundary and no-fallback
rules are easy to get backwards. **Breaking it signals:** stale setups
would either be silently admitted (risk of trading an outdated pivot) or
fresh setups wrongly rejected.

### `test_sweep_reclaim_window.py`
**Verifies:** `_sweep_reclaim_window` (the shared N-bar sweep/reclaim state
machine) in isolation via `process_signal_candle`: default `N=1` is
bit-identical to the original same-bar rule (proven via a 400-bar random
walk compared against a hand-written same-bar predicate), `N=2` correctly
confirms a 1-bar-lagged gap-return, window expiry, bear-direction mirroring
with the correct ATR-buffered stop value, and the "no sweep at all" no-op
case. **A/A/A:** Arrange a broker in `sweep_reclaim` mode with a given
`sweep_reclaim_max_bars`; Act feed a sequence of candles; Assert
`order.meta["sweep_reclaim_confirmed"]`/`lag_bars` and the relevant stats
counters. **Why it matters:** the experimental multi-bar window must be
byte-for-byte inert at its default setting (`N=1`) — this is the test that
actually proves that claim, which `docs/ENTRY_FUNNEL_FINDINGS.md` cites as
empirically "INERT" on real data too. **Breaking it signals:** either the
default window stopped matching the original same-bar rule (a silent
behavior change for every existing production run), or the N>1 experimental
path miscounts lag/confirms.

### `test_sweep_reclaim_tick_runner.py`
**Verifies:** the full sweep_reclaim path across the tick execution
boundary: `process_signal_candle` confirms + buffers the stop, but the
**position only opens on the next tick** (never on the confirming candle
itself); buy/sell mirrored fill-price rules (Ask for buy, Bid for sell); a
candle with no entry-edge penetration never confirms; a sweep without a
reclaim close doesn't confirm; a candle that also breaks the *stop* is
cancelled by the OB engine's own invalidation logic rather than being
allowed to trade (regression test for "the self-defeating stop-based
sweep"); the buffer is applied exactly once even if a second qualifying
candle arrives; and — critically — `run_tick_historical` itself is spied on
to confirm it drives state through the real `PaperBroker.process_signal_candle`
entry point (not a hand-rolled bypass). **A/A/A:** Arrange a sweep_reclaim
broker/order; Act feed a confirming candle then a tick; Assert
`sweep_reclaim_confirmed`, the buffered stop value, zero positions after the
candle, exactly one position with the correct entry/stop/target after the
tick. **Why it matters:** this is the regression suite for a documented
past bug class (stop-based sweeps being tradeable, and the tick runner
silently bypassing the real confirmation entry point) — both are safety-
relevant correctness properties, not just features. **Breaking it signals:**
either sweep_reclaim starts filling on the wrong price/side, filling on the
confirming candle itself (a look-ahead bug), or the tick runner reverts to
a bypass that skips the shared confirmation logic.

### `test_revival_shadow_audit.py`
**Verifies:** the `revival_shadow_audit` **EXPERIMENTAL, observation-only**
feature end-to-end: flag-off means zero shadow state and unchanged real
cancellation; a full suspend→trend-return→fresh-sweep→reclaim→shadow-fill→
shadow-win cycle produces the exact expected hypothetical stop/fill/R values
while the **real** order stays cancelled throughout; a reclaim that happens
*before* the trend actually returns never counts; only one revival attempt
is tracked per OB (a second trend-loss ends tracking permanently); OB death
(explicit `cancel_ob`) ends tracking; a return after more than 12 bars is
rejected; the 3/6/12-bar lag buckets are attributed correctly; state
survives a `dump_state`/`load_state` round-trip. **A/A/A:** Arrange a broker
with `revival_shadow_audit=True` + a pre-cancelled order; Act feed
`process_signal_candle` sequences simulating trend return and price action;
Assert `revival_shadow`/`shadow_positions` contents and the associated stats
counters, and always additionally assert the real `order.active`/
`lifecycle_state` never changed from the cancellation. **Why it matters:**
proves an experimental audit feature genuinely cannot leak into real trading
decisions — the assertion pattern in nearly every test explicitly re-checks
the real order/position state is untouched. **Breaking it signals:** either
the shadow-audit math is wrong (misleading research conclusions), or worse,
the audit somehow started influencing real order state (a containment
failure for an explicitly "never trades" feature).

**Module dependency graph** for these tests (imports, common pattern: each
tries `pine_ob_bot.X` first, falls back to bare `X` with a `sys.path`
insert for "pytest invoked from inside pine_ob_bot/"): all ten import from
`pine_ob_bot.config`, most from `pine_ob_bot.models`/`pine_ob_bot.paper`/
`pine_ob_bot.trend_filter`; `test_position_sizing.py` imports only
`pine_ob_bot.position_sizing`; `test_trend_filter.py` imports only
`pine_ob_bot.trend_filter`; `test_trend_engine_integration.py` additionally
imports `pine_ob_bot.pine_engine`; `test_sweep_reclaim_tick_runner.py`
additionally imports `pine_ob_bot.tick_historical`; `test_breakeven.py` and
`test_paper_broker_sizing.py` additionally import
`pine_ob_bot.demo_executor`; `test_entry_pivot_freshness.py` additionally
imports `pine_ob_bot.entry_pivot`.

---

## Module dependency graph

Text adjacency list (`A -> B` means "A imports from B"), restricted to
intra-`pine_ob_bot` imports plus the two documented cross-project imports.
Compiled from every module's actual `import`/`from` statements read above.

```
__init__.py         -> config, models, pine_engine, paper

config.py            -> (no internal deps; stdlib only)

models.py             -> (no internal deps; stdlib only)

trend_filter.py       -> (no internal deps; stdlib only)

entry_pivot.py         -> models

structure_context.py    -> models

liquidity_context.py     -> models

position_sizing.py        -> (no internal deps; stdlib only)

pine_engine.py              -> config, models

mtf_context.py                -> config, models, pine_engine

paper.py                        -> config, entry_pivot, models,
                                    position_sizing, trend_filter

historical.py                     -> config, entry_pivot, models,
                                      mtf_context, liquidity_context, paper,
                                      pine_engine, reporting, storage,
                                      structure_context, trend_filter
                                      + EXTERNAL: data.loader (root legacy pkg)

tick_historical.py                 -> config, entry_pivot, liquidity_context,
                                       models, mtf_context, paper,
                                       pine_engine, reporting,
                                       structure_context, trend_filter

reporting.py                        -> paper   (only for type hints /
                                                 attribute access, no other
                                                 pine_ob_bot import)

report_worker.py                     -> (no internal deps; stdlib only)

storage.py                            -> models

cli_risk.py                            -> (no internal deps; stdlib
                                            argparse only)

dashboard_server.py                     -> (no internal deps; stdlib only)

market_recorder.py                       -> models

mt5_feed.py                               -> config, models, paper
                                             (SymbolSpec only)
                                             + EXTERNAL: MetaTrader5 (3rd
                                               party), config.settings
                                               (root legacy pkg, optional
                                               fallback)

demo_executor.py                           -> models   (PaperPosition only)

app.py                                      -> config, dashboard_server,
                                                demo_executor, entry_pivot,
                                                models, mt5_feed,
                                                mtf_context, liquidity_context,
                                                market_recorder, paper,
                                                pine_engine, reporting,
                                                report_worker, storage,
                                                structure_context,
                                                trend_filter

run_pine_ob_paper.py (root)                  -> pine_ob_bot.app,
                                                 pine_ob_bot.cli_risk,
                                                 pine_ob_bot.config,
                                                 pine_ob_bot.mt5_feed,
                                                 pine_ob_bot.historical
```

Leaf modules with zero internal `pine_ob_bot` dependencies:
`config.py`, `models.py`, `trend_filter.py`, `position_sizing.py`,
`report_worker.py`, `cli_risk.py`, `dashboard_server.py`. These are the
safest/simplest starting points for an educational reading order.

`app.py` is the most connected module (13 internal imports) — the
orchestration hub. `paper.py` is the most *depended-upon* module by other
orchestrators (`historical.py`, `tick_historical.py`, `app.py`, `mt5_feed.py`
for `SymbolSpec`, `demo_executor.py`'s tests) — the execution core.

---

## CLI / entrypoints

`run_pine_ob_paper.py` (root-level script). Never calls `order_send`
directly (only reachable transitively through `--demo-orders` →
`DemoExecutor`, which has its own hard demo-account guard).

### `build_parser() -> argparse.ArgumentParser`
Flags (all confirmed present in code, grouped by purpose):

- `--once`: connect, catch up, export, and exit (single iteration of the
  live loop instead of running forever).
- `--symbol` (default `"XAUUSD"`).
- `--trend-swing-length` (int, default `None` → resolved to 12) and its
  deprecated alias `--swing-length` (int, default `None`) — see
  `resolve_swing_settings` below for the reconciliation rule.
- `--entry-pivot-left`/`--entry-pivot-right` (int, default 5 each).
- `--max-entry-pivot-age N` (int, default `None` = no age limit).
- `--db` (Path, default `None` → auto-generated from swing settings).
- `--backtest CSV` (Path) — **switches the whole run into
  `run_historical(...)` mode** instead of live `PaperApp`.
- `--initial-equity` (float, default 10,000.0).
- `--spread` (float, default 0.20) — historical-only fixed spread.
- `--rr` (float, default 1.5).
- `--target-mode` (`fixed_rr` | `m5_liquidity_min_rr`, default `fixed_rr`).
- `--breakeven-at-r R` (float, default `None` = disabled).
- `--max-positions` (int, default 1).
- Six adaptive risk-sizing switches (mutually exclusive, enforced by
  `BotConfig.validate`): `--liquidity-risk-sizing`, `--choch-risk-sizing`,
  `--combined-context-risk-sizing`, `--displacement-risk-sizing`,
  `--choch-displacement-risk-sizing`, `--three-factor-risk-sizing`.
- `--choch-risk-cap FRACTION` (float, default `None` — distinguishes
  "not given" from "given as 0.005"; resolved via `cli_risk.resolve_choch_risk_cap`).
- `--fixed-risk` (flag) — see `cli_risk.py`; cannot combine with any
  adaptive-sizing flag or explicit `--choch-risk-cap`.
- `--no-market-capture` (disables `MarketRecorder`).
- `--demo-orders` (enables `DemoExecutor` mirroring; refuses non-demo MT5
  accounts at construction).
- `--no-dashboard`, `--dashboard-host`, `--dashboard-port`.
- `--bos-only` (flag) and `--include-choch` (flag) and `--strong-choch-only`
  (flag) — together determine `allowed_break_kinds`: default (neither
  `--include-choch` nor `--strong-choch-only`, or `--bos-only` given) is
  `("BOS",)`; `--include-choch` or `--strong-choch-only` (and not
  `--bos-only`) → `("BOS", "CHoCH")`.
- `--min-entry-wait` (int, default 0).
- `--entry-mode` (`limit` | `sweep_reclaim`, default `limit`).
- `--sweep-atr-buffer` (float, default 0.20).
- `--sweep-reclaim-max-bars N` (int, default 1) — **EXPERIMENTAL** flag for
  `sweep_reclaim_max_bars`.
- `--revival-shadow-audit` (flag) — **EXPERIMENTAL**, observation-only.
- `--run-label` (str, default `""`) — suffix for `--backtest` output files.
- `--lifecycle` (flag) — **EXPERIMENTAL**, enables `entry_lifecycle_enabled`.

### `resolve_swing_settings(args, parser) -> (trend_swing_length,
entry_pivot_left, entry_pivot_right)`
Reconciles `--trend-swing-length` vs deprecated `--swing-length`: if both
given and different → `parser.error(...)` (hard stop); if both given and
equal → allowed; if only one given → that value; if neither → `12`.
Additionally validates `trend_swing_length >= 2`,
`entry_pivot_left/right >= 1`, erroring via `parser.error` otherwise.

### `build_config(args, trend_swing_length, db_path) -> BotConfig`
Maps CLI args onto `BotConfig` fields. Notably: the default adaptive risk
model is `choch_displacement_risk_sizing_enabled=True` **unless**
`--fixed-risk` or any *other* adaptive flag is explicitly given — i.e. if
the user gives none of the six risk-sizing flags and doesn't pass
`--fixed-risk`, the production default silently becomes
"CHoCH+Displacement adaptive sizing," matching the documented "default
runner profile" in `README.md`/`ROBOT_TECHNICAL_GUIDE.md`.
`choch_risk_cap_fraction` comes from `cli_risk.resolve_choch_risk_cap`, not
directly from `args.choch_risk_cap`.

### `main(argv=None) -> int`
`parser.parse_args()` → `validate_fixed_risk_args(parser, args)`
(immediately, before any other work) → `resolve_swing_settings` →
`build_config` → prints trend/pivot confirmation delays and the resolved DB
path → if `--backtest` given: calls `run_historical(...)` and prints every
summary key/value, returns 0; else: constructs
`PaperApp(cfg, MT5ReadOnlyFeed(cfg)).run(once=args.once)`. Catches
`KeyboardInterrupt` (clean exit code 0) and any other `Exception` (prints to
stderr, exit code 1).

**How it wires together config/broker/historical/reporting:** `main()` is
the only place that constructs `BotConfig`; it hands that single config
object to either `run_historical` (which internally builds its own
`PaperBroker`/`PineSwingOBEngine`/etc. and calls `reporting.py` functions
directly) or to `PaperApp` (which does the same construction internally,
plus MT5 connectivity, and calls `reporting.py` functions via its own
`ReportWorker`-mediated methods). There is no direct
`run_pine_ob_paper.py -> paper.py` import — the coupling is entirely
mediated through `app.py`/`historical.py`.

---

## Config reference (`BotConfig`, `config.py`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `symbol` | str | `"XAUUSD"` | Traded instrument symbol. |
| `timeframe` | str | `"M5"` | Must be `"M5"` (validated). |
| `timeframe_minutes` | int | `5` | Must be `5` (validated). |
| `trend_swing_length` | int | `12` | Bars each side for Trend Swing (Major BOS/CHoCH/Trend State) confirmation. |
| `entry_pivot_left` | int | `5` | Left window for the classic Entry Pivot. |
| `entry_pivot_right` | int | `5` | Right window (= confirmation delay) for the Entry Pivot. |
| `swing_length` | InitVar/property | `None` | Deprecated alias for `trend_swing_length`; read-only property after init. |
| `atr_period` | int | `200` | ATR lookback for the Trend Swing engine. |
| `m15_swing_length` | int | `10` | Swing window for the M15 observation context. |
| `m15_atr_period` | int | `200` | ATR lookback for the M15 observation context. |
| `rr` | float | `1.5` | Reward:risk multiple for `fixed_rr` targets. |
| `target_mode` | str | `"fixed_rr"` | `"fixed_rr"` or `"m5_liquidity_min_rr"`. |
| `risk_fraction` | float | `0.01` | Base/default per-trade risk fraction of equity. |
| `liquidity_risk_sizing_enabled` | bool | `False` | Enables the liquidity-sweep adaptive risk model. |
| `choch_risk_sizing_enabled` | bool | `False` | Enables the opposite-CHoCH adaptive risk model. |
| `choch_opposite_risk_fraction` | float | `0.0125` | Risk % when CHoCH model detects opposite CHoCH at fill. |
| `choch_base_risk_fraction` | float | `0.0075` | Risk % otherwise, CHoCH model. |
| `combined_context_risk_sizing_enabled` | bool | `False` | Enables the combined liquidity+CHoCH 0/1/2-signal model. |
| `combined_two_signal_risk_fraction` | float | `0.0125` | Risk % at combined score 2. |
| `combined_one_signal_risk_fraction` | float | `0.01` | Risk % at combined score 1. |
| `combined_zero_signal_risk_fraction` | float | `0.0075` | Risk % at combined score 0. |
| `displacement_risk_sizing_enabled` | bool | `False` | Enables the BOS-displacement-rank adaptive model. |
| `displacement_rank_window` | int | `100` | Rolling sample window for displacement percentile. |
| `displacement_rank_min_samples` | int | `20` | Minimum samples before a percentile is computed. |
| `displacement_top_risk_fraction` | float | `0.0125` | Risk % at top-quartile displacement. |
| `displacement_base_risk_fraction` | float | `0.0075` | Risk % otherwise. |
| `choch_displacement_risk_sizing_enabled` | bool | `False` | Enables the combined CHoCH+displacement model (the CLI's implicit default). |
| `choch_risk_cap_fraction` | float\|None | `None` | Hard ceiling on risk % for CHoCH-sourced setups (CLI default resolves this to 0.005). |
| `three_factor_risk_sizing_enabled` | bool | `False` | Enables the 3-factor (CHoCH+displacement+age) scoring model. |
| `bos_three_factor_risk_enabled` | bool | `False` | BOS-only override applying the same 3-factor scoring. |
| `choch_split_risk_enabled` | bool | `False` | CHoCH-only override: toxic/sweep/other 3-tier split. |
| `choch_toxic_risk_fraction` | float | `0.0025` | Risk % for "toxic" CHoCH (old age + strong displacement). |
| `choch_sweep_risk_fraction` | float | `0.0055` | Risk % for CHoCH with a setup-liquidity sweep. |
| `choch_other_risk_fraction` | float | `0.0045` | Risk % for CHoCH otherwise. |
| `bos_m15_counter_bonus_fraction` | float | `0.0` | Additive risk bonus for BOS setups counter to M15 trend (capped at 1.25%). |
| `entry_age_rank_window` | int | `100` | Rolling window for wait-bars percentile. |
| `entry_age_rank_min_samples` | int | `20` | Minimum samples before entry-age percentile computed. |
| `liquidity_risk_fraction` | float | `0.0125` | Risk % (liquidity model) with a setup sweep. |
| `base_risk_fraction` | float | `0.0075` | Risk % (liquidity model) without a sweep. |
| `max_open_positions` | int | `1` | Position capacity (see NEEDS REVIEW re: actual multi-position support). |
| `allowed_break_kinds` | tuple[str,...] | `("BOS",)` | Which break kinds may become tradeable setups. |
| `strong_choch_only` | bool | `False` | Require top-quartile displacement for any admitted CHoCH. |
| `min_entry_wait_bars` | int | `0` | Minimum closed bars between OB formation and eligible fill. |
| `entry_mode` | str | `"limit"` | `"limit"` or `"sweep_reclaim"`. |
| `sweep_reclaim_atr_buffer` | float | `0.20` | ATR multiple added to the stop on sweep_reclaim confirmation. |
| `sweep_reclaim_max_bars` | int | `1` | EXPERIMENTAL sweep-to-reclaim lag window (1 = original same-bar rule). |
| `revival_shadow_audit` | bool | `False` | EXPERIMENTAL observation-only trend-cancellation revival audit. |
| `max_entry_pivot_age_bars` | int\|None | `None` | Max age (bars, from pivot confirmation) for an Entry Pivot to still admit setups. |
| `breakeven_trigger_r` | float\|None | `None` | R-multiple of MFE that triggers a one-time breakeven stop move. |
| `entry_lifecycle_enabled` | bool | `False` | EXPERIMENTAL formed→accepted→armed OB lifecycle gate. |
| `poll_seconds` | float | `1.0` | Live tick polling interval. |
| `fallback_spread` | float | `0.20` | Spread used where live Bid/Ask isn't directly available (OHLC replay). |
| `history_bars` | int | `5000` | Bars fetched for bootstrap/catch-up. |
| `state_history_bars` | int | `2000` | Bars retained in a persisted snapshot. |
| `db_path` | Path | `pine_ob_bot_data/paper_swing12.sqlite3` | SQLite state DB. |
| `trades_csv` | Path | `pine_ob_bot_data/trades_swing12.csv` | Shutdown trades export path. |
| `summary_csv` | Path | `pine_ob_bot_data/summary_swing12.csv` | Shutdown summary export path. |
| `log_path` | Path | `pine_ob_bot_data/paper_swing12.log` | Log file path. |
| `market_capture_enabled` | bool | `True` | Enables tick/candle CSV capture. |
| `market_capture_dir` | Path | `pine_ob_bot_data/market_capture` | Capture output directory. |
| `max_live_tick_age_seconds` | float | `30.0` | Ticks older than this are ignored live. |
| `entry_spread_guard_enabled` | bool | `True` | Enables the adaptive spread ceiling on entries. |
| `max_entry_spread_atr_fraction` | float | `0.10` | Spread ceiling as a fraction of ATR at OB formation. |
| `spread_median_window` | int | `300` | Rolling recent-spread sample size. |
| `spread_median_min_samples` | int | `20` | Minimum samples before the median-based ceiling applies. |
| `spread_median_multiplier` | float | `3.0` | Median-spread ceiling multiplier. |
| `daily_reports_dir` | Path | `pine_ob_bot_data/daily_reports` | Daily/cumulative report output directory. |
| `report_timezone` | str | `"Asia/Tehran"` | Local timezone for daily report day boundaries. |
| `report_refresh_seconds` | float | `300.0` | Daily HTML/metrics refresh interval. |
| `breakdown_refresh_seconds` | float | `900.0` | Diagnostic breakdown refresh interval (must be >= report refresh). |
| `candle_poll_seconds` | float | `5.0` | Live closed-candle polling interval. |
| `dashboard_enabled` | bool | `True` | Enables the local HTTP dashboard. |
| `dashboard_host` | str | `"127.0.0.1"` | Dashboard bind host. |
| `dashboard_port` | int | `8765` | Dashboard bind port. |
| `demo_orders_enabled` | bool | `False` | Enables `DemoExecutor` mirroring (guarded to demo accounts only). |
| `demo_magic` | int | `120512` | MT5 magic number for demo orders. |
| `demo_deviation_points` | int | `100` | MT5 price-deviation tolerance for demo orders. |
| `demo_max_free_margin_fraction` | float | `0.80` | Max fraction of free margin `DemoExecutor` may commit. |

---

## Overall architecture summary

`pine_ob_bot` is organized in five loose layers. **Data ingestion** sits at
the edges: `mt5_feed.py` (live, read-only) and `historical.py`/
`tick_historical.py`'s own CSV loaders feed `Candle`/`Tick` objects inward;
nothing downstream cares whether the data came from a live terminal or a
file. **Structure/context detection** is the analytical middle layer —
`pine_engine.py` (`PineSwingOBEngine`, the sole source of Trend Swing,
Major BOS/CHoCH, and Order Blocks), `entry_pivot.py` (independent local
timing pivots), `trend_filter.py` (the pure trend-direction gate function),
and three purely observational context trackers
(`mtf_context.py`, `liquidity_context.py`, `structure_context.py`) that
enrich every setup/fill with metadata but never themselves accept or reject
a trade. **Decision/execution** is concentrated almost entirely in one
class: `paper.py`'s `PaperBroker`, which owns OB admission (trend + Entry
Pivot gates), the Pending Order lifecycle, all fill logic across three
execution modes (tick, OHLC replay, sweep-reclaim), every risk-sizing model,
breakeven, and position close/PnL — `position_sizing.py` is its one
extracted, independently-hardened dependency for the actual lot-size math.
**Orchestration/backtest/live** wraps that decision core into three parallel
runners that share the exact same engine/broker/context classes in the same
call order: `app.py`'s `PaperApp` (live, tick-driven, MT5-connected, with
persistence/dashboard/demo-mirroring), `historical.py`'s `run_historical`
(OHLC CSV replay), and `tick_historical.py`'s `run_tick_historical`
(streaming Bid/Ask tick replay) — the project's core architectural claim,
stated explicitly in multiple docstrings, is that these three orchestrators
never diverge on structural/trend/admission decisions, only on fill
fidelity. **Persistence/reporting/CLI** is the outer shell:
`storage.py` (SQLite snapshot+events+trades), `report_worker.py`
(background export thread), `reporting.py` (CSV/HTML generation),
`dashboard_server.py` (local read-only HTTP viewer), `market_recorder.py`
(raw tick/candle capture), `cli_risk.py` (shared CLI flag resolution), and
the root `run_pine_ob_paper.py` script that ties a `BotConfig` to either
`PaperApp` or `run_historical`.

The one class capable of real-world side effects — sending an actual order —
is `demo_executor.py`'s `DemoExecutor`, and it is structurally isolated:
constructible only against a confirmed MT5 **demo** account, invoked only
from `app.py`'s live tick-processing path, and never touched by either
backtest runner. Every other module, including the entire `paper.py`
execution core, is pure in-process simulation with no network or order-
placement capability of its own.

---

## Safety-relevant modules

These modules must **never be executed** by the later interactive
documentation lab — only described, quoted, and (where a safe fake-object
test pattern already exists, as in `test_breakeven.py`/
`test_paper_broker_sizing.py`'s `_FakeMT5` classes) exercised against pure
Python fakes, never against a real `MetaTrader5` import or network
connection:

1. **`pine_ob_bot/mt5_feed.py`** — `MT5ReadOnlyFeed`. Imports the real
   `MetaTrader5` package, requires a Windows MT5 terminal and real
   credentials (`MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER` env vars, or a
   fallback into the sibling legacy project's `config.settings.MT5`). Even
   though it is read-only (no `order_send`), it establishes a real terminal
   session and reads real account/tick data — not appropriate for an
   interactive lab under any circumstance.

2. **`pine_ob_bot/demo_executor.py`** — `DemoExecutor`. The **only** class
   anywhere in the package that calls `mt5.order_send` /
   `mt5.order_check` / `mt5.order_calc_margin`. Guarded at construction to
   refuse non-demo accounts, but still capable of placing/modifying/closing
   real (demo-account) broker positions if ever connected. Must never be
   instantiated against a real `MetaTrader5` module in the lab; safe to
   demonstrate only via the fake-MT5 object pattern already used in the
   existing test suite.

3. **`pine_ob_bot/app.py`** — `PaperApp`, specifically its live paths:
   `__init__` (calls `feed.connect()`, i.e. requires MT5), `restore_or_bootstrap`,
   `on_closed_candle`, `on_tick`, `run`. These all assume a live
   `MT5ReadOnlyFeed` and, when `cfg.demo_orders_enabled`, a live
   `DemoExecutor`. The class itself never calls `order_send` directly, but
   it is the orchestration path that *can* reach `DemoExecutor.open/close/
   modify_stop` — must be described only (e.g. walk through its method
   bodies as text/diagrams), never actually instantiated/run in the lab.

4. **`pine_ob_bot/dashboard_server.py`** — `DashboardServer`. Binds and
   serves a real (if localhost-default) HTTP server thread. Even though it
   only serves static report files, an interactive lab should not bind
   arbitrary network listeners either — describe its behavior, do not start
   it.

5. **`pine_ob_bot/mt5_feed.py`'s indirect dependency on the sibling legacy
   package** (`config.settings.MT5`) and **`historical.py`'s dependency on
   `data.loader.load_m5`** are not themselves unsafe, but they are the two
   points where `pine_ob_bot` reaches outside its own package boundary —
   worth a footnote so a reader doesn't assume the package is fully
   self-contained.

By contrast, **safe to actually run** in an interactive lab: everything in
`models.py`, `config.py`, `trend_filter.py`, `entry_pivot.py`,
`structure_context.py`, `liquidity_context.py`, `mtf_context.py`,
`position_sizing.py`, `pine_engine.py`, `paper.py` (the full `PaperBroker`
simulation — it has no MT5/network dependency at all), `historical.py`/
`tick_historical.py` (given a small synthetic CSV/tick file, not the
project's real large data files), `reporting.py`, `storage.py` (local
SQLite file only), `report_worker.py`, `market_recorder.py` (local CSV file
only), and `cli_risk.py`. This aligns with `PLAN.md`'s own stated intent to
build small synthetic datasets under `runtime_data/` for the interactive
experiments rather than reusing the project's real market data or touching
MT5 in any way.
