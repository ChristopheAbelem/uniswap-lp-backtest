# Uniswap v3 LP backtest

Which range strategy would have earned the most as a liquidity provider (LP) in the Uniswap v3 USDC/WETH 0.05% pool on Ethereum, compared with simply holding the same tokens (HODL), after gas and trading costs?

**Result:** none beat holding. Parameters were chosen on July 2021 to December 2023 and tested on January 2024 to October 2026. Out of sample, the best rule, a full range, averaged -0.55% vs HODL per 90-day window and was statistically indistinguishable from holding. Narrow ranges that rebalance when the price leaves them lost the most, and the rule that led in training (a volatility-scaled range) was the only one clearly below holding out of sample (-3.22%, t = -2.96).

**Research note (5 pages):** [report/lp_strategy_note.pdf](report/lp_strategy_note.pdf)

## How it works

The engine replays every swap in the pool (11.97 million, May 2021 to October 2026) and inserts a hypothetical position into each one, in chain order.

- **Fees:** on each swap that ends inside its range, the position earns input × 0.05% × (1 − protocol share) × its share of active liquidity. The protocol fee switch of 27 December 2025 is included.
- **Costs:** each rebalance pays gas (real hourly gas prices × real gas used), a 0.05% swap fee and price impact estimated from pool depth.
- **Strategies:** full range; fixed range ±x%; range ±x% rebalanced when the price leaves it; range scaled by recent volatility.
- **Evaluation:** 90-day windows opened monthly. Parameters are chosen in training (July 2021 to December 2023) and tested on January 2024 to October 2026, with Newey-West t-statistics. Results are also split by regime and by position size ($10k, $100k, $1M).
- **Validation:** simulated fees for 30 real on-chain positions have a median error of 0.39%. Separately, an independent re-implementation matches the engine to six decimals.

## Project layout

```
lpbacktest/
  collector.py    Etherscan API V2 collection: swaps, gas prices, protocol fee, gas used
  storage.py      SQLite storage (large integers kept as text)
  engine.py       position maths, fees, costs, replay
  strategies.py   range rules
  metrics.py      returns, drawdown, benchmarks
  experiments.py  rolling windows, calibration, comparison, Newey-West
  validation.py   fees vs real positions
  __main__.py     command line
tests/            112 tests (pytest)
results/          CSV output used by the report
report/           research note and the script that builds it
```

## Running it

Requires Python 3.11+.

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
```

**Collect the data.** This needs a free Etherscan API key in `.env` (`ETHERSCAN_API_KEY=...`). The free tier allows 3 calls per second, so expect about 13 hours for swaps and about a day for gas prices. Collection resumes from checkpoints if interrupted. The database (`data/pool_events.sqlite`, about 3.5 GB) is not in the repository.

```
python -m lpbacktest.collector --to 2026-11-01
python -m lpbacktest.collector --gas --to 2026-11-01
python -m lpbacktest.collector --fee-protocol
python -m lpbacktest.collector --gas-used
```

**Run the experiments:**

```
python -m lpbacktest run --start 2024-01-01 --end 2024-04-01 --strategy rebalance --width 0.1 --usd 100000
python -m lpbacktest calibrate      # training and test windows
python -m lpbacktest compare        # by regime and position size
python -m lpbacktest validate       # fees vs real positions
```

**Rebuild the report.** This uses the CSVs in `results/` and does not need the database.

```
pip install matplotlib playwright && playwright install chromium
python report/build_report.py
```

## Limitations

- **Fee share:** it uses the liquidity after each swap, so a swap that crosses a liquidity gap credits the position with slightly too much. This favours the LP.
- **Rebalancing is optimistic:** it executes at the end-of-block price, checked about once an hour, with no delay and no MEV.
- **Small sample:** overlapping windows give roughly 11 independent observations out of sample.
- **Fees are not reinvested.**
- **One pool only:** other fee tiers and layer 2 networks are untested.

## Next steps

- Hedge the position's ETH with perpetual futures and compare it with holding USDC, net of funding.
- Test other rule families: asymmetric ranges, a wide base plus a narrow range, and scheduled rebalancing.

## Sources

- Pool data: Ethereum mainnet via Etherscan API V2.
- Loesch et al. (2021), "Impermanent Loss in Uniswap v3", arXiv:2111.09192.
- Milionis et al. (2022), "Automated Market Making and Loss-Versus-Rebalancing", arXiv:2208.06046.

Academic portfolio project. Not investment advice.
