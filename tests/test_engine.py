from itertools import count
from math import floor, sqrt

import pytest

from lpbacktest.engine import (
    GAS_PER_REBALANCE,
    GAS_BURN,
    GAS_MINT,
    amounts_to_usd,
    gas_cost_usd,
    is_in_range,
    liquidity_for_usd,
    position_amounts,
    price_impact_share,
    range_ticks,
    rebalance_cost,
    simulate_range,
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


def test_range_ticks_are_plus_minus_width_in_eth_price():
    tick = 194996
    lower, upper = range_ticks(tick, 0.10)

    assert lower % 10 == 0 and upper % 10 == 0
    # The lower tick is the higher ETH price.
    assert tick_to_usdc_per_eth(lower) == pytest.approx(tick_to_usdc_per_eth(tick) * 1.1, rel=1e-3)
    assert tick_to_usdc_per_eth(upper) == pytest.approx(tick_to_usdc_per_eth(tick) * 0.9, rel=1e-3)


def test_wide_range_is_symmetric_in_eth_price():
    tick = 194996
    lower, upper = range_ticks(tick, 0.5)

    assert tick_to_usdc_per_eth(lower) == pytest.approx(tick_to_usdc_per_eth(tick) * 1.5, rel=1e-3)
    assert tick_to_usdc_per_eth(upper) == pytest.approx(tick_to_usdc_per_eth(tick) * 0.5, rel=1e-3)


BLOCKS = count(1)


def swap_at(tick, amount0=0, amount1=0, active_liquidity=10**18, block=None):
    block = next(BLOCKS) if block is None else block
    sqrt_price_x96 = int(1.0001 ** (tick / 2) * 2**96)
    return (amount0, amount1, sqrt_price_x96, active_liquidity, tick, block)


def test_flat_price_has_no_impermanent_loss():
    swaps = [swap_at(195000)] + [swap_at(195000, amount0=10**9) for _ in range(3)]

    result = simulate_range(swaps, 100_000, 0.10)

    assert result["swaps"] == 3
    assert result["swaps_in_range"] == 3
    assert result["position_vs_hodl_usd"] == pytest.approx(0, abs=1e-6)
    assert result["net_vs_hodl_usd"] == pytest.approx(result["fees_usd"])
    assert result["fees_usd"] > 0


def test_price_exit_stops_fees_and_creates_a_loss():
    swaps = [swap_at(195000), swap_at(195000, amount0=10**9), swap_at(200000, amount0=10**9)]

    result = simulate_range(swaps, 100_000, 0.10)
    only_first = simulate_range(swaps[:2], 100_000, 0.10)

    assert result["swaps"] == 2
    assert result["swaps_in_range"] == 1
    assert result["position_vs_hodl_usd"] < 0
    # Fee tokens are the same, whatever the final price.
    assert result["fees_usd"] == pytest.approx(only_first["fees_usd"])


def test_wide_position_matches_the_v2_impermanent_loss_formula():
    # A very wide range behaves like v2: IL = 2 * sqrt(r) / (1 + r) - 1.
    r = 1.21  # raw price ratio
    sqrt_start, sqrt_end = 10_000.0, 10_000.0 * sqrt(r)
    lower, upper = sqrt_start / 10**6, sqrt_start * 10**6

    liquidity = 10**18
    start0, start1 = position_amounts(liquidity, sqrt_start, lower, upper)
    end0, end1 = position_amounts(liquidity, sqrt_end, lower, upper)
    price = sqrt_end**2  # value both tokens in token1

    position = end0 * price + end1
    hodl = start0 * price + start1

    assert position / hodl - 1 == pytest.approx(2 * sqrt(r) / (1 + r) - 1, abs=1e-5)


def test_protocol_fee_applies_only_after_its_block():
    swaps = [swap_at(195000, block=1)]
    swaps += [swap_at(195000, amount0=10**9, block=block) for block in (5, 10, 15)]

    before = simulate_range(swaps, 100_000, 0.10)
    after = simulate_range(swaps, 100_000, 0.10, fee_changes=[(10, 0.25)])

    # Two of the three swaps pay 75% of the fee.
    assert after["fees_usd"] == pytest.approx(before["fees_usd"] * (1 + 0.75 * 2) / 3)
    assert after["final_protocol_share"] == 0.25


def test_gas_cost_uses_the_last_known_price():
    blocks, prices = [100, 400], [10 * 10**9, 50 * 10**9]

    # 10 gwei at $2,000.
    expected = GAS_PER_REBALANCE * 10e9 / 1e18 * 2000
    assert gas_cost_usd(399, 2000, blocks, prices) == pytest.approx(expected)
    # Two samples known at block 400: their median is 30 gwei.
    assert gas_cost_usd(400, 2000, blocks, prices) == pytest.approx(expected * 3)


def test_gas_cost_ignores_a_one_sample_spike():
    blocks, prices = [100, 400, 700], [10 * 10**9, 1_000 * 10**9, 12 * 10**9]

    expected = GAS_PER_REBALANCE * 12e9 / 1e18 * 2000
    assert gas_cost_usd(700, 2000, blocks, prices) == pytest.approx(expected)
    # A spike that lasts long enough is paid.
    assert gas_cost_usd(700, 2000, blocks, [10 * 10**9, 1_000 * 10**9, 900 * 10**9]) == pytest.approx(expected * 75)


def test_price_impact_matches_constant_liquidity_math():
    # amount1 = L * d(sqrtP): 1 token1 raw at sqrt_price 1 and L = 1,000 moves 0.1%.
    assert price_impact_share(1e-18 * 2000, 2000, 1.0, 1000) == pytest.approx(0.001)


def test_rebalance_cost_charges_only_the_swapped_value():
    old = (0, 50 * 10**18)  # 50 WETH at $2,000
    new = (50_000 * 10**6, 25 * 10**18)  # same value, half in USDC

    swap_fee, impact = rebalance_cost(old, new, 2000, 1.0, 10**24)

    assert swap_fee == pytest.approx(25)
    assert impact == pytest.approx(50_000 * 25e18 / 1e24)


def test_exit_without_rebalance_has_no_costs():
    swaps = [swap_at(195000), swap_at(200000, amount0=10**9)]

    result = simulate_range(swaps, 100_000, 0.10)

    assert result["rebalances"] == 0
    assert result["gas_usd"] == 0


def test_rebalance_moves_the_range_and_pays_costs():
    gas = ([1], [10 * 10**9])
    swaps = [
        swap_at(195000, block=1),
        swap_at(200000, amount0=10**9, active_liquidity=10**21, block=2),
        swap_at(200000, amount0=10**9, active_liquidity=10**21, block=400),
    ]

    moved = simulate_range(swaps, 100_000, 0.10, gas=gas, rebalance=True)

    assert moved["rebalances"] == 1
    assert moved["tick_lower"] < 200000 < moved["tick_upper"]
    assert moved["swaps_in_range"] == 1
    total_costs = moved["gas_usd"] + moved["swap_fee_usd"] + moved["price_impact_usd"]
    assert total_costs > 0
    # Fixed pays only mint and burn gas here, so compare with those included.
    fixed = simulate_range(swaps, 100_000, 0.10, gas=gas)
    extra_costs = total_costs - fixed["gas_usd"]
    # With no price move after the rebalance, value falls by exactly the extra costs.
    assert moved["position_usd"] == pytest.approx(fixed["position_usd"] - extra_costs, rel=1e-4)


def test_sandwich_inside_one_block_does_not_trigger_a_rebalance():
    gas = ([1], [10 * 10**9])
    swaps = [
        swap_at(195000, block=1),
        swap_at(200000, block=500),  # front leg
        swap_at(195000, block=500),  # back leg, same block
        swap_at(195000, block=1000),
    ]

    result = simulate_range(swaps, 100_000, 0.10, gas=gas, rebalance=True)

    assert result["rebalances"] == 0


def test_position_is_checked_about_once_an_hour():
    gas = ([1], [10 * 10**9])
    swaps = [swap_at(195000, block=1), swap_at(200000, block=2), swap_at(200000, block=100)]

    result = simulate_range(swaps, 100_000, 0.10, gas=gas, rebalance=True)

    assert result["rebalances"] == 0


def test_price_impact_ignores_a_liquidity_gap():
    gas = ([1], [10 * 10**9])
    deep = [swap_at(195000, active_liquidity=10**21, block=block) for block in range(1, 21)]
    late = swap_at(200000, active_liquidity=10**21, block=500)

    def run(exit_liquidity):
        exit_swap = swap_at(200000, active_liquidity=exit_liquidity, block=21)
        return simulate_range(deep + [exit_swap, late], 100_000, 0.10, gas=gas, rebalance=True)

    assert run(10**16)["price_impact_usd"] == pytest.approx(run(10**21)["price_impact_usd"])


def test_lp_stops_when_a_rebalance_costs_more_than_the_position():
    gas = ([1], [3 * 10**11])  # 300 gwei, on a $1,000 position at about $3,390 per ETH
    swaps = [
        swap_at(195000, block=1),
        swap_at(200000, block=2),
        swap_at(200000, block=400),
        swap_at(200000, block=800),
    ]

    result = simulate_range(swaps, 1_000, 0.10, gas=gas, rebalance=True)

    # After the mint (about $430), about $570 is left, less than a rebalance (about $870).
    assert result["stopped"] is True
    assert result["rebalances"] == 0
    assert result["gas_usd"] <= 1_000  # never more than the position could pay


def test_costs_never_exceed_the_position():
    gas = ([1], [10**18])  # absurd gas: even the mint costs more than the position

    result = simulate_range([swap_at(195000, block=1), swap_at(195000, block=2)], 1_000, 0.10, gas=gas)

    assert result["gas_usd"] == pytest.approx(1_000)
    assert result["position_usd"] == pytest.approx(0, abs=1e-9)


def test_rebalance_needs_gas_prices():
    with pytest.raises(ValueError, match="gas prices"):
        simulate_range([swap_at(195000)], 100_000, 0.10, rebalance=True)


def test_entry_uses_the_price_at_the_end_of_the_first_block():
    swaps = [
        swap_at(200000, block=1),  # front leg of a sandwich
        swap_at(195000, block=1),  # back leg
        swap_at(195000, amount0=10**9, block=2),
    ]

    result = simulate_range(swaps, 100_000, 0.10)

    assert result["entry_price"] == pytest.approx(simulate_range(swaps[1:], 100_000, 0.10)["entry_price"])
    assert result["swaps"] == 1


def test_position_pays_entry_and_exit_gas_and_hodl_does_not():
    gas = ([1], [10 * 10**9])
    swaps = [swap_at(195000, block=1), swap_at(195000, block=2)]

    free = simulate_range(swaps, 100_000, 0.10)
    paid = simulate_range(swaps, 100_000, 0.10, gas=gas)

    price = paid["entry_price"]
    expected = (GAS_MINT + GAS_BURN) * 10e9 / 1e18 * price
    assert paid["gas_usd"] == pytest.approx(expected)
    assert paid["position_usd"] == pytest.approx(free["position_usd"] - expected)
    assert paid["hodl_usd"] == pytest.approx(free["hodl_usd"])


def test_fees_from_thin_liquidity_are_reported_apart():
    swaps = [
        swap_at(195000),
        swap_at(195000, amount0=10**9, active_liquidity=10**24),
        swap_at(195000, amount0=10**9, active_liquidity=1),
    ]

    result = simulate_range(swaps, 100_000, 0.10)

    assert 0 < result["thin_liquidity_fees_usd"] <= result["fees_usd"]


def test_no_swaps_is_an_error():
    with pytest.raises(ValueError):
        simulate_range([], 100_000, 0.10)
