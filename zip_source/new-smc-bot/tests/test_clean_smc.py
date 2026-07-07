import unittest
import pandas as pd

from data.loader import align_index
from smc.structure import structure_events
from smc.warmup import WarmupState, LiquidityZone
from smc.types import Dir


class CleanSMCTests(unittest.TestCase):
    def test_h1_range_allows_both_but_trend_blocks_countertrend(self):
        state = WarmupState({})
        self.assertTrue(state.trend_allows(Dir.BULL))
        self.assertTrue(state.trend_allows(Dir.BEAR))
        state.h1_trend = 1
        self.assertTrue(state.trend_allows(Dir.BULL))
        self.assertFalse(state.trend_allows(Dir.BEAR))
        state.h1_trend = -1
        self.assertFalse(state.trend_allows(Dir.BULL))
        self.assertTrue(state.trend_allows(Dir.BEAR))

    def test_continuous_penetration_counts_as_one_sweep(self):
        cfg = {"merge_threshold": 0.5, "h1_proximity": 2.0,
               "min_score_extra_life": 2, "base_max_sweeps": 2,
               "touched_max_sweeps": 3, "near_h1_max_sweeps": 4}
        state = WarmupState(cfg)
        zone = LiquidityZone("BSL", 100.0, 1)
        state.liquidity.append(zone)
        state._update_liquidity_lifecycle({"idx": 2, "high": 101, "low": 99})
        state._update_liquidity_lifecycle({"idx": 3, "high": 102, "low": 100})
        self.assertEqual(zone.sweeps, 1)
        state._update_liquidity_lifecycle({"idx": 4, "high": 99, "low": 98})
        state._update_liquidity_lifecycle({"idx": 5, "high": 101, "low": 99})
        self.assertNotIn(zone, state.liquidity)

    def test_htf_visible_only_after_close(self):
        htf = pd.DataFrame({"time": pd.to_datetime([
            "2026-01-01 00:00Z", "2026-01-01 00:15Z"
        ])})
        self.assertEqual(align_index(htf, pd.Timestamp("2026-01-01 00:05Z")), -1)
        self.assertEqual(align_index(htf, pd.Timestamp("2026-01-01 00:10Z")), 0)

    def test_break_requires_close_beyond_confirmed_pivot(self):
        highs = [1, 2, 5, 2, 1, 4, 6]
        lows = [0, 0.5, 1, 0.5, 0, 1, 2]
        closes = [0.5, 1.5, 4, 1, 0.5, 4, 5.5]
        df = pd.DataFrame({"open": closes, "high": highs, "low": lows,
                           "close": closes})
        events = structure_events(df, left=2, right=2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["break_idx"], 6)
        self.assertEqual(events[0]["tag"], "BOS")


if __name__ == "__main__":
    unittest.main()
