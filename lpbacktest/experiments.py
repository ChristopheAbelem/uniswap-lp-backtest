"""Calibrate strategy parameters on one period and test them on a later one.

Each strategy is replayed in rolling windows: a new position opens on the first day
of every month and is held for HORIZON_DAYS. Many start and end dates keep a result
from depending on where one long period happens to begin and end.
"""

import calendar
import csv
import datetime
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
CANDIDATES = (
    [("full_range", None)]
    + [("fixed", width) for width in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5)]
    + [("rebalance", width) for width in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5)]
    + [("vol_scaled", k) for k in (0.5, 1, 2, 3, 4, 6)]
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
    "windows",
    "information_ratio",
    "mean_vs_hodl",
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


def aggregate(rows):
    """Return one summary per strategy and parameter over its windows."""
    grouped = {}
    for row in rows:
        grouped.setdefault((row["period"], row["strategy"], row["param"]), []).append(row)

    summaries = []
    for (period, strategy, param), group in grouped.items():
        excess = [row["net_vs_hodl_pct"] for row in group]
        spread = statistics.stdev(excess) if len(excess) > 1 else 0.0
        summaries.append(
            {
                "period": period,
                "strategy": strategy,
                "param": param,
                "windows": len(group),
                "information_ratio": statistics.mean(excess) / spread if spread > 0 else None,
                "mean_vs_hodl": statistics.mean(excess),
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
    return sorted(summaries, key=ratio_key, reverse=True)


def ratio_key(summary):
    ratio = summary["information_ratio"]
    return ratio if ratio is not None else float("-inf")


def choose_best(summaries):
    """Return the summary with the highest information ratio for each strategy."""
    best = {}
    for summary in summaries:
        current = best.get(summary["strategy"])
        if current is None or ratio_key(summary) > ratio_key(current):
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

    The parameter with the highest information ratio wins: mean excess return over
    HODL divided by its standard deviation across windows.
    """
    results_dir = Path(results_dir)
    jobs = [
        ("train", start, end, name, param, usd, db_path)
        for start, end in windows(train, horizon_days)
        for name, param in candidates
    ]
    train_windows = run_jobs(jobs, workers)
    train_summary = aggregate(train_windows)
    save(train_windows, WINDOW_COLUMNS, results_dir / "calibration_train_windows.csv")
    save(train_summary, SUMMARY_COLUMNS, results_dir / "calibration_train.csv")

    best = choose_best(train_summary)
    jobs = [
        ("test", start, end, summary["strategy"], summary["param"], usd, db_path)
        for start, end in windows(test, horizon_days)
        for summary in best.values()
    ]
    test_windows = run_jobs(jobs, workers)
    test_summary = aggregate(test_windows)
    save(test_windows, WINDOW_COLUMNS, results_dir / "calibration_test_windows.csv")
    save(test_summary, SUMMARY_COLUMNS, results_dir / "calibration_test.csv")
    return train_summary, test_summary
