"""Compare the engine's fees with fees that real LP positions collected on-chain."""

import csv
from collections import defaultdict
from pathlib import Path

from lpbacktest.collector import (
    BURN_TOPIC,
    MINT_TOPIC,
    POSITION_MANAGER,
    fetch_all_logs,
    fetch_logs,
    fetch_receipt,
    hex_to_int,
    to_signed,
)
from lpbacktest.engine import amounts_to_usd, replay_real_position, sqrt_price_to_usdc_per_eth
from lpbacktest.storage import load_fee_changes, load_swaps_by_block

INCREASE_TOPIC = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
DECREASE_TOPIC = "0x26f6a048ee9138f2c0ce266f322cb99228e8d619ae2bff30c67f8dcf9d2377b4"
COLLECT_TOPIC = "0x40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01"
OWNER_TOPIC = "0x" + "0" * 24 + POSITION_MANAGER[2:]

SAMPLE_BLOCKS = (13_100_000, 15_100_000, 17_100_000, 19_100_000, 21_100_000, 24_500_000)
MINT_WINDOW = 7_200  # blocks in which a position may start, about one day
HORIZON = 100_000  # blocks searched for its end, about two weeks
MIN_LIFE = 3_000  # blocks, about ten hours, which leaves out JIT liquidity
COLLECT_MARGIN = 50_000  # blocks after the burn in which fees may be collected
POSITIONS_PER_SAMPLE = 5
RESULTS_PATH = Path("results/validation.csv")


def words(log):
    data = log["data"][2:]
    return [data[i : i + 64] for i in range(0, len(data), 64)]


def decode_liquidity_event(log, is_mint):
    """Return the fields of a pool Mint or Burn log that identify a position."""
    fields = words(log)
    return {
        "block": hex_to_int(log["blockNumber"]),
        "tx_hash": log["transactionHash"],
        "tick_lower": to_signed(log["topics"][2][2:]),
        "tick_upper": to_signed(log["topics"][3][2:]),
        "liquidity": int(fields[1 if is_mint else 0], 16),
    }


def match_positions(mints, burns, last_mint_block):
    """Return positions with exactly one mint and one full burn at their ticks."""
    grouped = defaultdict(lambda: ([], []))
    for mint in mints:
        grouped[(mint["tick_lower"], mint["tick_upper"])][0].append(mint)
    for burn in burns:
        if burn["liquidity"] > 0:
            grouped[(burn["tick_lower"], burn["tick_upper"])][1].append(burn)

    positions = []
    for ticks, (found_mints, found_burns) in grouped.items():
        if len(found_mints) != 1 or len(found_burns) != 1:
            continue
        mint, burn = found_mints[0], found_burns[0]
        life = burn["block"] - mint["block"]
        if mint["block"] > last_mint_block or mint["liquidity"] != burn["liquidity"]:
            continue
        if life >= MIN_LIFE:
            positions.append(
                {
                    "tick_lower": ticks[0],
                    "tick_upper": ticks[1],
                    "liquidity": mint["liquidity"],
                    "mint_block": mint["block"],
                    "burn_block": burn["block"],
                    "mint_tx": mint["tx_hash"],
                }
            )
    return sorted(positions, key=lambda p: p["mint_block"] - p["burn_block"])


def find_positions(start_block):
    """Return real positions that opened and fully closed around a block."""
    end_block = start_block + HORIZON
    mints = fetch_all_logs(MINT_TOPIC, start_block, end_block, topic1=OWNER_TOPIC)
    burns = fetch_all_logs(BURN_TOPIC, start_block, end_block, topic1=OWNER_TOPIC)
    return match_positions(
        [decode_liquidity_event(log, True) for log in mints],
        [decode_liquidity_event(log, False) for log in burns],
        start_block + MINT_WINDOW,
    )[: POSITIONS_PER_SAMPLE]


def token_id_of(position):
    """Return the tokenId topic of a position, from its mint transaction."""
    for log in fetch_receipt(position["mint_tx"])["logs"]:
        is_increase = log["address"].lower() == POSITION_MANAGER and log["topics"][0] == INCREASE_TOPIC
        if is_increase and int(words(log)[0], 16) == position["liquidity"]:
            return log["topics"][1]
    return None


def real_fees(position, token_id):
    """Return the raw (fee0, fee1) collected, or None if the position is ambiguous."""
    first, last = position["mint_block"], position["burn_block"] + COLLECT_MARGIN

    def logs(topic):
        return fetch_all_logs(topic, first, last, POSITION_MANAGER, token_id)

    if len(logs(INCREASE_TOPIC)) != 1:
        return None
    decreases = logs(DECREASE_TOPIC)
    collects = logs(COLLECT_TOPIC)
    if not decreases or not collects:
        return None

    # A Collect pays back principal and fees. Its data is recipient, amount0, amount1.
    fee0 = sum(int(words(log)[1], 16) for log in collects)
    fee1 = sum(int(words(log)[2], 16) for log in collects)
    fee0 -= sum(int(words(log)[1], 16) for log in decreases)
    fee1 -= sum(int(words(log)[2], 16) for log in decreases)
    return fee0, fee1


def compare(conn, position, real):
    """Return one result row, with both fee totals in USD at the final price."""
    swaps = load_swaps_by_block(conn, position["mint_block"] + 1, position["burn_block"] - 1)
    simulated = replay_real_position(
        swaps,
        position["liquidity"],
        position["tick_lower"],
        position["tick_upper"],
        load_fee_changes(conn),
    )
    price = sqrt_price_to_usdc_per_eth(int(swaps[-1][2]))
    real_usd = amounts_to_usd(real[0], real[1], price)
    simulated_usd = amounts_to_usd(simulated[0], simulated[1], price)

    return {
        "mint_block": position["mint_block"],
        "burn_block": position["burn_block"],
        "tick_lower": position["tick_lower"],
        "tick_upper": position["tick_upper"],
        "real_fee_usd": round(real_usd, 2),
        "simulated_fee_usd": round(simulated_usd, 2),
        "error_pct": round((simulated_usd / real_usd - 1) * 100, 2) if real_usd else None,
    }


def validate(conn):
    """Validate the engine on real positions and save the results."""
    rows = []
    for start_block in SAMPLE_BLOCKS:
        positions = find_positions(start_block)
        print(f"Block {start_block}: {len(positions)} candidate positions.")
        for position in positions:
            token_id = token_id_of(position)
            real = real_fees(position, token_id) if token_id else None
            if real is None:
                print("  Skipped an ambiguous position.")
                continue
            row = compare(conn, position, real)
            rows.append(row)
            print(
                f"  Real ${row['real_fee_usd']:,.2f}, simulated "
                f"${row['simulated_fee_usd']:,.2f}, error {row['error_pct']}%."
            )

    if rows:
        RESULTS_PATH.parent.mkdir(exist_ok=True)
        with RESULTS_PATH.open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)
    return rows
