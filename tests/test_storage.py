from lpbacktest.storage import connect, get_checkpoint, insert_events, set_checkpoint

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
