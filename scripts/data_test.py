"""Data feasibility test for the Uniswap v3 LP backtester (Etherscan API V2, free tier).

Checks:
  1. The pool address is really USDC/WETH 0.05% (token0, token1, fee via eth_call).
  2. Swap events can be pulled and decoded (first swaps after pool creation + a recent window).
  3. Mint and Burn events exist.
  4. Historical base fee (gas) is available from block headers.

Usage:
  export ETHERSCAN_API_KEY="your_key"
  python data_test.py
"""

import os
from dotenv import load_dotenv
import sys
import time

import requests

load_dotenv()

API = "https://api.etherscan.io/v2/api"
KEY = os.environ["ETHERSCAN_API_KEY"]
POOL = "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

TOPICS = {
    "Swap": "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
    "Mint": "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde",
    "Burn": "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c",
}


def call(retries=3, **params):
    params.update(chainid=1, apikey=KEY)
    for attempt in range(retries):
        r = requests.get(API, params=params, timeout=30)
        r.raise_for_status()
        time.sleep(0.4)  # stay under 3 calls/s
        data = r.json()
        if isinstance(data.get("result"), str) and data.get("status") == "0":
            if attempt < retries - 1:
                time.sleep(2)  # transient errors: wait and retry
                continue
            raise RuntimeError(f"API error: {data}")
        return data["result"]


def eth_call(selector):
    return call(module="proxy", action="eth_call", to=POOL, data=selector, tag="latest")


def block_at(ts):
    return int(call(module="block", action="getblocknobytime", timestamp=ts, closest="before"))


def get_logs(topic, from_block, to_block):
    return call(module="logs", action="getLogs", address=POOL, topic0=TOPICS[topic],
                fromBlock=from_block, toBlock=to_block, page=1, offset=1000)


def signed(word):
    v = int(word, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


def decode_swap(log):
    d = log["data"][2:]
    w = [d[i:i + 64] for i in range(0, len(d), 64)]
    sqrt_p = int(w[2], 16)
    raw = (sqrt_p / 2**96) ** 2           # WETH raw per USDC raw
    usdc_per_eth = 1 / (raw * 1e-12)      # adjust for 6 vs 18 decimals
    return {
        "block": int(log["blockNumber"], 16),
        "time": time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(log["timeStamp"], 16))),
        "amount_usdc": signed(w[0]) / 1e6,
        "amount_weth": signed(w[1]) / 1e18,
        "price_usdc_per_eth": round(usdc_per_eth, 2),
        "liquidity": int(w[3], 16),
        "tick": signed(w[4]),
    }


def main():
    if not KEY:
        sys.exit("Set ETHERSCAN_API_KEY first.")

    print("1) Pool identity")
    token0 = "0x" + eth_call("0x0dfe1681")[-40:]
    token1 = "0x" + eth_call("0xd21220a7")[-40:]
    fee = int(eth_call("0xddca3f43"), 16)
    print(f"   token0={token0} (USDC? {token0 == USDC})")
    print(f"   token1={token1} (WETH? {token1 == WETH})")
    print(f"   fee={fee} (expected 500 = 0.05%)")

    print("\n2) Swap events")
    early = get_logs("Swap", 12_370_000, 12_500_000)
    print(f"   early window: {len(early)} swaps (1000 = page is full, more exist)")
    print("   first swap:", decode_swap(early[0]))

    now = int(time.time())
    b_recent = block_at(now - 3600)
    recent = get_logs("Swap", b_recent, "latest")
    print(f"   last hour: {len(recent)} swaps")
    if recent:
        print("   latest swap:", decode_swap(recent[-1]))

    print("\n3) Mint / Burn events (early window)")
    for t in ("Mint", "Burn"):
        logs = get_logs(t, 12_370_000, 12_500_000)
        print(f"   {t}: {len(logs)} events")

    print("\n4) Gas history (base fee from block header)")
    for label, b in [("~2022-06", 14_900_000), ("~2024-05", 20_000_000)]:
        blk = call(module="proxy", action="eth_getBlockByNumber", tag=hex(b), boolean="false")
        print(f"   {label}: block {b}, base fee {int(blk['baseFeePerGas'], 16) / 1e9:.2f} gwei")

    print("\nDone. If all four sections printed without errors, the data test passes.")


if __name__ == "__main__":
    main()