"""Command line interface: python -m lpbacktest run ..."""

import argparse
import calendar
import time

from lpbacktest.engine import simulate_fixed_range
from lpbacktest.storage import connect, load_swaps


def to_timestamp(date):
    return calendar.timegm(time.strptime(date, "%Y-%m-%d"))


def run(args):
    conn = connect()
    swaps = load_swaps(conn, to_timestamp(args.start), to_timestamp(args.end))

    started = time.perf_counter()
    result = simulate_fixed_range(swaps, args.usd, args.width, args.protocol_share)
    elapsed = time.perf_counter() - started

    share_in_range = result["swaps_in_range"] / result["swaps"]
    print(f"Period: {args.start} to {args.end}. Position: ${args.usd:,.0f}, +/-{args.width:.0%}.")
    print(f"ETH price: ${result['entry_price']:,.2f} to ${result['end_price']:,.2f}.")
    print(f"Ticks: {result['tick_lower']} to {result['tick_upper']}.")
    print(f"Swaps: {result['swaps']:,}. In range: {share_in_range:.1%}.")
    print(f"Fees: ${result['fees_usd']:,.2f}.")
    print(f"Position value: ${result['position_usd']:,.2f}.")
    print(f"HODL value: ${result['hodl_usd']:,.2f}.")
    print(f"Impermanent loss: ${result['impermanent_loss_usd']:,.2f}.")
    print(f"Net vs HODL: ${result['net_vs_hodl_usd']:,.2f}.")
    print(f"Protocol fee share: {args.protocol_share:.0%}. No gas or rebalancing yet.")
    print(f"Replay time: {elapsed:.1f} s.")


def main():
    parser = argparse.ArgumentParser(prog="lpbacktest")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="replay a fixed range position")
    run_parser.add_argument("--start", required=True, help="start date, YYYY-MM-DD")
    run_parser.add_argument("--end", required=True, help="end date, YYYY-MM-DD")
    run_parser.add_argument("--width", type=float, default=0.1, help="0.1 means +/-10%%")
    run_parser.add_argument("--usd", type=float, default=100_000)
    run_parser.add_argument("--protocol-share", type=float, default=0.0)
    run_parser.set_defaults(handler=run)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
