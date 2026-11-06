"""Download Uniswap v3 pool events from the Etherscan API V2."""

import argparse
import calendar
import os
import statistics
import time

import requests
from dotenv import load_dotenv

from lpbacktest.storage import connect, get_checkpoint, insert_events, set_checkpoint

API_URL = "https://api.etherscan.io/v2/api"
POOL = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SET_FEE_PROTOCOL_TOPIC = "0x973d8d92bb299f4af6ce49b52a8adb85ae46b9f214c4c4fc06ac77401237b133"
MINT_TOPIC = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
BURN_TOPIC = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"
SLOT0_SELECTOR = "0x3850c7bd"
START_BLOCK = 12_370_000  # before the pool's first swap (block 12376891)
PAGE_SIZE = 1000  # free tier limit per request
GAS_GRID_BLOCKS = 300  # about one hour of blocks

POSITION_MANAGER = "0xc36442b4a4522e871399cd717abdd847ab11fe88"
SWAP_ROUTERS = (
    "0xe592427a0aece92de3edee1f18e0157c05861564",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",
)
GAS_OPERATIONS = {
    "mint": (MINT_TOPIC, (POSITION_MANAGER,)),
    "burn": (BURN_TOPIC, (POSITION_MANAGER,)),
    "swap": (SWAP_TOPIC, SWAP_ROUTERS),
}
GAS_SAMPLE_BLOCKS = (13_000_000, 15_000_000, 17_000_000, 19_000_000, 21_000_000, 23_000_000, 25_000_000)
GAS_SAMPLE_WINDOW = 20_000  # blocks, about three days
GAS_SAMPLE_SIZE = 30  # transactions per operation and window
GAS_MAX_RECEIPTS = 60  # receipts read per operation and window

CALL_INTERVAL = 0.4  # seconds, stays under 3 calls/s
RETRY_DELAYS = (5, 15, 60, 300, 900, 1800, 1800)  # seconds, about 1h25 in total
TEMPORARY_ERRORS = ("invalid api key", "rate limit")


def get_api_key():
    """Read the Etherscan key from .env."""
    load_dotenv()
    return os.environ["ETHERSCAN_API_KEY"]


def call_api(params):
    """Call Etherscan, waiting and retrying on temporary errors."""
    params = {**params, "chainid": 1, "apikey": get_api_key()}

    for delay in (*RETRY_DELAYS, None):
        try:
            response = requests.get(API_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            problem = str(error)
        else:
            time.sleep(CALL_INTERVAL)
            result = data.get("result")
            is_hex = isinstance(result, str) and result.startswith("0x")
            if isinstance(result, (list, dict)) or is_hex or data.get("status") == "1":
                return result  # an empty list means no events
            problem = str(result if result is not None else data)
            if not any(text in problem.lower() for text in TEMPORARY_ERRORS):
                raise RuntimeError(f"Etherscan error: {problem}")

        if delay is None:
            raise RuntimeError(f"Etherscan kept failing: {problem}")
        print(f"Temporary error: {problem}. Waiting {delay} s.")
        time.sleep(delay)


def block_at_date(date):
    """Return the last block before 00:00 UTC of a YYYY-MM-DD date."""
    timestamp = calendar.timegm(time.strptime(date, "%Y-%m-%d"))
    result = call_api(
        {
            "module": "block",
            "action": "getblocknobytime",
            "timestamp": timestamp,
            "closest": "before",
        }
    )
    return int(result)


def fetch_logs(topic, from_block, to_block, address=POOL, topic1=None):
    """Return up to PAGE_SIZE raw logs of one event, oldest first."""
    params = {
        "module": "logs",
        "action": "getLogs",
        "address": address,
        "topic0": topic,
        "fromBlock": from_block,
        "toBlock": to_block,
        "page": 1,
        "offset": PAGE_SIZE,
    }
    if topic1 is not None:
        params.update(topic1=topic1, topic0_1_opr="and")
    return call_api(params)


def fetch_all_logs(topic, from_block, to_block, address=POOL, topic1=None):
    """Return every log of one event in a block range, without duplicates."""
    found = {}
    while from_block <= to_block:
        logs = fetch_logs(topic, from_block, to_block, address, topic1)
        for log in logs:
            found[(log["blockNumber"], log["logIndex"])] = log
        if len(logs) < PAGE_SIZE:
            break
        last_block = hex_to_int(logs[-1]["blockNumber"])
        if last_block == from_block:
            raise RuntimeError(f"Block {last_block} has more events than one page")
        from_block = last_block
    return list(found.values())


def fetch_swap_logs(from_block, to_block):
    """Return up to PAGE_SIZE raw Swap logs, oldest first."""
    return fetch_logs(SWAP_TOPIC, from_block, to_block)


def hex_to_int(value):
    """Convert an Etherscan hex field. Etherscan writes zero as "0x"."""
    return int(value, 16) if value != "0x" else 0


def to_signed(word):
    """Convert a 32-byte hex word to a signed integer."""
    value = int(word, 16)
    return value - 2**256 if value >= 2**255 else value


def decode_swap(log):
    """Convert a raw Swap log to a row of the swaps table."""
    data = log["data"][2:]
    words = [data[i : i + 64] for i in range(0, len(data), 64)]

    return {
        "block_number": hex_to_int(log["blockNumber"]),
        "log_index": hex_to_int(log["logIndex"]),
        "tx_hash": log["transactionHash"],
        "block_time": hex_to_int(log["timeStamp"]),
        "sender": "0x" + log["topics"][1][-40:],
        "recipient": "0x" + log["topics"][2][-40:],
        "amount0": str(to_signed(words[0])),
        "amount1": str(to_signed(words[1])),
        "sqrt_price_x96": str(int(words[2], 16)),
        "liquidity": str(int(words[3], 16)),
        "tick": to_signed(words[4]),
    }


def decode_fee_protocol(log):
    """Convert a raw SetFeeProtocol log to a row of its table."""
    data = log["data"][2:]
    words = [int(data[i : i + 64], 16) for i in range(0, len(data), 64)]

    return {
        "block_number": hex_to_int(log["blockNumber"]),
        "log_index": hex_to_int(log["logIndex"]),
        "tx_hash": log["transactionHash"],
        "block_time": hex_to_int(log["timeStamp"]),
        "fee0_old": words[0],
        "fee1_old": words[1],
        "fee0_new": words[2],
        "fee1_new": words[3],
    }


def decode_slot0_fee_protocol(result):
    """Return (fee0, fee1) from an eth_call to slot0()."""
    data = result[2:]
    value = int(data[5 * 64 : 6 * 64], 16)
    return value % 16, value >> 4


def fetch_slot0_fee_protocol():
    """Return the pool's current (fee0, fee1) protocol fee divisors."""
    result = call_api(
        {
            "module": "proxy",
            "action": "eth_call",
            "to": POOL,
            "data": SLOT0_SELECTOR,
            "tag": "latest",
        }
    )
    return decode_slot0_fee_protocol(result)


def collect_fee_protocol(conn):
    """Download every SetFeeProtocol event. There are too few to need paging."""
    logs = fetch_logs(SET_FEE_PROTOCOL_TOPIC, START_BLOCK, "latest")
    if len(logs) >= PAGE_SIZE:
        raise RuntimeError("More SetFeeProtocol events than one page")
    rows = [decode_fee_protocol(log) for log in logs]
    insert_events(conn, "fee_protocol_changes", rows)
    return rows


def fetch_receipt(tx_hash):
    """Return the receipt of a transaction."""
    return call_api(
        {
            "module": "proxy",
            "action": "eth_getTransactionReceipt",
            "txhash": tx_hash,
        }
    )


def changes_liquidity(log):
    """Return False for a Burn of zero, which only updates a position's fees."""
    return int(log["data"][2:66], 16) > 0


def collect_gas_used(conn):
    """Save gasUsed of real mint, burn and swap transactions. Safe to resume."""
    for start in GAS_SAMPLE_BLOCKS:
        for op, (topic, contracts) in GAS_OPERATIONS.items():
            logs = fetch_logs(topic, start, start + GAS_SAMPLE_WINDOW)
            if op == "burn":
                logs = [log for log in logs if changes_liquidity(log)]
            tx_hashes = list(dict.fromkeys(log["transactionHash"] for log in logs))

            saved = 0
            for tx_hash in tx_hashes[:GAS_MAX_RECEIPTS]:
                if saved == GAS_SAMPLE_SIZE:
                    break
                exists = conn.execute(
                    "SELECT 1 FROM tx_gas WHERE op = ? AND tx_hash = ?", (op, tx_hash)
                ).fetchone()
                if exists:
                    saved += 1
                    continue
                receipt = fetch_receipt(tx_hash)
                if receipt["to"] in contracts:
                    row = {
                        "op": op,
                        "tx_hash": tx_hash,
                        "block_number": hex_to_int(receipt["blockNumber"]),
                        "gas_used": hex_to_int(receipt["gasUsed"]),
                    }
                    insert_events(conn, "tx_gas", [row])
                    saved += 1
            print(f"Block {start}, {op}: {saved} transactions.")


def fetch_block(block_number):
    """Return a block with its full transactions."""
    return call_api(
        {
            "module": "proxy",
            "action": "eth_getBlockByNumber",
            "tag": hex(block_number),
            "boolean": "true",
        }
    )


def decode_gas(block):
    """Convert a raw block to a row of the gas_prices table."""
    prices = [hex_to_int(tx["gasPrice"]) for tx in block["transactions"]]
    base_fee = block.get("baseFeePerGas")

    return {
        "block_number": hex_to_int(block["number"]),
        "block_time": hex_to_int(block["timestamp"]),
        "base_fee_wei": hex_to_int(base_fee) if base_fee else None,
        "median_price_wei": statistics.median_low(prices) if prices else None,
    }


def collect_gas(conn, to_block):
    """Download one gas sample every GAS_GRID_BLOCKS, resuming from the checkpoint."""
    checkpoint = get_checkpoint(conn, "Gas")
    next_block = START_BLOCK if checkpoint is None else checkpoint + GAS_GRID_BLOCKS
    samples = 0

    for block_number in range(next_block, to_block + 1, GAS_GRID_BLOCKS):
        row = decode_gas(fetch_block(block_number))
        insert_events(conn, "gas_prices", [row])
        set_checkpoint(conn, "Gas", block_number)
        samples += 1

        if samples % 500 == 0:
            print(f"Gas samples: {samples}. Reached block {block_number}.")

    return samples


def collect_swaps(conn, to_block):
    """Download all swaps up to to_block, resuming from the checkpoint."""
    checkpoint = get_checkpoint(conn, "Swap")
    from_block = START_BLOCK if checkpoint is None else checkpoint + 1
    pages = 0

    while from_block <= to_block:
        logs = fetch_swap_logs(from_block, to_block)
        rows = [decode_swap(log) for log in logs]
        insert_events(conn, "swaps", rows)
        pages += 1

        if len(rows) < PAGE_SIZE:
            set_checkpoint(conn, "Swap", to_block)
            break

        # A full page can cut a block in half, so the last block is read again.
        last_block = rows[-1]["block_number"]
        if last_block == from_block:
            raise RuntimeError(f"Block {last_block} has more events than one page")
        set_checkpoint(conn, "Swap", last_block - 1)
        from_block = last_block

        if pages % 50 == 0:
            print(f"Pages: {pages}. Reached block {last_block}.")

    return pages


def main():
    """Download swaps, gas prices or protocol fee changes."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", help="end date, YYYY-MM-DD")
    parser.add_argument("--gas", action="store_true", help="collect gas prices")
    parser.add_argument("--fee-protocol", action="store_true", help="collect fee changes")
    parser.add_argument("--gas-used", action="store_true", help="sample real gasUsed")
    args = parser.parse_args()

    conn = connect()

    if args.fee_protocol:
        for row in collect_fee_protocol(conn):
            print(row)
        print("Current slot0 feeProtocol (fee0, fee1):", fetch_slot0_fee_protocol())
        return

    if args.gas_used:
        collect_gas_used(conn)
        return

    if args.to is None:
        parser.error("--to is required")
    to_block = block_at_date(args.to)

    if args.gas:
        samples = collect_gas(conn, to_block)
        total, first, last = conn.execute(
            "SELECT COUNT(*), MIN(block_number), MAX(block_number) FROM gas_prices"
        ).fetchone()
        print(f"Samples this run: {samples}. Target block: {to_block}.")
        print(f"Gas samples in database: {total}. Blocks: {first} to {last}.")
        return

    pages = collect_swaps(conn, to_block)
    total, first, last = conn.execute(
        "SELECT COUNT(*), MIN(block_number), MAX(block_number) FROM swaps"
    ).fetchone()
    print(f"Pages this run: {pages}. Target block: {to_block}.")
    print(f"Swaps in database: {total}. Blocks: {first} to {last}.")


if __name__ == "__main__":
    main()
