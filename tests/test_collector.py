import pytest
import requests

from lpbacktest import collector


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
    fake_results(monkeypatch, ["Invalid API Key (#err2)"] * 5)

    with pytest.raises(RuntimeError, match="kept failing"):
        collector.call_api({})
