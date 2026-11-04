from math import floor, sqrt

import pytest

from lpbacktest.engine import (
    amounts_to_usd,
    is_in_range,
    liquidity_for_usd,
    position_amounts,
    sqrt_price_to_usdc_per_eth,
    swap_fees,
    tick_to_sqrt_price,
    tick_to_usdc_per_eth,
)

# Real swaps from the pool: (sqrtPriceX96, tick).
FIRST_SWAP = (1358206768703179146794161129278934, 194996)  # block 12376891
LATER_SWAP = (1351009436323195212087408184467187, 194890)  # block 12384488


def test_first_swap_price():
    price = sqrt_price_to_usdc_per_eth(FIRST_SWAP[0])

    assert price == pytest.approx(3402.73, abs=0.01)


def test_lower_tick_means_higher_eth_price():
    assert tick_to_usdc_per_eth(194890) > tick_to_usdc_per_eth(194996)


@pytest.mark.parametrize("sqrt_price_x96, tick", [FIRST_SWAP, LATER_SWAP])
def test_tick_and_sqrt_price_agree(sqrt_price_x96, tick):
    price_from_sqrt = sqrt_price_to_usdc_per_eth(sqrt_price_x96)
    price_from_tick = tick_to_usdc_per_eth(tick)

    # A tick is rounded down, so the two differ by at most one tick (0.01%).
    assert price_from_tick == pytest.approx(price_from_sqrt, rel=1.1e-4)


def test_tick_to_sqrt_price_matches_the_pool():
    sqrt_price_x96, tick = FIRST_SWAP

    assert tick_to_sqrt_price(tick) == pytest.approx(sqrt_price_x96 / 2**96, rel=1e-4)


# Official test vectors: Uniswap v3-periphery, test/LiquidityAmounts.spec.ts.
LOWER = sqrt(100 / 110)
UPPER = sqrt(110 / 100)


@pytest.mark.parametrize(
    "sqrt_price, liquidity, expected",
    [
        (1.0, 2148, (99, 99)),  # price inside the range
        (sqrt(99 / 110), 1048, (99, 0)),  # price below the range
        (sqrt(111 / 100), 2097, (0, 199)),  # price above the range
        (LOWER, 1048, (99, 0)),  # price on the lower boundary
        (UPPER, 2097, (0, 199)),  # price on the upper boundary
    ],
)
def test_position_amounts_match_uniswap(sqrt_price, liquidity, expected):
    amount0, amount1 = position_amounts(liquidity, sqrt_price, LOWER, UPPER)

    assert (floor(amount0), floor(amount1)) == expected


def test_amounts_to_usd():
    amount0 = 1_000 * 10**6  # 1,000 USDC
    amount1 = 2 * 10**18  # 2 WETH

    assert amounts_to_usd(amount0, amount1, 3000) == pytest.approx(7000)


@pytest.mark.parametrize("price_move", [0.8, 1.0, 1.25])  # below, inside, above
def test_liquidity_for_usd_gives_the_requested_value(price_move):
    entry_sqrt = FIRST_SWAP[0] / 2**96
    sqrt_price = entry_sqrt * sqrt(price_move)
    lower, upper = entry_sqrt * sqrt(0.9), entry_sqrt * sqrt(1.1)

    liquidity = liquidity_for_usd(100_000, sqrt_price, lower, upper)
    amount0, amount1 = position_amounts(liquidity, sqrt_price, lower, upper)
    usdc_per_eth = 10**12 / sqrt_price**2

    assert amounts_to_usd(amount0, amount1, usdc_per_eth) == pytest.approx(100_000)


@pytest.mark.parametrize(
    "tick, expected",
    [(99, False), (100, True), (150, True), (199, True), (200, False)],
)
def test_is_in_range(tick, expected):
    assert is_in_range(tick, 100, 200) is expected


def test_swap_fees_charge_the_token_that_entered_the_pool():
    usdc_in = swap_fees(1_000 * 10**6, -3 * 10**17, 10, 90)
    weth_in = swap_fees(-1_000 * 10**6, 3 * 10**17, 10, 90)

    # 0.05% of the input, and the position has 10% of the liquidity.
    assert usdc_in == pytest.approx((50_000, 0))
    assert weth_in == pytest.approx((0, 1.5 * 10**13))


def test_swap_fees_after_the_protocol_fee():
    fee0, fee1 = swap_fees(1_000 * 10**6, -3 * 10**17, 10, 90, protocol_share=0.25)

    # 0.0375% of the input goes to LPs, as in the Uniswap fee table.
    assert fee0 == pytest.approx(37_500)
    assert fee1 == 0
