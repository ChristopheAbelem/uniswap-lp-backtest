import pytest

from lpbacktest.engine import sqrt_price_to_usdc_per_eth, tick_to_usdc_per_eth

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
