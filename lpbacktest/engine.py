"""Replay engine for a Uniswap v3 LP position."""

import bisect
import itertools
import math
import statistics
from collections import deque

USDC_DECIMALS = 6
WETH_DECIMALS = 18
DECIMALS_FACTOR = 10 ** (WETH_DECIMALS - USDC_DECIMALS)
POOL_FEE = 0.0005  # 0.05% fee tier, read on-chain
TICK_SPACING = 10  # tick spacing of the 0.05% tier
# Swaps used for the typical depth of the pool. A swap at the edge of a liquidity gap
# shows a tiny L, while a real rebalance would be routed around that gap.
LIQUIDITY_WINDOW = 500
REBALANCE_CHECK_BLOCKS = 300  # the keeper looks at the position about once an hour
THIN_LIQUIDITY_SHARE = 0.05  # above this share of a swap, the fee is reported apart
# Median gasUsed of 430 real transactions in the tx_gas table.
GAS_MINT = 418_892
GAS_BURN = 243_676
GAS_SWAP = 192_352
GAS_PER_REBALANCE = GAS_BURN + GAS_SWAP + GAS_MINT
Q96 = 2**96


def sqrt_price_to_usdc_per_eth(sqrt_price_x96):
    """Convert sqrtPriceX96 to the ETH price in USDC."""
    return DECIMALS_FACTOR * 2**192 / sqrt_price_x96**2


def tick_to_usdc_per_eth(tick):
    """Convert a tick to the ETH price in USDC."""
    return DECIMALS_FACTOR / 1.0001**tick


def tick_to_sqrt_price(tick):
    """Convert a tick to the square root of the raw price."""
    return 1.0001 ** (tick / 2)


def position_amounts(liquidity, sqrt_price, sqrt_lower, sqrt_upper):
    """Return the raw (amount0, amount1) held by a position at a price."""
    # Keep the price inside the range, so outside it the position holds one token.
    sqrt_price = min(max(sqrt_price, sqrt_lower), sqrt_upper)

    amount0 = liquidity * (sqrt_upper - sqrt_price) / (sqrt_price * sqrt_upper)
    amount1 = liquidity * (sqrt_price - sqrt_lower)
    return amount0, amount1


def amounts_to_usd(amount0, amount1, usdc_per_eth):
    """Return the USD value of raw USDC (token0) and WETH (token1) amounts."""
    usdc = amount0 / 10**USDC_DECIMALS
    weth = amount1 / 10**WETH_DECIMALS
    return usdc + weth * usdc_per_eth


def liquidity_for_usd(usd, sqrt_price, sqrt_lower, sqrt_upper):
    """Return the liquidity of a position worth a given USD value."""
    usdc_per_eth = DECIMALS_FACTOR / sqrt_price**2

    # Value grows in proportion to liquidity, so one unit gives the scale.
    amount0, amount1 = position_amounts(1, sqrt_price, sqrt_lower, sqrt_upper)
    return usd / amounts_to_usd(amount0, amount1, usdc_per_eth)


def is_in_range(tick, tick_lower, tick_upper):
    """Return True if the position earns fees at this tick."""
    return tick_lower <= tick < tick_upper


def swap_fees(amount0, amount1, position_liquidity, active_liquidity, protocol_share=0.0):
    """Return the raw (fee0, fee1) a position earns from one swap.

    Swap amounts are signed from the pool's side: the positive one entered the
    pool and already includes the fee. The position is hypothetical, so it is
    not part of active_liquidity and has to be added to the denominator.
    """
    lp_fee = POOL_FEE * (1 - protocol_share)
    share = position_liquidity / (active_liquidity + position_liquidity)

    fee0 = max(amount0, 0) * lp_fee * share
    fee1 = max(amount1, 0) * lp_fee * share
    return fee0, fee1


def range_ticks(tick, width):
    """Return usable ticks for a range of +/- width around a tick."""
    base = math.log(1.0001)
    lower = tick + math.log(1 - width) / base
    upper = tick + math.log(1 + width) / base

    tick_lower = math.floor(lower / TICK_SPACING) * TICK_SPACING
    tick_upper = math.ceil(upper / TICK_SPACING) * TICK_SPACING
    return tick_lower, tick_upper


def gas_cost_usd(block_number, usdc_per_eth, gas_blocks, gas_prices, gas_used=GAS_PER_REBALANCE):
    """Return the USD cost of an operation at a block."""
    index = max(bisect.bisect_right(gas_blocks, block_number) - 1, 0)
    return gas_used * gas_prices[index] / 10**18 * usdc_per_eth


def price_impact_share(swap_usd, usdc_per_eth, sqrt_price, active_liquidity):
    """Return the average price impact of a swap inside the active liquidity.

    From amount1 = L * d(sqrtP), the average execution price moves by d(sqrtP) / sqrtP.
    """
    amount1 = swap_usd / usdc_per_eth * 10**WETH_DECIMALS
    return amount1 / (active_liquidity * sqrt_price)


def rebalance_cost(old_amounts, new_amounts, usdc_per_eth, sqrt_price, active_liquidity):
    """Return (swap_fee_usd, price_impact_usd) of moving between two compositions."""
    old_usdc = amounts_to_usd(old_amounts[0], 0, usdc_per_eth)
    new_usdc = amounts_to_usd(new_amounts[0], 0, usdc_per_eth)
    swap_usd = abs(new_usdc - old_usdc)

    swap_fee = swap_usd * POOL_FEE
    impact = swap_usd * price_impact_share(swap_usd, usdc_per_eth, sqrt_price, active_liquidity)
    return swap_fee, impact


def replay_real_position(swaps, liquidity, tick_lower, tick_upper, fee_changes=()):
    """Return the raw (fee0, fee1) of a position that existed on-chain.

    The position is real, so it is already inside the event's liquidity.
    """
    fee0 = fee1 = 0.0
    protocol_share = 0.0
    pending_changes = list(fee_changes)

    for amount0, amount1, _, active_liquidity, tick, block_number in swaps:
        while pending_changes and block_number >= pending_changes[0][0]:
            protocol_share = pending_changes.pop(0)[1]
        if is_in_range(tick, tick_lower, tick_upper):
            others = max(int(active_liquidity) - liquidity, 0)
            earned0, earned1 = swap_fees(int(amount0), int(amount1), liquidity, others, protocol_share)
            fee0 += earned0
            fee1 += earned1
    return fee0, fee1


def simulate_range(swaps, usd, width, fee_changes=(), gas=None, rebalance=False):
    """Replay swaps for one position, valuing fees at the final price.

    Each swap is (amount0, amount1, sqrt_price_x96, liquidity, tick, block_number).
    The position opens at the price at the end of the first block, so a sandwich
    inside that block is ignored. The event's liquidity and tick are the values
    after the swap, so swaps that cross ticks are approximated. fee_changes is
    [(block_number, protocol_share)], oldest first. gas is (blocks, prices): with it,
    the position pays mint gas at the start and burn gas at the end. HODL starts
    with the same tokens and pays neither. With rebalance=True, the position is
    checked about once an hour at the end of a block. If it is out of range, it
    moves to a new range around that price. Fees stay outside the position and are
    not reinvested. Costs are paid from the position.
    """
    swaps = iter(swaps)
    try:
        first = next(swaps)
    except StopIteration:
        raise ValueError("No swaps in this period") from None
    if rebalance and gas is None:
        raise ValueError("Rebalancing needs gas prices")

    for swap in swaps:
        if swap[5] != first[5]:
            swaps = itertools.chain([swap], swaps)
            break
        first = swap

    sqrt_price = int(first[2]) / Q96
    entry_price = DECIMALS_FACTOR / sqrt_price**2
    tick_lower, tick_upper = range_ticks(first[4], width)
    sqrt_lower = tick_to_sqrt_price(tick_lower)
    sqrt_upper = tick_to_sqrt_price(tick_upper)

    hodl_liquidity = liquidity_for_usd(usd, sqrt_price, sqrt_lower, sqrt_upper)
    start0, start1 = position_amounts(hodl_liquidity, sqrt_price, sqrt_lower, sqrt_upper)

    gas_usd = gas_cost_usd(first[5], entry_price, *gas, GAS_MINT) if gas else 0.0
    invested = max(usd - gas_usd, 0.0)
    liquidity = liquidity_for_usd(invested, sqrt_price, sqrt_lower, sqrt_upper)

    fee0 = fee1 = thin_fee0 = thin_fee1 = 0.0
    total_swaps = swaps_in_range = rebalances = 0
    swap_fee_usd = impact_usd = 0.0
    protocol_share = 0.0
    pending_changes = list(fee_changes)
    recent_liquidity = deque(maxlen=LIQUIDITY_WINDOW)
    previous_block = first[5]
    previous_tick = first[4]
    next_check = first[5] + REBALANCE_CHECK_BLOCKS

    for amount0, amount1, sqrt_price_x96, active_liquidity, tick, block_number in swaps:
        total_swaps += 1

        if (
            rebalance
            and block_number != previous_block
            and block_number >= next_check
        ):
            next_check = block_number + REBALANCE_CHECK_BLOCKS
            if not is_in_range(previous_tick, tick_lower, tick_upper):
                price = DECIMALS_FACTOR / sqrt_price**2
                old = position_amounts(liquidity, sqrt_price, sqrt_lower, sqrt_upper)
                value = amounts_to_usd(old[0], old[1], price)

                tick_lower, tick_upper = range_ticks(previous_tick, width)
                sqrt_lower = tick_to_sqrt_price(tick_lower)
                sqrt_upper = tick_to_sqrt_price(tick_upper)
                target_liquidity = liquidity_for_usd(value, sqrt_price, sqrt_lower, sqrt_upper)
                target = position_amounts(target_liquidity, sqrt_price, sqrt_lower, sqrt_upper)

                typical_liquidity = statistics.median(recent_liquidity)
                swap_fee, impact = rebalance_cost(old, target, price, sqrt_price, typical_liquidity)
                gas_cost = gas_cost_usd(previous_block, price, *gas)
                remaining = max(value - swap_fee - impact - gas_cost, 0.0)
                liquidity = liquidity_for_usd(remaining, sqrt_price, sqrt_lower, sqrt_upper)

                rebalances += 1
                gas_usd += gas_cost
                swap_fee_usd += swap_fee
                impact_usd += impact

        sqrt_price = int(sqrt_price_x96) / Q96
        recent_liquidity.append(int(active_liquidity))
        while pending_changes and block_number >= pending_changes[0][0]:
            protocol_share = pending_changes.pop(0)[1]

        if is_in_range(tick, tick_lower, tick_upper):
            swaps_in_range += 1
            earned0, earned1 = swap_fees(
                int(amount0), int(amount1), liquidity, int(active_liquidity), protocol_share
            )
            fee0 += earned0
            fee1 += earned1
            if liquidity / (int(active_liquidity) + liquidity) > THIN_LIQUIDITY_SHARE:
                thin_fee0 += earned0
                thin_fee1 += earned1

        previous_block = block_number
        previous_tick = tick

    end_price = DECIMALS_FACTOR / sqrt_price**2
    end0, end1 = position_amounts(liquidity, sqrt_price, sqrt_lower, sqrt_upper)
    position_usd = amounts_to_usd(end0, end1, end_price)
    fees_usd = amounts_to_usd(fee0, fee1, end_price)
    hodl_usd = amounts_to_usd(start0, start1, end_price)
    if gas:
        exit_gas = gas_cost_usd(previous_block, end_price, *gas, GAS_BURN)
        gas_usd += exit_gas
        position_usd = max(position_usd - exit_gas, 0.0)

    return {
        "entry_price": entry_price,
        "end_price": end_price,
        "tick_lower": tick_lower,
        "tick_upper": tick_upper,
        "swaps": total_swaps,
        "swaps_in_range": swaps_in_range,
        "final_protocol_share": protocol_share,
        "rebalances": rebalances,
        "thin_liquidity_fees_usd": amounts_to_usd(thin_fee0, thin_fee1, end_price),
        "gas_usd": gas_usd,
        "swap_fee_usd": swap_fee_usd,
        "price_impact_usd": impact_usd,
        "fees_usd": fees_usd,
        "position_usd": position_usd,
        "hodl_usd": hodl_usd,
        "impermanent_loss_usd": position_usd - hodl_usd,
        "net_vs_hodl_usd": position_usd + fees_usd - hodl_usd,
    }
