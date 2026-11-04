from math import floor, sqrt

import pytest

from lpbacktest.engine import (
    position_amounts,
    sqrt_price_to_usdc_per_eth,
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
