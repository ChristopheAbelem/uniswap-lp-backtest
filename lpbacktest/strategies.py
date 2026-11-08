"""Range strategies for the replay engine.

A strategy gives the ticks of each new range from the current tick and the hourly
prices known at that moment, and says whether the position moves when it leaves
its range.
"""

import math
import statistics

from lpbacktest.engine import range_ticks

MAX_TICK = 887270  # largest tick usable with spacing 10 (887272 rounded down)
CHECKS_PER_DAY = 24  # the engine records about one price per hour


class FullRange:
    """The whole price range, as in Uniswap v2. Never leaves its range."""

    name = "full_range"
    rebalances = False

    def range(self, tick, closes):
        return -MAX_TICK, MAX_TICK


class FixedRange:
    """A +/- width range around the entry price, optionally moved on exit."""

    def __init__(self, width, rebalance=False):
        if not 0 < width < 1:
            raise ValueError("width must be between 0 and 1")
        self.width = width
        self.rebalances = rebalance
        self.name = f"{'rebalance' if rebalance else 'fixed'}_{width:g}"

    def range(self, tick, closes):
        return range_ticks(tick, self.width)


class VolScaled:
    """A range of +/- k daily standard deviations, moved on exit.

    Volatility comes only from prices already known, so there is no look-ahead.
    """

    rebalances = True
    MIN_WIDTH = 0.01  # below this the range has a few tens of ticks, where validation errors grow
    MAX_WIDTH = 0.9

    def __init__(self, k, lookback_hours=168):
        if k <= 0:
            raise ValueError("k must be positive")
        self.k = k
        self.lookback_hours = lookback_hours
        self.name = f"vol_scaled_k{k:g}"

    def width(self, closes):
        recent = closes[-(self.lookback_hours + 1) :]
        if len(recent) < CHECKS_PER_DAY + 1:
            raise ValueError("VolScaled needs at least one day of hourly prices")
        returns = [math.log(later / earlier) for earlier, later in zip(recent, recent[1:])]
        daily_volatility = statistics.stdev(returns) * math.sqrt(CHECKS_PER_DAY)
        return min(max(self.k * daily_volatility, self.MIN_WIDTH), self.MAX_WIDTH)

    def range(self, tick, closes):
        return range_ticks(tick, self.width(closes))


def build(name, width=0.1, k=2.0):
    """Return a strategy from its command line name."""
    if name == "full_range":
        return FullRange()
    if name == "fixed":
        return FixedRange(width)
    if name == "rebalance":
        return FixedRange(width, rebalance=True)
    if name == "vol_scaled":
        return VolScaled(k)
    raise ValueError(f"Unknown strategy: {name}")
