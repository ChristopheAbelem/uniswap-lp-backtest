import math

import pytest

from lpbacktest.engine import hourly_closes, range_ticks, simulate, simulate_range
from lpbacktest.strategies import FixedRange, FullRange, VolScaled, build


def swap_at(tick, block, amount0=0, active_liquidity=10**18):
    sqrt_price_x96 = int(1.0001 ** (tick / 2) * 2**96)
    return (amount0, 0, sqrt_price_x96, active_liquidity, tick, block)


def closes_with_volatility(hourly_return, count):
    """Prices that move up and down by the same log return every hour."""
    prices = [2000.0]
    for i in range(count):
        prices.append(prices[-1] * math.exp(hourly_return if i % 2 == 0 else -hourly_return))
    return prices


def test_full_range_uses_usable_ticks():
    lower, upper = FullRange().range(195000, [])

    assert lower == -887270 and upper == 887270
    assert lower % 10 == 0 and upper % 10 == 0


def test_full_range_matches_the_v2_impermanent_loss_formula():
    # Full range is Uniswap v2: position / HODL - 1 = 2 * sqrt(r) / (1 + r) - 1.
    start, end = 195000, 197000
    swaps = [swap_at(start, 1), swap_at(end, 2)]

    result = simulate(swaps, 100_000, FullRange())

    r = 1.0001 ** (start - end)  # ratio of ETH prices in USDC
    expected = 2 * math.sqrt(r) / (1 + r) - 1
    assert result["position_usd"] / result["hodl_usd"] - 1 == pytest.approx(expected, abs=1e-6)


def test_fixed_range_gives_the_same_result_as_simulate_range():
    swaps = [swap_at(195000, 1), swap_at(195100, 2, amount0=10**9), swap_at(196000, 3)]

    by_strategy = simulate(swaps, 100_000, FixedRange(0.1))
    by_width = simulate_range(swaps, 100_000, 0.1)

    assert by_strategy["net_vs_hodl_usd"] == pytest.approx(by_width["net_vs_hodl_usd"])
    assert FixedRange(0.1).range(195000, []) == range_ticks(195000, 0.1)


@pytest.mark.parametrize("width", [0, 1, -0.1])
def test_fixed_range_rejects_impossible_widths(width):
    with pytest.raises(ValueError):
        FixedRange(width)


def test_vol_scaled_width_is_k_daily_volatilities():
    closes = closes_with_volatility(0.002, 168)
    hourly_std = math.sqrt(168 / 167) * 0.002  # sample standard deviation of +/- 0.002

    width = VolScaled(2).width(closes)

    assert width == pytest.approx(2 * hourly_std * math.sqrt(24))
    assert VolScaled(4).width(closes) == pytest.approx(2 * width)


def test_vol_scaled_width_has_a_floor_and_a_cap():
    calm = closes_with_volatility(0.00001, 168)
    wild = closes_with_volatility(0.2, 168)

    assert VolScaled(1).width(calm) == VolScaled.MIN_WIDTH
    assert VolScaled(5).width(wild) == VolScaled.MAX_WIDTH


def test_vol_scaled_uses_only_the_lookback_window():
    old_wild = closes_with_volatility(0.05, 200)
    recent_calm = closes_with_volatility(0.001, 168)

    width = VolScaled(2, lookback_hours=168).width(old_wild + recent_calm)

    assert width == pytest.approx(VolScaled(2).width(recent_calm))


def test_vol_scaled_needs_a_day_of_prices():
    with pytest.raises(ValueError, match="one day"):
        VolScaled(2).width([2000.0] * 10)


class SpyStrategy:
    """Records how many prices it could see at each call."""

    rebalances = True

    def __init__(self):
        self.seen = []

    def range(self, tick, closes):
        self.seen.append(len(closes))
        return range_ticks(tick, 0.1)


def test_strategy_sees_warm_up_and_replay_prices_only():
    gas = ([1], [10 * 10**9])
    swaps = [swap_at(195000, 1), swap_at(200000, 2), swap_at(200000, 400)]
    spy = SpyStrategy()

    result = simulate(swaps, 100_000, spy, gas=gas, closes=[1.0, 2.0, 3.0])

    # First range: the 3 warm-up prices. Rebalance at block 400: one more price.
    assert spy.seen == [3, 4]
    assert result["rebalances"] == 1


def test_path_ends_at_the_final_values():
    swaps = [swap_at(195000, 1), swap_at(195000, 400, amount0=10**9), swap_at(195000, 800)]

    result = simulate(swaps, 100_000, FixedRange(0.1))

    block, lp_value, hodl_value = result["path"][-1]
    assert block == 800
    assert lp_value == pytest.approx(result["position_usd"] + result["fees_usd"])
    assert hodl_value == pytest.approx(result["hodl_usd"])
    assert len(result["path"]) == 3  # checks at blocks 400 and 800, then the end


def test_hourly_closes_keep_the_last_price_of_each_bucket():
    swaps = [swap_at(195000, 10), swap_at(195100, 20), swap_at(196000, 310)]

    closes = hourly_closes(swaps)

    assert len(closes) == 2
    assert closes[0] == pytest.approx(10**12 / 1.0001**195100, rel=1e-6)


@pytest.mark.parametrize(
    "name, expected",
    [("full_range", FullRange), ("fixed", FixedRange), ("rebalance", FixedRange), ("vol_scaled", VolScaled)],
)
def test_build_returns_each_strategy(name, expected):
    strategy = build(name, width=0.1, k=2)

    assert isinstance(strategy, expected)
    assert strategy.rebalances == (name in ("rebalance", "vol_scaled"))
