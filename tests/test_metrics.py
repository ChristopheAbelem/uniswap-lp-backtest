import math
import statistics

import pytest

from lpbacktest.engine import simulate
from lpbacktest.metrics import CHECKS_PER_YEAR, benchmark_values, series_metrics, summarize
from lpbacktest.strategies import FixedRange


def swap_at(tick, block, amount0=0):
    sqrt_price_x96 = int(1.0001 ** (tick / 2) * 2**96)
    return (amount0, 0, sqrt_price_x96, 10**18, tick, block)


def test_return_and_max_drawdown():
    metrics = series_metrics([110, 99, 120], 100)

    assert metrics["return"] == pytest.approx(0.20)
    assert metrics["max_drawdown"] == pytest.approx(1 - 99 / 110)


def test_sharpe_is_annualized_from_hourly_returns():
    values = [101, 100, 102, 101]
    returns = [101 / 100 - 1, 100 / 101 - 1, 102 / 100 - 1, 101 / 102 - 1]

    expected = statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(CHECKS_PER_YEAR)
    assert series_metrics(values, 100)["sharpe"] == pytest.approx(expected)


def test_flat_series_has_no_sharpe_and_no_drawdown():
    metrics = series_metrics([100, 100, 100], 100)

    assert metrics["sharpe"] is None
    assert metrics["max_drawdown"] == 0
    assert metrics["return"] == 0


def test_hodl_50_50_doubles_half_of_the_money():
    path = [(1, 2000, 0, 0), (2, 4000, 0, 0)]

    values = benchmark_values(path, 100_000, 2000)

    assert values["hodl_50_50"] == [100_000, 150_000]
    assert values["usdc"] == [100_000, 100_000]


def test_summarize_a_replay():
    swaps = [swap_at(195000, 1)] + [swap_at(195000, 300 * n, amount0=10**9) for n in range(1, 6)]
    result = simulate(swaps, 100_000, FixedRange(0.1))

    summary = summarize(result, 100_000, days=36.5)

    assert summary["fee_apr"] == pytest.approx(result["fees_usd"] / 100_000 * 10)
    assert summary["lp_return"] == pytest.approx(result["fees_usd"] / 100_000, rel=1e-6)
    assert summary["hodl_same_tokens_return"] == pytest.approx(0, abs=1e-9)
    assert summary["share_of_swaps_in_range"] == 1
    assert summary["usdc_return"] == 0
