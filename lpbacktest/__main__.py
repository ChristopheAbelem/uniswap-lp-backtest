"""Command line interface: python -m lpbacktest run ..."""

import argparse
import calendar
import time

from lpbacktest.engine import hourly_closes, simulate
from lpbacktest.metrics import summarize
from lpbacktest.storage import connect, load_fee_changes, load_gas_prices, load_swaps
from lpbacktest.strategies import build

WARM_UP_SECONDS = 8 * 86_400  # a week of hourly prices for volatility, plus margin


def to_timestamp(date):
    return calendar.timegm(time.strptime(date, "%Y-%m-%d"))


def run(args):
    conn = connect()
    start, end = to_timestamp(args.start), to_timestamp(args.end)
    strategy = build(args.strategy, args.width, args.k)
    closes = hourly_closes(load_swaps(conn, start - WARM_UP_SECONDS, start))
    swaps = load_swaps(conn, start, end)
    fee_changes = load_fee_changes(conn)
    gas = load_gas_prices(conn)

    started = time.perf_counter()
    result = simulate(swaps, args.usd, strategy, fee_changes, gas, closes)
    elapsed = time.perf_counter() - started

    share_in_range = result["swaps_in_range"] / result["swaps"]
    print(f"Period: {args.start} to {args.end}. Position: ${args.usd:,.0f}. Strategy: {strategy.name}.")
    print(f"ETH price: ${result['entry_price']:,.2f} to ${result['end_price']:,.2f}.")
    print(f"Ticks: {result['tick_lower']} to {result['tick_upper']}.")
    print(f"Swaps: {result['swaps']:,}. In range: {share_in_range:.1%}.")
    print(f"Fees: ${result['fees_usd']:,.2f}.")
    print(f"Position value: ${result['position_usd']:,.2f}.")
    print(f"HODL value: ${result['hodl_usd']:,.2f}.")
    print(f"Position vs HODL: ${result['position_vs_hodl_usd']:,.2f}.")
    print(f"Net vs HODL: ${result['net_vs_hodl_usd']:,.2f}.")
    thin_fees = result["thin_liquidity_fees_usd"]
    without_thin = result["net_vs_hodl_usd"] - thin_fees
    print(f"Fees above 5% of a swap: ${thin_fees:,.2f}. Net without them: ${without_thin:,.2f}.")
    print(f"Protocol fee share at the end: {result['final_protocol_share']:.0%}.")
    print(f"Rebalances: {result['rebalances']}.")
    print(f"Costs: gas ${result['gas_usd']:,.2f}, swap fees ${result['swap_fee_usd']:,.2f}, price impact ${result['price_impact_usd']:,.2f}.")
    print(f"Replay time: {elapsed:.1f} s.")

    summary = summarize(result, args.usd, (end - start) / 86_400)
    print()
    print(f"Fee APR: {summary['fee_apr']:.1%}. Costs: {summary['costs_pct']:.2%} of the position.")
    print(f"{'':18}{'Return':>10}{'Max drawdown':>14}{'Sharpe':>9}")
    for name, label in [
        ("lp", "LP strategy"),
        ("hodl_same_tokens", "HODL same tokens"),
        ("hodl_50_50", "HODL 50/50"),
        ("usdc", "100% USDC"),
    ]:
        sharpe = summary[f"{name}_sharpe"]
        sharpe_text = "n/a" if sharpe is None else f"{sharpe:.2f}"
        print(
            f"{label:18}{summary[f'{name}_return']:>10.1%}"
            f"{summary[f'{name}_max_drawdown']:>14.1%}{sharpe_text:>9}"
        )


def validate_engine(args):
    from lpbacktest.validation import validate

    rows = validate(connect())
    print(f"Validated positions: {len(rows)}. Saved to results/validation.csv.")


def main():
    parser = argparse.ArgumentParser(prog="lpbacktest")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="replay one strategy")
    run_parser.add_argument("--start", required=True, help="start date, YYYY-MM-DD")
    run_parser.add_argument("--end", required=True, help="end date, YYYY-MM-DD")
    run_parser.add_argument(
        "--strategy",
        choices=["full_range", "fixed", "rebalance", "vol_scaled"],
        default="fixed",
    )
    run_parser.add_argument("--width", type=float, default=0.1, help="0.1 means +/-10%%")
    run_parser.add_argument("--k", type=float, default=2.0, help="daily volatilities per side")
    run_parser.add_argument("--usd", type=float, default=100_000)
    run_parser.set_defaults(handler=run)

    validate_parser = commands.add_parser("validate", help="compare fees with real positions")
    validate_parser.set_defaults(handler=validate_engine)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
