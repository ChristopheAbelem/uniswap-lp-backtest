from lpbacktest.storage import (
    connect,
    get_checkpoint,
    insert_events,
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
