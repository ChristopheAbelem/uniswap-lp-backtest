"""Replay engine for a Uniswap v3 LP position."""

USDC_DECIMALS = 6
WETH_DECIMALS = 18
DECIMALS_FACTOR = 10 ** (WETH_DECIMALS - USDC_DECIMALS)
POOL_FEE = 0.0005  # 0.05% fee tier, read on-chain


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
