"""Calibrate strategy parameters on one period and test them on a later one.

Each strategy is replayed in rolling windows: a new position opens on the first day
of every month and is held for HORIZON_DAYS. Many start and end dates keep a result
from depending on where one long period happens to begin and end.
"""

import calendar
import csv
import datetime
import math
import statistics
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from lpbacktest.engine import hourly_closes, simulate
from lpbacktest.metrics import summarize
from lpbacktest.storage import DEFAULT_DB_PATH, connect, load_fee_changes, load_gas_prices, load_swaps
from lpbacktest.strategies import build

WARM_UP_SECONDS = 8 * 86_400  # a week of hourly prices for volatility, plus margin
# The pool opened on 2021-05-05, but until mid-June 2021 less than $1M moved its price
# by 1% (about $1-9k in late May), so a $100k position would have been a large share
# of the pool. The analysis starts once it became the main USDC/WETH pool.
TRAIN = ("2021-07-01", "2024-01-01")
TEST = ("2024-01-01", "2026-11-01")
HORIZON_DAYS = 90
REGIMES = (
    ("2021 H2", "2021-07-01", "2022-01-01"),
    ("2022", "2022-01-01", "2023-01-01"),
    ("2023-24", "2023-01-01", "2025-01-01"),
    ("2025-26", "2025-01-01", "2026-11-01"),
)
SIZES = (10_000, 100_000, 1_000_000)
CANDIDATES = (
    [("full_range", None)]
    + [("fixed", width) for width in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 0.7, 0.9)]
    + [("rebalance", width) for width in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 0.7, 0.9)]
    + [("vol_scaled", k) for k in (0.5, 1, 2, 3, 4, 6, 8, 12)]
)
RESULTS_DIR = Path("results")
WINDOW_COLUMNS = [
    "period",
    "start",
    "end",
    "strategy",
    "param",
    "usd",
    "net_vs_hodl_pct",
    "net_vs_hodl_without_thin_pct",
    "lp_return",
    "lp_max_drawdown",
    "lp_sharpe",
    "fee_apr",
    "costs_pct",
    "rebalances",
    "share_of_swaps_in_range",
    "hodl_same_tokens_return",
    "hodl_50_50_return",
]
SUMMARY_COLUMNS = [
    "period",
    "strategy",
    "param",
    "usd",
    "windows",
    "mean_vs_hodl",
    "t_stat",
    "information_ratio",
    "median_vs_hodl",
    "share_beating_hodl",
    "worst_vs_hodl",
    "mean_vs_hodl_without_thin",
    "mean_costs",
    "mean_fee_apr",
    "mean_lp_max_drawdown",
    "mean_rebalances",
]


def to_timestamp(date):
    return calendar.timegm(time.strptime(date, "%Y-%m-%d"))


def add_months(date, months):
    month = date.month - 1 + months
    return date.replace(year=date.year + month // 12, month=month % 12 + 1)


def windows(period, horizon_days=HORIZON_DAYS):
    """Return (start, end) dates of windows that open monthly and fit in the period."""
    first = datetime.date.fromisoformat(period[0])
    last = datetime.date.fromisoformat(period[1])
    found = []
    start = first
    while start + datetime.timedelta(days=horizon_days) <= last:
        end = start + datetime.timedelta(days=horizon_days)
        found.append((start.isoformat(), end.isoformat()))
        start = add_months(start, 1)
    return found


def make_strategy(name, param):
    if name == "vol_scaled":
        return build(name, k=param)
    return build(name, width=param if param is not None else 0.1)


def run_one(job):
    """Replay one strategy over one period. Runs in a worker process."""
    period, start_date, end_date, name, param, usd, db_path = job
    conn = connect(db_path)
    start, end = to_timestamp(start_date), to_timestamp(end_date)
    closes = hourly_closes(load_swaps(conn, start - WARM_UP_SECONDS, start))
    result = simulate(
        load_swaps(conn, start, end),
        usd,
        make_strategy(name, param),
        load_fee_changes(conn),
        load_gas_prices(conn),
        closes,
    )
    summary = summarize(result, usd, (end - start) / 86_400)
    without_thin = result["net_vs_hodl_usd"] - result["thin_liquidity_fees_usd"]
    row = {
        "period": period,
        "start": start_date,
        "end": end_date,
        "strategy": name,
        "param": param,
        "usd": usd,
        "net_vs_hodl_without_thin_pct": without_thin / usd,
    }
    row.update({column: summary[column] for column in WINDOW_COLUMNS if column in summary})
    return row


def run_jobs(jobs, workers):
    if workers == 1:
        return [run_one(job) for job in jobs]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run_one, jobs))


def newey_west_t(values, lags):
    """Return the t-statistic of the mean, corrected for overlapping windows.

    Windows of 90 days opened monthly share data with the next two, so their
    results are correlated. Newey-West weights those autocovariances (Bartlett
    kernel) instead of treating each window as independent.
    """
    n = len(values)
    mean = statistics.mean(values)
    deviations = [value - mean for value in values]
    variance = sum(d * d for d in deviations) / n
    for lag in range(1, min(lags, n - 1) + 1):
        weight = 1 - lag / (lags + 1)
        covariance = sum(deviations[i] * deviations[i - lag] for i in range(lag, n)) / n
        variance += 2 * weight * covariance
    if variance <= 0:
        return None
    return mean / math.sqrt(variance / n)


def aggregate(rows, horizon_days=HORIZON_DAYS):
    """Return one summary per strategy and parameter over its windows, best mean first."""
    grouped = {}
    for row in rows:
        key = (row["period"], row["strategy"], row["param"], row["usd"])
        grouped.setdefault(key, []).append(row)

    lags = math.ceil(horizon_days / 30) - 1  # windows open monthly
    summaries = []
    for (period, strategy, param, usd), group in grouped.items():
        group = sorted(group, key=lambda row: row["start"])
        excess = [row["net_vs_hodl_pct"] for row in group]
        spread = statistics.stdev(excess) if len(excess) > 1 else 0.0
        summaries.append(
            {
                "period": period,
                "strategy": strategy,
                "param": param,
                "usd": usd,
                "windows": len(group),
                "mean_vs_hodl": statistics.mean(excess),
                "t_stat": newey_west_t(excess, lags) if len(excess) > 1 else None,
                "information_ratio": statistics.mean(excess) / spread if spread > 0 else None,
                "median_vs_hodl": statistics.median(excess),
                "share_beating_hodl": sum(value > 0 for value in excess) / len(excess),
                "worst_vs_hodl": min(excess),
                "mean_vs_hodl_without_thin": statistics.mean(
                    row["net_vs_hodl_without_thin_pct"] for row in group
                ),
                "mean_costs": statistics.mean(row["costs_pct"] for row in group),
                "mean_fee_apr": statistics.mean(row["fee_apr"] for row in group),
                "mean_lp_max_drawdown": statistics.mean(row["lp_max_drawdown"] for row in group),
                "mean_rebalances": statistics.mean(row["rebalances"] for row in group),
            }
        )
    return sorted(summaries, key=lambda summary: summary["mean_vs_hodl"], reverse=True)


def choose_best(summaries):
    """Return the summary with the highest mean result vs HODL for each strategy.

    The information ratio is not used: with a negative mean, a larger spread moves
    it toward zero, so it would reward inconsistency.
    """
    best = {}
    for summary in summaries:
        current = best.get(summary["strategy"])
        if current is None or summary["mean_vs_hodl"] > current["mean_vs_hodl"]:
            best[summary["strategy"]] = summary
    return best


def save(rows, columns, path):
    path.parent.mkdir(exist_ok=True)
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def calibrate(
    usd=100_000,
    candidates=CANDIDATES,
    train=TRAIN,
    test=TEST,
    db_path=DEFAULT_DB_PATH,
    workers=4,
    results_dir=RESULTS_DIR,
    horizon_days=HORIZON_DAYS,
):
    """Choose each strategy's parameter on train windows, then replay it on test windows.

    The parameter with the highest mean result vs HODL across windows wins.
    """
    results_dir = Path(results_dir)
    jobs = [
        ("train", start, end, name, param, usd, db_path)
        for start, end in windows(train, horizon_days)
        for name, param in candidates
    ]
    train_windows = run_jobs(jobs, workers)
    train_summary = aggregate(train_windows, horizon_days)
    save(train_windows, WINDOW_COLUMNS, results_dir / "calibration_train_windows.csv")
    save(train_summary, SUMMARY_COLUMNS, results_dir / "calibration_train.csv")

    best = choose_best(train_summary)
    jobs = [
        ("test", start, end, summary["strategy"], summary["param"], usd, db_path)
        for start, end in windows(test, horizon_days)
        for summary in best.values()
    ]
    test_windows = run_jobs(jobs, workers)
    test_summary = aggregate(test_windows, horizon_days)
    save(test_windows, WINDOW_COLUMNS, results_dir / "calibration_test_windows.csv")
    save(test_summary, SUMMARY_COLUMNS, results_dir / "calibration_test.csv")
    return train_summary, test_summary


def load_choices(path):
    """Return [(strategy, param)] chosen in a saved train summary."""
    with open(path, newline="") as file:
        rows = list(csv.DictReader(file))
    for row in rows:
        row["param"] = float(row["param"]) if row["param"] else None
        row["mean_vs_hodl"] = float(row["mean_vs_hodl"])
    return [(row["strategy"], row["param"]) for row in choose_best(rows).values()]


def compare(
    choices=None,
    sizes=SIZES,
    regimes=REGIMES,
    db_path=DEFAULT_DB_PATH,
    workers=4,
    results_dir=RESULTS_DIR,
    horizon_days=HORIZON_DAYS,
):
    """Replay the chosen strategies by market regime and position size."""
    results_dir = Path(results_dir)
    if choices is None:
        choices = load_choices(results_dir / "calibration_train.csv")
    jobs = [
        (regime, start, end, name, param, usd, db_path)
        for regime, first, last in regimes
        for start, end in windows((first, last), horizon_days)
        for name, param in choices
        for usd in sizes
    ]
    rows = run_jobs(jobs, workers)
    order = {regime: i for i, (regime, _, _) in enumerate(regimes)}
    summary = sorted(
        aggregate(rows, horizon_days),
        key=lambda row: (order[row["period"]], row["strategy"], row["usd"]),
    )
    save(rows, WINDOW_COLUMNS, results_dir / "comparison_windows.csv")
    save(summary, SUMMARY_COLUMNS, results_dir / "comparison.csv")
    return summary
