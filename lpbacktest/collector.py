"""Download Uniswap v3 pool events from the Etherscan API V2."""

import argparse
import calendar
import os
import time

import requests
from dotenv import load_dotenv

from lpbacktest.storage import connect, get_checkpoint, insert_events, set_checkpoint

API_URL = "https://api.etherscan.io/v2/api"
POOL = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
START_BLOCK = 12_370_000  # before the pool's first swap (block 12376891)
PAGE_SIZE = 1000  # free tier limit per request

CALL_INTERVAL = 0.4  # seconds, stays under 3 calls/s
RETRY_DELAYS = (5, 15, 60, 300)  # seconds
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
            if isinstance(data["result"], list) or data.get("status") == "1":
                return data["result"]  # an empty list means no events
            problem = str(data["result"])
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


def fetch_swap_logs(from_block, to_block):
    """Return up to PAGE_SIZE raw Swap logs, oldest first."""
    return call_api(
        {
            "module": "logs",
            "action": "getLogs",
            "address": POOL,
            "topic0": SWAP_TOPIC,
            "fromBlock": from_block,
            "toBlock": to_block,
            "page": 1,
            "offset": PAGE_SIZE,
        }
    )


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
    """Download swaps from the pool's start up to a date."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True, help="end date, YYYY-MM-DD")
    args = parser.parse_args()

    conn = connect()
    to_block = block_at_date(args.to)
    pages = collect_swaps(conn, to_block)

    total, first, last = conn.execute(
        "SELECT COUNT(*), MIN(block_number), MAX(block_number) FROM swaps"
    ).fetchone()
    print(f"Pages this run: {pages}. Target block: {to_block}.")
    print(f"Swaps in database: {total}. Blocks: {first} to {last}.")


if __name__ == "__main__":
    main()
