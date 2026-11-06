from lpbacktest.storage import (
    connect,
    get_checkpoint,
    insert_events,
    load_fee_changes,
    load_gas_prices,
    load_swaps,
    set_checkpoint,
)

SWAP = {
    "block_number": 12376891,
    "log_index": 7,
    "tx_hash": "0xabc",
    "block_time": 1620252900,
    "sender": "0x1",
    "recipient": "0x2",
    "amount0": "-119744094",
    "amount1": "35000000000000000",
    "sqrt_price_x96": str(2**159),  # larger than a 64-bit integer
    "liquidity": str(2**120),
    "tick": 194996,
}


def test_insert_is_idempotent(tmp_path):
    conn = connect(tmp_path / "test.sqlite")

    assert insert_events(conn, "swaps", [SWAP]) == 1
    assert insert_events(conn, "swaps", [SWAP]) == 0
    assert conn.execute("SELECT COUNT(*) FROM swaps").fetchone()[0] == 1


def test_large_integers_keep_exact_value(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    insert_events(conn, "swaps", [SWAP])

    stored = conn.execute("SELECT sqrt_price_x96 FROM swaps").fetchone()[0]
    assert int(stored) == 2**159


def test_checkpoint_updates(tmp_path):
    conn = connect(tmp_path / "test.sqlite")

    assert get_checkpoint(conn, "Swap") is None
    set_checkpoint(conn, "Swap", 100)
    set_checkpoint(conn, "Swap", 200)
    assert get_checkpoint(conn, "Swap") == 200


def test_load_swaps_respects_the_time_range(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    insert_events(
        conn,
        "gas_prices",
        [
            {"block_number": 100, "block_time": 1000, "base_fee_wei": None, "median_price_wei": 1},
            {"block_number": 400, "block_time": 4000, "base_fee_wei": None, "median_price_wei": 1},
        ],
    )
    rows = [
        {**SWAP, "block_number": block, "block_time": block * 10, "tick": block}
        for block in (150, 200, 300, 500)
    ]
    insert_events(conn, "swaps", rows)

    swaps = list(load_swaps(conn, 2000, 5000))

    assert [swap[4] for swap in swaps] == [200, 300]


def test_load_fee_changes_converts_the_divisor_to_a_share(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    row = {
        "block_number": 100,
        "log_index": 1,
        "tx_hash": "0xabc",
        "block_time": 1000,
        "fee0_old": 0,
        "fee1_old": 0,
        "fee0_new": 4,
        "fee1_new": 4,
    }
    insert_events(conn, "fee_protocol_changes", [row])

    assert load_fee_changes(conn) == [(100, 0.25)]


def test_load_gas_prices_repeats_the_last_price(tmp_path):
    conn = connect(tmp_path / "test.sqlite")
    prices = [(100, None), (400, 10), (700, None), (1000, 30)]
    rows = [
        {"block_number": b, "block_time": b, "base_fee_wei": None, "median_price_wei": p}
        for b, p in prices
    ]
    insert_events(conn, "gas_prices", rows)

    assert load_gas_prices(conn) == ([400, 700, 1000], [10, 10, 30])
