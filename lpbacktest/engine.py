"""Replay engine for a Uniswap v3 LP position."""

USDC_DECIMALS = 6
WETH_DECIMALS = 18
DECIMALS_FACTOR = 10 ** (WETH_DECIMALS - USDC_DECIMALS)


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
