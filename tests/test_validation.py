import pytest

from lpbacktest import validation
from lpbacktest.engine import replay_real_position


def word(value):
    return f"{value % 2**256:064x}"


def mint(block, lower=100, upper=200, liquidity=5, tx="0xa"):
    return {"block": block, "tx_hash": tx, "tick_lower": lower, "tick_upper": upper, "liquidity": liquidity}


def test_decode_mint_reads_negative_ticks_and_liquidity():
    log = {
        "blockNumber": hex(10),
        "transactionHash": "0xa",
        "topics": ["0x0", "0x0", "0x" + word(-887270), "0x" + word(887270)],
        "data": "0x" + word(1) + word(777) + word(2) + word(3),
    }

    event = validation.decode_liquidity_event(log, True)

    assert (event["tick_lower"], event["tick_upper"]) == (-887270, 887270)
    assert event["liquidity"] == 777


def test_decode_burn_reads_liquidity_from_the_first_word():
    log = {
        "blockNumber": hex(10),
        "transactionHash": "0xa",
        "topics": ["0x0", "0x0", "0x" + word(100), "0x" + word(200)],
        "data": "0x" + word(777) + word(2) + word(3),
    }

    assert validation.decode_liquidity_event(log, False)["liquidity"] == 777


def test_match_keeps_a_clean_position():
    positions = validation.match_positions([mint(1000)], [mint(9000)], 2000)

    assert len(positions) == 1
    assert positions[0]["mint_block"] == 1000
    assert positions[0]["burn_block"] == 9000


@pytest.mark.parametrize(
    "mints, burns",
    [
        ([mint(1000), mint(1500)], [mint(9000)]),  # two mints at the same ticks
        ([mint(1000)], [mint(9000, liquidity=4)]),  # partial burn
        ([mint(1000)], [mint(1100)]),  # short life, as in JIT liquidity
        ([mint(5000)], [mint(9000)]),  # opened after the mint window
    ],
)
def test_match_rejects_ambiguous_positions(mints, burns):
    assert validation.match_positions(mints, burns, 2000) == []


def test_real_fees_remove_principal(monkeypatch):
    position = {"mint_block": 1, "burn_block": 2}
    increase = {"data": "0x" + word(5) + word(1) + word(2)}
    decrease = {"data": "0x" + word(5) + word(100) + word(200)}
    collect = {"data": "0x" + word(0) + word(130) + word(260)}
    logs = {
        validation.INCREASE_TOPIC: [increase],
        validation.DECREASE_TOPIC: [decrease],
        validation.COLLECT_TOPIC: [collect],
    }
    monkeypatch.setattr(validation, "fetch_all_logs", lambda topic, *args: logs[topic])

    assert validation.real_fees(position, "0x1") == (30, 60)


def test_real_fees_skip_a_position_that_grew(monkeypatch):
    position = {"mint_block": 1, "burn_block": 2}
    monkeypatch.setattr(validation, "fetch_all_logs", lambda topic, *args: [{}, {}])

    assert validation.real_fees(position, "0x1") is None


def test_replay_counts_a_real_position_inside_the_liquidity():
    swaps = [(10**9, -1, 0, 100, 150, 1), (10**9, -1, 0, 100, 500, 2)]

    fee0, fee1 = replay_real_position(swaps, 25, 100, 200)

    # Only the first swap is in range, and the position is 25% of its liquidity.
    assert fee0 == pytest.approx(10**9 * 0.0005 * 0.25)
    assert fee1 == 0
