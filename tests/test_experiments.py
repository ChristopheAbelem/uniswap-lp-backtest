import calendar
import csv
import time

import pytest

from lpbacktest import experiments
from lpbacktest.storage import connect, insert_events


def ts(date):
    return calendar.timegm(time.strptime(date, "%Y-%m-%d"))


def make_db(path):
    """A small pool: one swap per hour from 2021-01-01 to 2021-06-01."""
    conn = connect(path)
    start = ts("2021-01-01")
    swaps, gas = [], []
    for hour in range(151 * 24):
        block = 1_000 + hour * 300
        tick = 195_000 + (hour % 48 - 24) * 10  # a slow wave of about +/-2.4%
        swaps.append(
            {
                "block_number": block,
                "log_index": 0,
                "tx_hash": "0x",
                "block_time": start + hour * 3600,
                "sender": "0x",
                "recipient": "0x",
                "amount0": str(10**10),
                "amount1": "-1",
                "sqrt_price_x96": str(int(1.0001 ** (tick / 2) * 2**96)),
                "liquidity": str(10**20),
                "tick": tick,
            }
        )
        gas.append(
            {"block_number": block, "block_time": start + hour * 3600, "base_fee_wei": 1, "median_price_wei": 10**10}
        )
    insert_events(conn, "swaps", swaps)
    insert_events(conn, "gas_prices", gas)
    conn.close()


def test_windows_open_monthly_and_fit_in_the_period():
    found = experiments.windows(("2021-07-01", "2022-01-01"), horizon_days=90)

    assert found[0] == ("2021-07-01", "2021-09-29")
    assert found[-1] == ("2021-10-01", "2021-12-30")
    assert len(found) == 4


def test_windows_cross_a_year_end():
    found = experiments.windows(("2021-11-01", "2022-06-01"), horizon_days=30)

    assert [start for start, _ in found][:3] == ["2021-11-01", "2021-12-01", "2022-01-01"]


def test_calibrate_chooses_on_train_and_replays_on_test(tmp_path):
    db_path = tmp_path / "pool.sqlite"
    make_db(db_path)
    candidates = [("fixed", 0.01), ("fixed", 0.05), ("vol_scaled", 2)]

    train, test = experiments.calibrate(
        candidates=candidates,
        train=("2021-01-10", "2021-04-01"),
        test=("2021-04-01", "2021-06-01"),
        db_path=db_path,
        workers=1,
        results_dir=tmp_path,
        horizon_days=30,
    )

    assert len(train) == 3 and all(row["windows"] == 2 for row in train)
    assert {row["strategy"] for row in test} == {"fixed", "vol_scaled"}
    best_fixed = max((r for r in train if r["strategy"] == "fixed"), key=experiments.ratio_key)
    assert next(r for r in test if r["strategy"] == "fixed")["param"] == best_fixed["param"]
    with open(tmp_path / "calibration_test_windows.csv") as file:
        assert len(list(csv.DictReader(file))) == 4  # 2 windows x 2 strategies


def window(strategy, param, excess, costs=0.01):
    return {
        "period": "train",
        "strategy": strategy,
        "param": param,
        "usd": 100_000,
        "net_vs_hodl_pct": excess,
        "net_vs_hodl_without_thin_pct": excess,
        "costs_pct": costs,
        "fee_apr": 0.1,
        "lp_max_drawdown": 0.2,
        "rebalances": 3,
    }


def test_aggregate_measures_consistency_over_windows():
    rows = [window("fixed", 0.1, value) for value in (0.02, -0.01, 0.05)]

    summary = experiments.aggregate(rows)[0]

    assert summary["windows"] == 3
    assert summary["mean_vs_hodl"] == pytest.approx(0.02)
    assert summary["information_ratio"] == pytest.approx(0.02 / 0.03)
    assert summary["share_beating_hodl"] == pytest.approx(2 / 3)
    assert summary["worst_vs_hodl"] == -0.01


def test_choose_best_prefers_steady_excess_over_a_lucky_one():
    rows = [window("fixed", 0.1, value) for value in (0.30, -0.20, 0.25, -0.15)]
    rows += [window("fixed", 0.5, value) for value in (0.02, 0.03, 0.02, 0.03)]

    best = experiments.choose_best(experiments.aggregate(rows))

    assert best["fixed"]["param"] == 0.5


def test_load_choices_reads_the_best_parameters(tmp_path):
    rows = [window("fixed", 0.1, value) for value in (0.30, -0.20, 0.25, -0.15)]
    rows += [window("fixed", 0.5, value) for value in (0.02, 0.03, 0.02, 0.03)]
    rows += [window("full_range", None, value) for value in (0.01, 0.02)]
    path = tmp_path / "calibration_train.csv"
    experiments.save(experiments.aggregate(rows), experiments.SUMMARY_COLUMNS, path)

    assert sorted(experiments.load_choices(path), key=str) == sorted(
        [("fixed", 0.5), ("full_range", None)], key=str
    )


def test_compare_by_regime_and_size(tmp_path):
    db_path = tmp_path / "pool.sqlite"
    make_db(db_path)
    regimes = (("first", "2021-01-10", "2021-03-01"), ("second", "2021-03-01", "2021-06-01"))

    summary = experiments.compare(
        choices=[("fixed", 0.05), ("full_range", None)],
        sizes=(10_000, 100_000),
        regimes=regimes,
        db_path=db_path,
        workers=1,
        results_dir=tmp_path,
        horizon_days=30,
    )

    assert len(summary) == 2 * 2 * 2  # regimes x strategies x sizes
    assert [row["period"] for row in summary[:4]] == ["first"] * 4
    small, large = (
        next(r for r in summary if r["period"] == "first" and r["strategy"] == "fixed" and r["usd"] == usd)
        for usd in (10_000, 100_000)
    )
    # Gas is a fixed amount in dollars, so it weighs more on a smaller position.
    assert small["mean_costs"] > large["mean_costs"]
