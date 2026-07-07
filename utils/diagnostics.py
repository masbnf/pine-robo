"""Per-run strategy diagnostics ("rejection funnel").

Counts how many setups die at each filter stage, and — where useful — records
HOW FAR each rejected setup was from passing (the "edge" / margin). This tells
you which filter is the real bottleneck and whether it's *barely* rejecting
setups (so a small tweak would unlock many) or rejecting them by a wide margin.
"""
from __future__ import annotations
import os
from datetime import datetime


class _Stat:
    """Tiny running aggregate (no list storage)."""
    __slots__ = ("n", "s", "mn", "mx")

    def __init__(self):
        self.n = 0
        self.s = 0.0
        self.mn = None
        self.mx = None

    def add(self, v: float):
        self.n += 1
        self.s += v
        self.mn = v if self.mn is None else min(self.mn, v)
        self.mx = v if self.mx is None else max(self.mx, v)

    @property
    def avg(self) -> float:
        return self.s / self.n if self.n else 0.0


class Diagnostics:
    # ordered to match the strategy sequence; (key, human description, has-margin)
    ORDER = [
        ("no_bos",          "STEP1  no BOS at all in window",              False),
        ("bos_wrong_direction", "STEP1  BOS exists but is the other side", False),
        ("extended",        "STEP1  price too extended from BOS origin",   True),   # margin = dist/ATR
        ("ema_trend",       "STEP1  against HTF EMA trend",                False),
        ("against_h1_trend", "STEP1b against H1 main trend",              True),   # margin = H1 bars since that BOS (staleness)
        ("no_sweep",        "STEP2  required liquidity sweep missing",     False),
        ("no_ob",           "STEP3  no order block found",                 False),
        ("no_zone",         "STEP3  no supply/demand zone",                False),
        ("zone_not_tapped", "STEP3  price never tapped zone (not armed)",  True),   # margin = dist/ATR
        ("no_fvg_overlap",  "STEP3  no unfilled FVG overlapping zone",     False),
        ("zone_no_m5_bos",  "STEP3c zone touched but no M5 BOS confirm",  False),
        ("arm_expired",     "STEP4  armed zone expired before CHoCH",      True),   # margin = bars waited
        ("no_choch",        "STEP4  no M5 CHoCH yet (still waiting/none)",  False),
        ("rsi_block",       "STEP4b RSI confirmation failed",              False),
        ("low_score",       "SCORE  setup score below minimum",           True),
        ("too_far_from_zone", "STEP4c entry ran too far from the zone",    True),   # margin = dist/ATR
        ("no_tp",           "STEP6  no valid take-profit target",          False),
        ("bad_stop",        "STEP5  stop wrong-side / smaller than min",   True),   # margin = stop/min ratio
        ("low_rr",          "STEP6  reward:risk below min_rr",             True),   # margin = rr value
    ]

    def __init__(self):
        self.total = 0          # direction-evaluations attempted
        self.signals = 0        # full setups that passed everything
        self.counts = {k: 0 for k, _, _ in self.ORDER}
        self.stats = {k: _Stat() for k, _, _ in self.ORDER}

    # ---- recording API (called by the strategy) ----
    def start(self):
        self.total += 1

    def reject(self, reason: str, margin: float | None = None):
        if reason in self.counts:
            self.counts[reason] += 1
            if margin is not None:
                self.stats[reason].add(margin)

    def success(self):
        self.signals += 1

    # ---- reporting ----
    def render(self, header: str = "") -> str:
        L = []
        if header:
            L.append(header)
        t = max(self.total, 1)
        L.append(f"Total direction-evaluations : {self.total}")
        L.append(f"Setups that passed ALL filters: {self.signals} "
                 f"({self.signals / t * 100:.3f}%)")
        L.append("")
        L.append("REJECTION FUNNEL  (survivors after each filter):")
        L.append("-" * 72)
        survivors = self.total
        for key, desc, has_margin in self.ORDER:
            cut = self.counts[key]
            survivors -= cut
            pct = cut / t * 100
            line = f"{desc:<44} -{cut:>6}  ({pct:5.1f}%)  -> {survivors:>6} left"
            L.append(line)
            if has_margin and self.stats[key].n:
                s = self.stats[key]
                label = {
                    "extended": "over-extension xATR",
                    "zone_not_tapped": "distance to zone xATR",
                    "arm_expired": "bars waited before expiry",
                    "against_h1_trend": "H1 bars since opposing BOS (staleness)",
                    "bad_stop": "stop / min-stop ratio",
                    "low_rr": "rr value",
                }.get(key, "margin")
                L.append(f"      └─ {label}: avg {s.avg:.2f}  min {s.mn:.2f}  max {s.mx:.2f}")
        L.append("-" * 72)
        L.append("")
        L.append("HOW TO READ THIS:")
        L.append("  • The filter with the biggest single '-N' cut is your main bottleneck.")
        L.append("  • If a margin's AVG is just past the limit (e.g. zone distance ~0.6 vs")
        L.append("    tolerance 0.5), loosening that one knob would unlock many setups.")
        L.append("  • If margins are wide, the filter is doing real work — leave it.")
        return "\n".join(L)


def _sparkline(values, width: int = 64) -> str:
    """ASCII sparkline of an equity curve, downsampled to `width` columns."""
    if not values or len(values) < 2:
        return "(not enough trades)"
    blocks = "▁▂▃▄▅▆▇█"
    # downsample to width points
    n = len(values)
    if n > width:
        step = n / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values
    lo, hi = min(sampled), max(sampled)
    rng = (hi - lo) or 1.0
    return "".join(blocks[min(7, int((v - lo) / rng * 7))] for v in sampled)


# metric keys that belong in the "risk" block (vs the headline summary)
_RISK_KEYS = {"profit_factor", "max_drawdown", "max_drawdown_pct", "avg_win",
              "avg_loss", "expectancy", "largest_win", "largest_loss",
              "max_win_streak", "max_loss_streak"}


def write_run_log(log_dir: str, report: dict, diag: Diagnostics,
                  data_range: str = "", extra: dict | None = None,
                  equity: list | None = None, extra_text: str = "") -> str:
    """Write a timestamped diagnostics file (+ equity CSV). Returns the log path."""
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(log_dir, f"run_{stamp}.log")

    lines = [
        "=" * 72,
        f" BACKTEST RUN  —  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 72,
    ]
    if data_range:
        lines.append(f"Data range : {data_range}")

    # headline summary (non-risk keys)
    lines.append("")
    lines.append("SUMMARY")
    lines.append("-" * 72)
    for k, v in report.items():
        if k not in _RISK_KEYS:
            lines.append(f"  {k:>18}: {v}")
    if extra:
        for k, v in extra.items():
            lines.append(f"  {k:>18}: {v}")

    # risk metrics block
    lines.append("")
    lines.append("RISK METRICS  (how painful the path was, not just the destination)")
    lines.append("-" * 72)
    for k in ["profit_factor", "expectancy", "avg_win", "avg_loss",
              "largest_win", "largest_loss", "max_win_streak", "max_loss_streak",
              "max_drawdown", "max_drawdown_pct"]:
        if k in report:
            lines.append(f"  {k:>18}: {report[k]}")
    lines.append("")
    lines.append("  reading: profit_factor>1 = profitable; <1.3 is fragile.")
    lines.append("           max_drawdown_pct = worst equity drop from a peak.")
    lines.append("           max_loss_streak = most losses in a row (sizing stress test).")

    # equity curve
    if equity:
        lines.append("")
        lines.append("EQUITY CURVE")
        lines.append("-" * 72)
        lines.append(f"  start {equity[0]:.0f}  ->  end {equity[-1]:.0f}   "
                     f"peak {max(equity):.0f}   trough {min(equity):.0f}")
        lines.append("  " + _sparkline(equity))
        # save full curve as CSV for plotting
        eq_path = os.path.join(log_dir, f"equity_{stamp}.csv")
        try:
            with open(eq_path, "w", encoding="utf-8") as f:
                f.write("trade_no,equity\n")
                for i, v in enumerate(equity):
                    f.write(f"{i},{v}\n")
            lines.append(f"  full curve -> {eq_path}")
        except Exception:
            pass

    if extra_text:
        lines.append("")
        lines.append(extra_text)

    lines.append("")
    lines.append(diag.render("FILTER DIAGNOSTICS"))
    lines.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
