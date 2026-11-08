"""Performance metrics for a replayed position and its benchmarks."""

import math
import statistics

DAYS_PER_YEAR = 365
CHECKS_PER_YEAR = 24 * DAYS_PER_YEAR  # the engine records about one value per hour


def series_metrics(values, start_value):
    """Return total return, max drawdown and Sharpe of a value series.

    values are the hourly values after start_value. Sharpe is annualized with a
    zero risk-free rate, as the cash benchmark holds USDC without yield. It is None
    when the series never moves.
    """
    series = [start_value, *values]
    returns = [later / earlier - 1 for earlier, later in zip(series, series[1:])]

    peak = series[0]
    max_drawdown = 0.0
    for value in series:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, 1 - value / peak)

    sharpe = None
    if len(returns) > 1 and statistics.stdev(returns) > 0:
        sharpe = statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(CHECKS_PER_YEAR)

    return {
        "return": series[-1] / start_value - 1,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
    }


def benchmark_values(path, usd, entry_price):
    """Return the hourly values of the benchmarks along the replay path."""
    return {
        "lp": [lp for _, _, lp, _ in path],
        "hodl_same_tokens": [hodl for _, _, _, hodl in path],
        "hodl_50_50": [usd / 2 + usd / 2 * price / entry_price for _, price, _, _ in path],
        "usdc": [usd for _ in path],
    }


def summarize(result, usd, days):
    """Return the metrics of a simulate() result over a period of days."""
    costs = result["gas_usd"] + result["swap_fee_usd"] + result["price_impact_usd"]
    summary = {
        "net_vs_hodl_pct": result["net_vs_hodl_usd"] / usd,
        "fee_apr": result["fees_usd"] / usd * DAYS_PER_YEAR / days,
        "position_vs_hodl_pct": result["position_vs_hodl_usd"] / usd,
        "costs_pct": costs / usd,
        "share_of_swaps_in_range": result["swaps_in_range"] / result["swaps"],
        "rebalances": result["rebalances"],
    }
    values = benchmark_values(result["path"], usd, result["entry_price"])
    for name, series in values.items():
        for metric, value in series_metrics(series, usd).items():
            summary[f"{name}_{metric}"] = value
    return summary
