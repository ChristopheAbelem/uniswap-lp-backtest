import pytest
import requests

from lpbacktest import collector
from lpbacktest.storage import connect, get_checkpoint


def word(value):
    """Encode an integer as a 32-byte hex word."""
    return f"{value % 2**256:064x}"


# Values of the pool's first swap, as printed by scripts/data_test.py.
RAW_SWAP = {
    "blockNumber": hex(12376891),
    "logIndex": "0x",
    "transactionHash": "0xabc",
    "timeStamp": hex(1620252900),
    "topics": [
        collector.SWAP_TOPIC,
        "0x" + "0" * 24 + "1" * 40,
        "0x" + "0" * 24 + "2" * 40,
    ],
    "data": "0x"
    + word(-119744094)
    + word(35 * 10**15)
    + word(2**150)
    + word(345073104699360)
    + word(-194996),
}


class FakeResponse:
    def __init__(self, result):
        self.result = result

    def raise_for_status(self):
        pass

    def json(self):
        return {"result": self.result}


@pytest.fixture
def no_waiting(monkeypatch):
    waits = []
    monkeypatch.setattr(collector.time, "sleep", waits.append)
    monkeypatch.setattr(collector, "get_api_key", lambda: "test-key")
    return waits


def fake_results(monkeypatch, results):
    responses = iter(results)

    def fake_get(*args, **kwargs):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    monkeypatch.setattr(collector.requests, "get", fake_get)


def test_decode_swap():
    row = collector.decode_swap(RAW_SWAP)

    assert row["block_number"] == 12376891
    assert row["log_index"] == 0
    assert row["sender"] == "0x" + "1" * 40
    assert row["amount0"] == "-119744094"
    assert row["amount1"] == str(35 * 10**15)
    assert row["sqrt_price_x96"] == str(2**150)
    assert row["liquidity"] == "345073104699360"
    assert row["tick"] == -194996


@pytest.mark.parametrize(
    "message",
    [
        "Invalid API Key (#err2)",
        "Too many invalid api key attempts, please try again later",
        "Max rate limit reached",
    ],
)
def test_temporary_error_is_retried(monkeypatch, no_waiting, message):
    fake_results(monkeypatch, [message, [RAW_SWAP]])

    assert collector.call_api({}) == [RAW_SWAP]
    assert 5 in no_waiting


def test_network_error_is_retried(monkeypatch, no_waiting):
    fake_results(monkeypatch, [requests.ConnectionError("offline"), []])

    assert collector.call_api({}) == []


def test_permanent_error_stops_immediately(monkeypatch, no_waiting):
    fake_results(monkeypatch, ["Query Timeout occured"])

    with pytest.raises(RuntimeError, match="Query Timeout"):
        collector.call_api({})


def test_gives_up_after_all_retries(monkeypatch, no_waiting):
    fake_results(monkeypatch, ["Invalid API Key (#err2)"] * (len(collector.RETRY_DELAYS) + 1))

    with pytest.raises(RuntimeError, match="kept failing"):
        collector.call_api({})


def raw_log(block, index):
    return {**RAW_SWAP, "blockNumber": hex(block), "logIndex": hex(index)}


def fake_chain(monkeypatch, logs):
    """Serve (block, index) logs in pages, like Etherscan. Returns the calls made."""
    calls = []

    def fake_fetch(from_block, to_block):
        calls.append(from_block)
        found = [log for log in logs if from_block <= log[0] <= to_block]
        return [raw_log(*log) for log in found[: collector.PAGE_SIZE]]

    monkeypatch.setattr(collector, "fetch_swap_logs", fake_fetch)
    return calls


def test_block_at_date(monkeypatch, no_waiting):
    responses = iter([{"status": "1", "result": "14720000"}])

    class Response(FakeResponse):
        def json(self):
            return next(responses)

    monkeypatch.setattr(collector.requests, "get", lambda *a, **k: Response(None))
    assert collector.block_at_date("2022-05-05") == 14720000


def test_collects_all_pages_without_losing_a_split_block(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "PAGE_SIZE", 3)
    monkeypatch.setattr(collector, "START_BLOCK", 100)
    # Block 102 is split between the first and second pages.
    logs = [(100, 0), (101, 0), (102, 0), (102, 1), (103, 0)]
    calls = fake_chain(monkeypatch, logs)
    conn = connect(tmp_path / "test.sqlite")

    collector.collect_swaps(conn, 110)

    assert conn.execute("SELECT COUNT(*) FROM swaps").fetchone()[0] == 5
    assert calls == [100, 102, 103]
    assert get_checkpoint(conn, "Swap") == 110


def test_resumes_from_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "START_BLOCK", 100)
    calls = fake_chain(monkeypatch, [(100, 0), (105, 0)])
    conn = connect(tmp_path / "test.sqlite")

    collector.collect_swaps(conn, 102)
    collector.collect_swaps(conn, 110)

    assert calls == [100, 103]
    assert conn.execute("SELECT COUNT(*) FROM swaps").fetchone()[0] == 2


def test_block_larger_than_a_page_stops(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "PAGE_SIZE", 2)
    monkeypatch.setattr(collector, "START_BLOCK", 100)
    fake_chain(monkeypatch, [(100, 0), (100, 1), (100, 2)])
    conn = connect(tmp_path / "test.sqlite")

    with pytest.raises(RuntimeError, match="more events than one page"):
        collector.collect_swaps(conn, 110)


def raw_block(number, prices, base_fee=None):
    block = {
        "number": hex(number),
        "timestamp": hex(1620252901),
        "transactions": [{"gasPrice": hex(price)} for price in prices],
    }
    if base_fee is not None:
        block["baseFeePerGas"] = hex(base_fee)
    return block


def test_decode_gas_after_london():
    row = collector.decode_gas(raw_block(13000000, [30, 10, 20], base_fee=9))

    assert row["block_number"] == 13000000
    assert row["base_fee_wei"] == 9
    assert row["median_price_wei"] == 20


def test_decode_gas_before_london_and_empty_block():
    assert collector.decode_gas(raw_block(12400000, [5]))["base_fee_wei"] is None
    assert collector.decode_gas(raw_block(12400000, []))["median_price_wei"] is None


def test_call_api_returns_a_block(monkeypatch, no_waiting):
    block = raw_block(1, [1])
    fake_results(monkeypatch, [block])

    assert collector.call_api({}) == block


def test_collect_gas_resumes_on_the_grid(monkeypatch, tmp_path):
    monkeypatch.setattr(collector, "START_BLOCK", 100)
    monkeypatch.setattr(collector, "GAS_GRID_BLOCKS", 10)
    calls = []

    def fake_block(number):
        calls.append(number)
        return raw_block(number, [1])

    monkeypatch.setattr(collector, "fetch_block", fake_block)
    conn = connect(tmp_path / "test.sqlite")

    collector.collect_gas(conn, 125)
    collector.collect_gas(conn, 145)

    assert calls == [100, 110, 120, 130, 140]
    assert conn.execute("SELECT COUNT(*) FROM gas_prices").fetchone()[0] == 5
