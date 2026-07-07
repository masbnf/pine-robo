from __future__ import annotations

import tempfile
import unittest
import ast
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

from pine_ob_bot.config import BotConfig
from pine_ob_bot.dashboard_server import DashboardServer
from pine_ob_bot.demo_executor import DemoExecutor
from pine_ob_bot.models import Candle, OrderBlock, PaperPosition, PendingOrder, StructureBreak, Tick
from pine_ob_bot.mtf_context import M15Context
from pine_ob_bot.liquidity_context import LiquidityTracker
from pine_ob_bot.market_recorder import MarketRecorder
from pine_ob_bot.paper import PaperBroker, SymbolSpec
from pine_ob_bot.trend_filter import TrendDirection
from pine_ob_bot.pine_engine import PineSwingOBEngine
from pine_ob_bot.report_worker import ReportWorker
from pine_ob_bot.storage import StateStore
from pine_ob_bot.structure_context import ChochContext, DisplacementContext, displacement_snapshot
from pine_ob_bot.tick_historical import M5Builder
from tools.fetch_mt5_ticks import month_bounds, range_exists


class DocumentationSyncTests(unittest.TestCase):
    def test_core_source_matches_acknowledged_technical_guide(self):
        root = Path(__file__).resolve().parents[1]
        manifest_path = root / "docs" / "source_manifest.json"
        self.assertTrue(manifest_path.exists(), "run tools/update_docs_manifest.py --confirm-guide-updated")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue((root / manifest["guide"]).exists())
        stale = []
        for name, item in manifest["files"].items():
            digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
            if digest != item["sha256"]:
                stale.append(f"{name} -> {item['guide_section']}")
        self.assertFalse(stale, "Update the guide and acknowledge it:\n" + "\n".join(stale))


def candle(i, o, h, low, c):
    return Candle(f"2026-01-01T00:{i:02d}:00+00:00", o, h, low, c)


class PineEngineTests(unittest.TestCase):
    def test_monthly_tick_ranges_are_half_open_and_detect_complete_cache(self):
        start, end = month_bounds("2024-02")
        self.assertEqual(start.isoformat(), "2024-02-01T00:00:00+00:00")
        self.assertEqual(end.isoformat(), "2024-03-01T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            days = []
            cursor = start
            while cursor < end:
                name = f"ticks_XAUUSD_{cursor:%Y%m%d}.csv.gz"
                (root / name).touch()
                days.append({"date": cursor.date().isoformat(), "ticks": 0,
                             "status": "downloaded", "file": name})
                cursor += __import__("datetime").timedelta(days=1)
            (root / "coverage.json").write_text(json.dumps({
                "complete_month": True, "range_start": start.isoformat(),
                "range_end_exclusive": end.isoformat(), "days": days}), encoding="utf-8")
            self.assertTrue(range_exists(root, start, end))
            (root / days[-1]["file"]).unlink()
            self.assertFalse(range_exists(root, start, end))

    def test_tick_builder_closes_bid_m5_without_lookahead(self):
        builder = M5Builder()
        self.assertIsNone(builder.push(Tick("2026-01-01T00:00:01+00:00", 100, 100.2)))
        self.assertIsNone(builder.push(Tick("2026-01-01T00:04:59+00:00", 102, 102.2)))
        closed = builder.push(Tick("2026-01-01T00:05:00+00:00", 101, 101.2))
        self.assertEqual((closed.open, closed.high, closed.low, closed.close),
                         (100, 102, 100, 102))
        self.assertEqual(closed.time, "2026-01-01T00:00:00+00:00")

    def test_delayed_candle_cannot_close_position_opened_in_its_future(self):
        broker = PaperBroker(BotConfig(entry_spread_guard_enabled=False), 10_000)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.pending.append(PendingOrder("o", "ob", "bull", 100, 99, 101.5, 1,
                                           "2026-01-01T00:00:00+00:00",
                                           lifecycle_state="armed"))
        broker.market_index = 2
        broker.process_tick(Tick("2026-01-01T00:10:01+00:00", 99.9, 100.0))
        closed = broker.process_candle(
            Candle("2026-01-01T00:05:00+00:00", 100, 102, 98, 101), 2, .2)
        self.assertEqual(closed, [])
        self.assertEqual(len(broker.positions), 1)

    def test_abnormal_spread_blocks_tick_entry(self):
        cfg = BotConfig(spread_median_min_samples=2, spread_median_window=3)
        broker = PaperBroker(cfg, 10_000)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.recent_spreads = [.2, .2]
        ob = OrderBlock("ob", "bull", 100, 98, 0, "t", 1, "t",
                        atr_at_formation=2)
        broker.add_ob(ob)
        broker.market_index = 2
        broker.process_tick(Tick("2026-01-01T00:10:00+00:00", 82, 99))
        self.assertFalse(broker.positions)
        self.assertEqual(broker.stats["spread_entry_blocked"], 1)

    def test_reject_open_rolls_back_paper_position(self):
        broker = PaperBroker(BotConfig(entry_spread_guard_enabled=False), 10_000)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.pending.append(PendingOrder("o", "ob", "bull", 100, 99, 101.5, 1, "t",
                                           lifecycle_state="armed"))
        broker.market_index = 2
        broker.process_tick(Tick("2026-01-01T00:10:00+00:00", 99.9, 100.0))
        position_id = broker.positions[0].id
        rejected = broker.reject_open(position_id, "No money")
        self.assertEqual(rejected.id, position_id)
        self.assertFalse(broker.positions)
        self.assertEqual(broker.stats["demo_entry_reverted"], 1)

    def test_displacement_rank_uses_past_values_only(self):
        context = DisplacementContext(window=4, min_samples=2)
        self.assertIsNone(context.observe(1.0)["bos_close_through_percentile"])
        self.assertIsNone(context.observe(2.0)["bos_close_through_percentile"])
        self.assertEqual(context.observe(3.0)["bos_close_through_percentile"], 1.0)
        self.assertEqual(context.observe(.5)["bos_close_through_percentile"], 0.0)

    def test_displacement_snapshot_is_atr_normalized(self):
        bar = candle(5, 100, 104, 99, 103)
        ob = OrderBlock("d", "bull", 101, 99, 1, "t1", 5, bar.time,
                        break_kind="BOS", atr_at_formation=2)
        br = StructureBreak("bull", "BOS", 102, 5, bar.time)
        result = displacement_snapshot(bar, ob, [br])
        self.assertAlmostEqual(result["bos_body_atr"], 1.5)
        self.assertAlmostEqual(result["bos_range_atr"], 2.5)
        self.assertAlmostEqual(result["bos_body_to_range"], .6)
        self.assertAlmostEqual(result["bos_close_through_atr"], .5)
        self.assertAlmostEqual(result["bos_close_location"], .8)
        self.assertTrue(result["bos_directional_body"])

    def test_choch_context_is_causal_observation_only_and_restorable(self):
        context = ChochContext()
        context.process([StructureBreak("bull", "BOS", 100, 5, "t5")])
        self.assertEqual(context.snapshot("bull", 6)["m5_last_choch_alignment"], "none")
        context.process([StructureBreak("bear", "CHoCH", 99, 7, "t7")])
        snap = context.snapshot("bull", 10)
        self.assertEqual(snap["m5_last_choch_alignment"], "opposite")
        self.assertEqual(snap["m5_last_choch_age_bars"], 3)
        restored = ChochContext()
        restored.load_state(context.dump_state())
        self.assertEqual(restored.snapshot("bear", 11)["m5_last_choch_alignment"], "aligned")

    def test_swing_break_forms_bullish_ob(self):
        cfg = BotConfig(swing_length=2, atr_period=20)
        engine = PineSwingOBEngine(cfg)
        bars = [candle(0, 9, 10, 8, 9), candle(1, 10, 12, 9, 11),
                candle(2, 10.5, 11, 10, 10.5), candle(3, 10, 10.5, 9.5, 9.8),
                candle(4, 10, 13.5, 9.6, 13)]
        result = [engine.process(x) for x in bars]
        breaks, formed, _ = result[-1]
        self.assertEqual((breaks[0].direction, breaks[0].kind), ("bull", "BOS"))
        self.assertEqual((formed[0].high, formed[0].low), (12, 9))

    def test_atr_uses_wilder_smoothing(self):
        engine = PineSwingOBEngine(BotConfig(swing_length=2, atr_period=2))
        engine.process(candle(0, 9, 10, 9, 9.5))
        engine.process(candle(1, 9.5, 10.5, 9.5, 10))
        engine.process(candle(2, 10, 15, 9, 11))
        self.assertAlmostEqual(engine.atr_values[1], 1.0)
        self.assertAlmostEqual(engine.atr_values[2], 3.5)
        self.assertEqual(engine.parsed_highs[2], 15)

    def test_m15_context_only_flushes_after_three_closed_m5_bars(self):
        context = M15Context(BotConfig(m15_swing_length=2, m15_atr_period=2))
        bars = [Candle("2026-01-01T00:00:00+00:00", 10, 11, 9, 10),
                Candle("2026-01-01T00:05:00+00:00", 10, 12, 10, 11),
                Candle("2026-01-01T00:10:00+00:00", 11, 13, 8, 12)]
        context.process_m5(bars[0]); context.process_m5(bars[1])
        self.assertEqual(len(context.engine.candles), 0)
        context.process_m5(bars[2])
        self.assertEqual(len(context.engine.candles), 1)
        self.assertEqual((context.engine.candles[0].open, context.engine.candles[0].close), (10, 12))

    def test_liquidity_pool_is_confirmed_then_swept_without_lookahead(self):
        tracker = LiquidityTracker(swing_length=2, atr_period=2)
        bars = [candle(0, 9, 10, 8, 9), candle(1, 10, 12, 9, 11),
                candle(2, 10.5, 11, 10, 10.5), candle(3, 10, 10.5, 9.5, 9.8)]
        for bar in bars[:3]:
            tracker.process(bar)
        self.assertFalse(any(x.kind == "BSL" for x in tracker.pools))
        tracker.process(bars[3])
        self.assertTrue(any(x.kind == "BSL" and x.state == "active" for x in tracker.pools))
        tracker.process(candle(4, 11, 13, 10, 11))
        snap = tracker.snapshot("bear", 11, "liq_m5")
        self.assertEqual(snap["liq_m5_opposite_last_event"], "swept")
        self.assertTrue(snap["liq_m5_opposite_sweep_seen"])
        related = tracker.setup_snapshot("bear", 1, 5)
        self.assertTrue(related["setup_m5_opposite_sweep"])
        self.assertFalse(tracker.setup_snapshot("bear", 5, 6)["setup_m5_opposite_sweep"])


class PaperBrokerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = BotConfig(risk_fraction=.01, rr=2, entry_lifecycle_enabled=False)
        self.spec = SymbolSpec(tick_size=.01, tick_value=1, volume_min=.01, volume_step=.01)
        self.ob = OrderBlock("ob", "bull", 100, 99, 0, "t0", 0, "t0")

    def test_bid_ask_fill_and_target(self):
        broker = PaperBroker(self.cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob)
        broker.process_tick(Tick("t1", 99.98, 100.0))
        self.assertIsNotNone(broker.position)
        trades = broker.process_tick(Tick("t2", 102.0, 102.02))
        self.assertAlmostEqual(trades[0].r_multiple, 2.0)

    def test_same_candle_stop_wins(self):
        broker = PaperBroker(self.cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob)
        trades = broker.process_candle(candle(1, 100, 103, 98, 101), 1, spread=.20,
                                       execution_source="candle_replay")
        self.assertEqual(trades[0].result, "loss")
        self.assertEqual(trades[0].meta["entry_execution_source"], "candle_replay")
        self.assertAlmostEqual(trades[0].meta["entry_execution_spread"], .20)
        self.assertAlmostEqual(trades[0].meta["exit_execution_spread"], .20)

    def test_signal_candle_advances_state_without_execution(self):
        broker = PaperBroker(self.cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        order = broker.add_ob(self.ob)
        broker.process_signal_candle(candle(1, 100, 103, 98, 101), 1)
        self.assertEqual(broker.market_index, 1)
        self.assertTrue(order.active)
        self.assertIsNone(broker.position)
        self.assertEqual(broker.trades, [])

    def test_signal_candle_advances_lifecycle_without_fill(self):
        broker = PaperBroker(BotConfig(entry_lifecycle_enabled=True), 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        order = broker.add_ob(self.ob, {"formation_high": 105, "formation_low": 99,
                                        "formation_close": 104})
        broker.process_signal_candle(candle(1, 104, 106, 98, 105), 1)
        self.assertEqual(order.lifecycle_state, "accepted")
        self.assertIsNone(broker.position)

    def test_live_sweep_reclaim_confirms_on_candle_and_fills_on_next_tick(self):
        cfg = BotConfig(entry_mode="sweep_reclaim", rr=2,
                        sweep_reclaim_atr_buffer=.2,
                        entry_spread_guard_enabled=False,
                        entry_lifecycle_enabled=False)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        ob = OrderBlock("sweep-live", "bull", 100, 99, 0, "t0", 0, "t0",
                        atr_at_formation=2.0)
        order = broker.add_ob(ob)
        broker.process_signal_candle(candle(1, 100, 101, 98.8, 100.5), 1)
        self.assertTrue(order.meta["sweep_reclaim_confirmed"])
        self.assertIsNone(broker.position)
        broker.process_tick(Tick("t2", 100.5, 100.7))
        self.assertIsNotNone(broker.position)
        self.assertAlmostEqual(broker.position.entry, 100.7)
        self.assertAlmostEqual(broker.position.stop, 98.6)
        self.assertAlmostEqual(broker.position.target, 104.9)
        self.assertEqual(broker.position.meta["entry_execution_source"], "tick")

    def test_single_position_oldest_order_and_lot_rounding(self):
        broker = PaperBroker(self.cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        first = broker.add_ob(self.ob)
        broker.add_ob(OrderBlock("ob2", "bull", 101, 99.5, 1, "t1", 1, "t1"))
        broker.process_tick(Tick("t2", 99.9, 100.0))
        self.assertEqual(broker.position.order_id, first.id)
        self.assertAlmostEqual(broker.position.volume, 1.0)

    def test_two_position_capacity_fills_two_orders(self):
        cfg = BotConfig(max_open_positions=2, entry_lifecycle_enabled=False)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob)
        broker.add_ob(OrderBlock("ob2", "bull", 101, 99.5, 1, "t1", 1, "t1"))
        broker.process_tick(Tick("t2", 99.98, 100.0))
        self.assertEqual(len(broker.positions), 2)

    def test_tick_beyond_stop_invalidates_without_fill(self):
        broker = PaperBroker(self.cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        order = broker.add_ob(self.ob)
        broker.process_tick(Tick("t1", 98.8, 98.9))
        self.assertIsNone(broker.position)
        self.assertFalse(order.active)

    def test_sweep_reclaim_requires_close_beyond_ob_and_uses_atr_buffer(self):
        cfg = BotConfig(entry_mode="sweep_reclaim", rr=2,
                        sweep_reclaim_atr_buffer=.2,
                        entry_lifecycle_enabled=False)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        ob = OrderBlock("sweep", "bull", 100, 99, 0, "t0", 0, "t0",
                        atr_at_formation=2.0)
        broker.add_ob(ob)
        broker.process_candle(candle(1, 100, 100.2, 98.8, 99.8), 1, spread=.2)
        self.assertIsNone(broker.position)

        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(ob)
        broker.process_candle(candle(1, 100, 101, 98.8, 100.5), 1, spread=.2)
        self.assertIsNotNone(broker.position)
        self.assertAlmostEqual(broker.position.entry, 100.7)
        self.assertAlmostEqual(broker.position.stop, 98.6)
        self.assertAlmostEqual(broker.position.target, 104.9)
        self.assertEqual(broker.position.meta["entry_mode"], "sweep_reclaim")

    def test_sweep_reclaim_never_enters_from_tick_touch(self):
        broker = PaperBroker(BotConfig(entry_mode="sweep_reclaim"), 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob)
        broker.process_tick(Tick("t1", 98.8, 98.9))
        self.assertIsNone(broker.position)
        self.assertTrue(broker.pending[0].active)

    def test_bos_only_rejects_choch_before_order_creation(self):
        cfg = BotConfig(allowed_break_kinds=("BOS",))
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        choch = OrderBlock("c", "bull", 100, 99, 0, "t0", 0, "t0",
                           break_kind="CHoCH")
        self.assertIsNone(broker.add_ob(choch))
        self.assertEqual(broker.stats["rejected_break_kind"], 1)

    def test_strong_choch_only_rejects_weak_and_accepts_top_quartile(self):
        cfg = BotConfig(allowed_break_kinds=("BOS", "CHoCH"), strong_choch_only=True)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        choch = OrderBlock("c", "bull", 100, 99, 0, "t0", 0, "t0",
                           break_kind="CHoCH")
        self.assertIsNone(broker.add_ob(choch, {"bos_displacement_top_quartile": False}))
        self.assertIsNotNone(broker.add_ob(choch, {"bos_displacement_top_quartile": True}))
        self.assertEqual(broker.stats["rejected_weak_choch"], 1)

    def test_default_strategy_is_bos_only(self):
        self.assertEqual(BotConfig().allowed_break_kinds, ("BOS",))
        self.assertEqual(BotConfig().swing_length, 12)
        self.assertFalse(BotConfig().choch_displacement_risk_sizing_enabled)
        self.assertEqual(BotConfig().rr, 1.5)
        self.assertEqual(BotConfig().fallback_spread, .20)

    def test_restore_retargets_pending_but_not_open_position(self):
        broker = PaperBroker(BotConfig(rr=1.5, entry_lifecycle_enabled=False), 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        order = broker.add_ob(self.ob)
        order.target = 102.0
        self.assertEqual(broker.reconcile_pending_targets(), 1)
        self.assertEqual(order.target, 101.5)

    def test_liquidity_sizing_changes_risk_not_trade_admission(self):
        cfg = BotConfig(entry_lifecycle_enabled=False, liquidity_risk_sizing_enabled=True)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob, {"setup_m5_opposite_sweep": True})
        broker.process_tick(Tick("t1", 99.98, 100.0))
        self.assertAlmostEqual(broker.position.meta["applied_risk_fraction"], .0125)

    def test_choch_sizing_uses_fill_context_without_rejecting_trade(self):
        cfg = BotConfig(entry_lifecycle_enabled=False, choch_risk_sizing_enabled=True)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob)
        broker.set_market_context({"bull": {"m5_last_choch_alignment": "opposite"}})
        broker.process_tick(Tick("t1", 99.98, 100.0))
        self.assertIsNotNone(broker.position)
        self.assertAlmostEqual(broker.position.meta["applied_risk_fraction"], .0125)
        self.assertEqual(broker.position.meta["fill_m5_last_choch_alignment"], "opposite")

    def test_combined_context_sizing_scores_both_signals(self):
        cfg = BotConfig(entry_lifecycle_enabled=False,
                        combined_context_risk_sizing_enabled=True)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob, {"setup_m5_opposite_sweep": True})
        broker.set_market_context({"bull": {"m5_last_choch_alignment": "opposite"}})
        broker.process_tick(Tick("t1", 99.98, 100.0))
        self.assertEqual(broker.position.meta["context_risk_score"], 2)
        self.assertAlmostEqual(broker.position.meta["applied_risk_fraction"], .0125)

    def test_choch_displacement_sizing_scores_both_signals(self):
        cfg = BotConfig(entry_lifecycle_enabled=False,
                        choch_displacement_risk_sizing_enabled=True)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob, {"bos_displacement_top_quartile": True})
        broker.set_market_context({"bull": {"m5_last_choch_alignment": "opposite"}})
        broker.process_tick(Tick("t1", 99.98, 100.0))
        self.assertEqual(broker.position.meta["context_risk_score"], 2)
        self.assertAlmostEqual(broker.position.meta["applied_risk_fraction"], .0125)

    def test_choch_risk_cap_overrides_quality_size(self):
        cfg = BotConfig(allowed_break_kinds=("BOS", "CHoCH"),
                        choch_displacement_risk_sizing_enabled=True,
                        choch_risk_cap_fraction=.005,
                        entry_lifecycle_enabled=False)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        choch = OrderBlock("cap", "bull", 100, 99, 0, "t0", 0, "t0",
                           break_kind="CHoCH")
        broker.add_ob(choch, {"bos_displacement_top_quartile": True})
        broker.set_market_context({"bull": {"m5_last_choch_alignment": "opposite"}})
        broker.process_tick(Tick("t1", 99.98, 100.0))
        self.assertAlmostEqual(broker.position.meta["applied_risk_fraction"], .005)

    def test_three_factor_sizing_uses_causal_entry_age_rank(self):
        cfg = BotConfig(three_factor_risk_sizing_enabled=True,
                        entry_age_rank_window=4, entry_age_rank_min_samples=2,
                        entry_lifecycle_enabled=False)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.entry_age_values = [1, 2]
        ob = OrderBlock("age", "bull", 100, 99, 0, "t0", 0, "t0",
                        break_kind="BOS")
        broker.add_ob(ob, {"bos_displacement_top_quartile": True})
        broker.set_market_context({"bull": {"m5_last_choch_alignment": "opposite"}})
        broker.market_index = 5
        broker.process_tick(Tick("t5", 99.98, 100.0))
        self.assertTrue(broker.position.meta["entry_age_top_quartile"])
        self.assertEqual(broker.position.meta["context_risk_score"], 3)
        self.assertAlmostEqual(broker.position.meta["applied_risk_fraction"], .0125)

    def test_split_choch_risk_and_bos_counter_bonus(self):
        cfg = BotConfig(allowed_break_kinds=("BOS", "CHoCH"),
                        choch_split_risk_enabled=True,
                        choch_risk_cap_fraction=None,
                        entry_age_rank_window=4, entry_age_rank_min_samples=2)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.entry_age_values = [1, 2]
        choch = OrderBlock("split", "bull", 100, 99, 0, "t0", 0, "t0",
                           break_kind="CHoCH")
        broker.add_ob(choch, {"bos_displacement_top_quartile": True})
        broker.market_index = 5
        broker.process_tick(Tick("t5", 99.98, 100))
        self.assertAlmostEqual(broker.position.meta["applied_risk_fraction"], .0025)

        cfg = BotConfig(bos_three_factor_risk_enabled=True,
                        bos_m15_counter_bonus_fraction=.001,
                        entry_age_rank_window=4, entry_age_rank_min_samples=2)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.entry_age_values = [1, 2]
        bos = OrderBlock("bonus", "bull", 100, 99, 0, "t0", 0, "t0",
                         break_kind="BOS")
        broker.add_ob(bos, {"bos_displacement_top_quartile": False})
        broker.set_market_context({"bull": {"m15_alignment": "counter"}})
        broker.market_index = 1
        broker.process_tick(Tick("t1", 99.98, 100))
        self.assertAlmostEqual(broker.position.meta["applied_risk_fraction"], .0085)

    def test_liquidity_target_requires_minimum_rr_and_uses_fill_context(self):
        cfg = BotConfig(target_mode="m5_liquidity_min_rr", rr=1.5,
                        entry_lifecycle_enabled=False)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob)
        broker.set_market_context({"bull": {"liq_m5_nearest_bsl_level": 103.0}})
        broker.process_tick(Tick("t1", 99.98, 100.0))
        self.assertAlmostEqual(broker.position.target, 103.0)
        self.assertEqual(broker.position.meta["target_source"], "m5_liquidity")
        self.assertAlmostEqual(broker.position.meta["planned_rr"], 3.0)

        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob)
        broker.set_market_context({"bull": {"liq_m5_nearest_bsl_level": 101.2}})
        broker.process_tick(Tick("t1", 99.98, 100.0))
        self.assertAlmostEqual(broker.position.target, 101.5)
        self.assertEqual(broker.position.meta["target_source"], "fixed_rr")

    def test_restore_reconciliation_cancels_legacy_pending(self):
        broker = PaperBroker(BotConfig(), 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.pending.append(PendingOrder("old", "ob", "bull", 100, 99, 102, 0, "t0"))
        self.assertEqual(broker.reconcile_entry_filters(), 1)
        self.assertFalse(broker.pending[0].active)

    def test_minimum_wait_blocks_first_two_bars_then_allows_fill(self):
        cfg = BotConfig(min_entry_wait_bars=3, entry_lifecycle_enabled=False)
        broker = PaperBroker(cfg, 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.add_ob(self.ob)
        for i in (1, 2):
            broker.process_candle(candle(i, 100, 100.5, 99.5, 100), i)
            self.assertIsNone(broker.position)
        broker.process_candle(candle(3, 100, 100.5, 99.5, 100), 3)
        self.assertIsNotNone(broker.position)
        self.assertEqual(broker.position.meta["wait_bars"], 3)

    def test_event_lifecycle_requires_extension_then_retracement(self):
        broker = PaperBroker(BotConfig(entry_lifecycle_enabled=True), 10_000, self.spec)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        order = broker.add_ob(self.ob, {"formation_high": 105, "formation_low": 99,
                                        "formation_close": 104})
        broker.process_candle(candle(1, 104, 106, 103, 105), 1)
        self.assertEqual(order.lifecycle_state, "accepted")
        broker.process_candle(candle(2, 105, 105.5, 103, 104), 2)
        self.assertEqual(order.lifecycle_state, "armed")
        self.assertIsNone(broker.position)
        broker.process_candle(candle(3, 101, 101, 99.5, 100.5), 3)
        self.assertIsNotNone(broker.position)


class PersistenceTests(unittest.TestCase):
    def test_trimmed_state_rebases_pending_age_and_continues_engine(self):
        cfg = BotConfig(swing_length=2, atr_period=3, state_history_bars=5)
        engine = PineSwingOBEngine(cfg)
        for i in range(20):
            base = 100 + (i % 5)
            engine.process(candle(i, base, base + 2, base - 2, base + .5))
        state = engine.dump_state(5)
        offset = state["index_offset"]
        restored = PineSwingOBEngine(cfg)
        restored.load_state(state)
        next_bar = candle(20, 104, 107, 103, 106)
        expected = [(item.direction, item.kind) for item in engine.process(next_bar)[0]]
        actual = [(item.direction, item.kind) for item in restored.process(next_bar)[0]]
        self.assertEqual(actual, expected)

        broker = PaperBroker(cfg, 10_000)
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        broker.market_index = 19
        broker.pending.append(PendingOrder("p", "ob", "bull", 100, 99, 101.5, 10, "t"))
        saved = broker.dump_state(offset)
        resumed = PaperBroker(cfg, 10_000)
        resumed.update_m5_trend(TrendDirection.BULLISH, "t0")
        resumed.load_state(saved)
        self.assertEqual(resumed.market_index - resumed.pending[0].created_index, 9)

    def test_report_worker_runs_export_off_thread(self):
        import logging
        completed = []
        worker = ReportWorker(logging.getLogger("test-report-worker"))
        worker.submit(lambda value: completed.append(value), "done")
        worker.close()
        self.assertEqual(completed, ["done"])

    def test_dashboard_serves_generated_report_directory(self):
        from urllib.request import urlopen
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "paper_report_latest.html").write_text("live-report", encoding="utf-8")
            server = DashboardServer(root, "127.0.0.1", 0)
            server.start()
            try:
                body = urlopen(server.url + "paper_report_latest.html", timeout=2).read().decode()
                self.assertEqual(body, "live-report")
            finally:
                server.close()

    def test_demo_executor_refuses_real_and_sends_guarded_demo_request(self):
        class FakeMT5:
            ACCOUNT_TRADE_MODE_DEMO = 0
            TRADE_ACTION_DEAL = 1
            ORDER_TYPE_BUY = 0
            ORDER_TYPE_SELL = 1
            ORDER_TIME_GTC = 0
            ORDER_FILLING_IOC = 1
            TRADE_RETCODE_DONE = 10009
            TRADE_RETCODE_DONE_PARTIAL = 10010

            def __init__(self, trade_mode):
                self.trade_mode = trade_mode
                self.request = None

            def account_info(self):
                return SimpleNamespace(trade_mode=self.trade_mode, trade_allowed=True)

            def terminal_info(self):
                return SimpleNamespace(connected=True, trade_allowed=True)

            def symbol_info_tick(self, symbol):
                return SimpleNamespace(bid=100.0, ask=100.2)

            def order_send(self, request):
                self.request = request
                return SimpleNamespace(retcode=10009, order=77, deal=88,
                                       price=request["price"], volume=request["volume"],
                                       comment="done")

            def positions_get(self, **kwargs):
                return ()

            def last_error(self):
                return (1, "ok")

        with self.assertRaises(RuntimeError):
            DemoExecutor(FakeMT5(2), "XAUUSD", 120512, 100)
        fake = FakeMT5(0)
        executor = DemoExecutor(fake, "XAUUSD", 120512, 100)
        position = PaperPosition("paper-id", "order", "bull", 100.2, 99, 102,
                                 .1, 100, "t1")
        result = executor.open(position)
        self.assertEqual(result["ticket"], 77)
        self.assertEqual(fake.request["type"], fake.ORDER_TYPE_BUY)
        self.assertEqual(fake.request["sl"], 99)
        self.assertTrue(position.meta["demo_order_sent"])
        self.assertEqual(position.meta["execution_state"], "demo_confirmed_active")

    def test_demo_reconciliation_matches_marks_missing_and_reports_orphans(self):
        class FakeMT5:
            ACCOUNT_TRADE_MODE_DEMO = 0

            def account_info(self):
                return SimpleNamespace(trade_mode=0, trade_allowed=True)

            def terminal_info(self):
                return SimpleNamespace(connected=True, trade_allowed=True)

            def positions_get(self, **kwargs):
                return (SimpleNamespace(ticket=11, magic=120512,
                                        comment="pineob:paper-match", volume=.1),
                        SimpleNamespace(ticket=12, magic=120512,
                                        comment="manual-orphan", volume=.2),
                        SimpleNamespace(ticket=13, magic=999,
                                        comment="other-bot", volume=.3))

        matched = PaperPosition("paper-match-id", "o1", "bull", 100, 99, 102,
                                .1, 100, "t1", meta={"demo_position_ticket": 11})
        missing = PaperPosition("paper-missing-id", "o2", "bear", 100, 101, 98,
                                .1, 100, "t1", meta={"demo_position_ticket": 99})
        result = DemoExecutor(FakeMT5(), "XAUUSD", 120512, 100).reconcile(
            [matched, missing])
        self.assertEqual(result["matched"][0]["ticket"], 11)
        self.assertEqual(result["missing"][0]["position_id"], missing.id)
        self.assertEqual(result["orphans"][0]["ticket"], 12)
        self.assertEqual(matched.meta["execution_state"], "demo_confirmed_active")
        self.assertEqual(missing.meta["execution_state"],
                         "reconciliation_required_demo_missing")

    def test_demo_margin_check_reduces_volume_before_send(self):
        class FakeMT5:
            ACCOUNT_TRADE_MODE_DEMO = 0
            TRADE_ACTION_DEAL = 1
            ORDER_TYPE_BUY = 0
            ORDER_TYPE_SELL = 1
            ORDER_TIME_GTC = 0
            ORDER_FILLING_IOC = 1
            TRADE_RETCODE_DONE = 10009
            TRADE_RETCODE_DONE_PARTIAL = 10010

            def __init__(self):
                self.request = None

            def account_info(self):
                return SimpleNamespace(trade_mode=0, trade_allowed=True, margin_free=1000)

            def terminal_info(self):
                return SimpleNamespace(connected=True, trade_allowed=True)

            def symbol_info_tick(self, symbol):
                return SimpleNamespace(bid=100, ask=100.2)

            def symbol_info(self, symbol):
                return SimpleNamespace(volume_min=.1, volume_max=10, volume_step=.1)

            def order_calc_margin(self, kind, symbol, volume, price):
                return volume * 1000

            def order_check(self, request):
                return SimpleNamespace(retcode=10009)

            def order_send(self, request):
                self.request = request
                return SimpleNamespace(retcode=10009, order=77, deal=88,
                                       price=request["price"], volume=request["volume"],
                                       comment="done")

            def positions_get(self, **kwargs):
                return ()

            def last_error(self):
                return (1, "ok")

        fake = FakeMT5()
        executor = DemoExecutor(fake, "XAUUSD", 120512, 100, .8)
        position = PaperPosition("margin", "order", "bull", 100.2, 99, 102,
                                 2.0, 100, "t1")
        result = executor.open(position)
        self.assertAlmostEqual(fake.request["volume"], .8)
        self.assertAlmostEqual(result["calculated_margin"], 800)

    def test_actual_demo_entry_and_exit_reprice_ledger(self):
        broker = PaperBroker(BotConfig(demo_orders_enabled=True), 10_000,
                             SymbolSpec(tick_size=.01, tick_value=1,
                                        volume_min=.01, volume_step=.01))
        broker.update_m5_trend(TrendDirection.BULLISH, "t0")
        ob = OrderBlock("actual", "bull", 100, 99, 0, "t0", 0, "t0")
        broker.add_ob(ob)
        broker.process_tick(Tick("t1", 99.98, 100))
        position = broker.position
        broker.apply_demo_entry(position, 100.2, .5)
        self.assertAlmostEqual(position.risk_money, 60)
        self.assertEqual(position.meta["execution_state"], "demo_confirmed_active")
        trade = broker.process_tick(Tick("t2", 102, 102.2))[0]
        paper_equity = broker.equity
        broker.apply_demo_exit(trade, 101.8, .5)
        self.assertAlmostEqual(trade.pnl, 80)
        self.assertAlmostEqual(trade.r_multiple, 80 / 60)
        self.assertAlmostEqual(broker.equity, paper_equity + trade.pnl - 90)

    def test_market_recorder_writes_ticks_candles_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            recorder = MarketRecorder(Path(temp), "XAUUSD")
            tick = Tick("2026-07-06T01:02:03+00:00", 100, 100.2)
            self.assertTrue(recorder.record_tick(tick))
            self.assertFalse(recorder.record_tick(tick))
            bar = candle(1, 100, 101, 99, 100.5)
            self.assertTrue(recorder.record_candle(bar))
            self.assertFalse(recorder.record_candle(bar))
            restarted = MarketRecorder(Path(temp), "XAUUSD")
            self.assertFalse(restarted.record_tick(tick))
            self.assertFalse(restarted.record_candle(bar))
            self.assertEqual(len((Path(temp) / "ticks_XAUUSD_20260706.csv").read_text().splitlines()), 2)
            self.assertEqual(len((Path(temp) / "candles_XAUUSD_M5.csv").read_text().splitlines()), 2)

    def test_snapshot_round_trip_matches_continuation(self):
        cfg = BotConfig(swing_length=2, atr_period=20)
        bars = [candle(0, 9, 10, 8, 9), candle(1, 10, 12, 9, 11),
                candle(2, 10, 11, 10, 10.5), candle(3, 10, 10.5, 9.5, 9.8)]
        engine, broker = PineSwingOBEngine(cfg), PaperBroker(cfg, 10_000)
        for item in bars:
            engine.process(item)
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp) / "state.db")
            store.save(engine.dump_state(), broker.dump_state())
            snap = store.load(); store.close()
            restored = PineSwingOBEngine(cfg); restored.load_state(snap["engine"])
            next_bar = candle(4, 10, 13.5, 9.6, 13)
            expected = engine.process(next_bar)[0][0]
            actual = restored.process(next_bar)[0][0]
            self.assertEqual((expected.direction, expected.kind, expected.pivot_level),
                             (actual.direction, actual.kind, actual.pivot_level))

    def test_mt5_adapter_has_no_order_send_path(self):
        source = (Path(__file__).parents[1] / "pine_ob_bot" / "mt5_feed.py").read_text(encoding="utf-8")
        calls = [node.func.attr for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertNotIn("order_send", calls)


if __name__ == "__main__":
    unittest.main()
