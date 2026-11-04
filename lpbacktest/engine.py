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
