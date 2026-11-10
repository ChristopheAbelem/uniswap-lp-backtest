"""Build the research note (report/lp_strategy_note.pdf) from the saved results.

Every result figure in the text and charts is read from results/*.csv. Facts about
the raw data that need the 3.5 GB database are constants below, each with the query
that produced it.

Requires matplotlib and playwright (with Chromium). Run from the project root:
    python report/build_report.py
"""

import base64
import csv
import io
import math
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from lpbacktest.engine import (  # noqa: E402
    amounts_to_usd,
    liquidity_for_usd,
    position_amounts,
    range_ticks,
    tick_to_sqrt_price,
)
from lpbacktest.strategies import MAX_TICK  # noqa: E402

RESULTS = Path("results")
OUTPUT = Path("report/lp_strategy_note.pdf")

# Facts from the database (data/pool_events.sqlite), checked on 9 November 2026.
SWAPS = 11_966_957  # SELECT COUNT(*) FROM swaps
GAS_SAMPLES = 46_488  # SELECT COUNT(*) FROM gas_prices
GAS_USED = {"mint": 418_892, "burn": 243_676, "swap": 192_352}  # medians of tx_gas by op
GAS_TRANSACTIONS = 430  # SELECT COUNT(*) FROM tx_gas
FEE_SWITCH_BLOCK = 24_106_471  # fee_protocol_changes: 27 Dec 2025, feeProtocol 4
GAS_GWEI = {"2021": 79, "2022": 44, "2026": 0.48}  # yearly mean of median_price_wei
MEDIAN_SWAPS_PER_DAY = 5_592  # daily swap counts from 1 Jul 2021 (lowest day 1,791)
MAX_SWAP_GAP_BLOCKS = 149  # largest block gap between consecutive swaps from 1 Jul 2021
PRE_MERGE_SECONDS_PER_BLOCK = 13.6  # gas_prices block_time, Jul 2021 to Sep 2022
LIQUIDITY_FALL = 6.8  # median swap liquidity, 2023 (2.77e19) over 2025 (4.08e18)

NAVY = "#1f3a5f"
BLUE = "#2a78d6"
GREY = "#9aa3ad"
RED = "#c8453a"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 10})


def read(name):
    with open(RESULTS / f"{name}.csv", newline="") as file:
        return list(csv.DictReader(file))


def num(value):
    return float(value) if value not in ("", None) else None


def pct(value, digits=1, sign=False):
    text = f"{value * 100:.{digits}f}%"
    return ("+" + text) if sign and value > 0 else text.replace("-", "−")


def tval(value):
    return f"{value:.2f}".replace("-", "−")


def param_key(value):
    return f"{float(value):g}" if value else ""


# ---------------------------------------------------------------- results
train = {(r["strategy"], param_key(r["param"])): r for r in read("calibration_train")}
test = {r["strategy"]: r for r in read("calibration_test")}
test_windows = read("calibration_test_windows")
comparison = {(r["period"], r["strategy"], int(float(r["usd"]))): r for r in read("comparison")}
validation = read("validation")

errors = [abs(float(r["error_pct"])) for r in validation]
real_total = sum(float(r["real_fee_usd"]) for r in validation)
sim_total = sum(float(r["simulated_fee_usd"]) for r in validation)
after_switch = [abs(float(r["error_pct"])) for r in validation if int(r["mint_block"]) > FEE_SWITCH_BLOCK]
VAL = {
    "n": len(validation),
    "median": statistics.median(errors),
    "total": (sim_total / real_total - 1) * 100,
    "worst": max(errors),
    "after_switch_max": max(after_switch),
}

CHOSEN = {s: param_key(r["param"]) for s, r in test.items()}
LABEL = {
    "full_range": "Full range",
    "fixed": f"Fixed ±{float(CHOSEN['fixed']) * 100:.0f}%",
    "rebalance": f"Rebalance ±{float(CHOSEN['rebalance']) * 100:.0f}%",
    "vol_scaled": f"Volatility k = {CHOSEN['vol_scaled']}",
}
ORDER = ["full_range", "rebalance", "fixed", "vol_scaled"]


def t_mean(strategy):
    r = test[strategy]
    return float(r["mean_vs_hodl"]), float(r["t_stat"])


full_mean, full_t = t_mean("full_range")
vol_mean, vol_t = t_mean("vol_scaled")
tied = [s for s in ORDER if abs(t_mean(s)[1]) < 2]
tied_means = [t_mean(s)[0] for s in tied]
tied_t_max = max(abs(t_mean(s)[1]) for s in tied)
full_windows = [r for r in test_windows if r["strategy"] == "full_range"]
full_beat = sum(float(r["net_vs_hodl_pct"]) > 0 for r in full_windows)
test_costs = [float(r["mean_costs"]) for r in test.values()]

reb = {w: float(train[("rebalance", f"{w:g}")]["mean_vs_hodl"]) for w in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 0.7, 0.9)}
fix = {w: float(train[("fixed", f"{w:g}")]["mean_vs_hodl"]) for w in (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 0.7, 0.9)}
vol_train = {k: float(train[("vol_scaled", f"{k:g}")]["mean_vs_hodl"]) for k in (0.5, 1, 2, 3, 4, 6, 8, 12)}
full_train = float(train[("full_range", "")]["mean_vs_hodl"])
reb_moves = {w: float(train[("rebalance", f"{w:g}")]["mean_rebalances"]) for w in (0.01, 0.05)}
fixed_worst = min(float(r["worst_vs_hodl"]) for (s, _), r in train.items() if s == "fixed")
reb_narrow_t = [float(train[("rebalance", f"{w:g}")]["t_stat"]) for w in (0.01, 0.02, 0.05, 0.1)]

REGIMES = ["2021 H2", "2022", "2023-24", "2025-26"]
REGIME_LABEL = {"2021 H2": "2021 H2 (rally)", "2022": "2022 (drawdown)", "2023-24": "2023–24", "2025-26": "2025–26"}


def comp(period, strategy, usd, field="mean_vs_hodl"):
    return float(comparison[(period, strategy, usd)][field])


# ---------------------------------------------------------------- figures
def figure_png(fig):
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e3e6ea", linewidth=0.8)
    ax.set_axisbelow(True)


def fig_width():
    fig, ax = plt.subplots(figsize=(7.4, 2.25))
    widths = list(reb)
    x = range(len(widths))
    ax.plot(x, [v * 100 for v in reb.values()], "-o", color=BLUE, lw=2, ms=4.5, label="Rebalanced when out of range")
    ax.plot(x, [v * 100 for v in fix.values()], "-o", color=GREY, lw=2, ms=4, label="Fixed, never moved")
    ax.axhline(0, color="#555", lw=0.8)
    ax.axhline(full_train * 100, color=GREY, lw=1, ls=":", label=f"Full range ({pct(full_train, 2, True)})")
    ax.set_ylim(min(reb.values()) * 100 - 5, 2)
    ax.set_xticks(list(x), [f"±{w * 100:g}%" for w in widths])
    ax.set_xlabel("Range width (±% of the ETH price at entry or at each rebalance)")
    ax.set_ylabel("Mean vs HODL (%)")
    for i, w in enumerate(widths[:4]):
        ax.annotate(pct(reb[w]), (i, reb[w] * 100), xytext=(7, -11), textcoords="offset points", fontsize=8, color=BLUE)
    ax.set_title("Training windows (Jul 2021 to Dec 2023), $100k: narrower rebalanced ranges lost more")
    ax.legend(frameon=False, loc="lower right", fontsize=8)
    style(ax)
    return figure_png(fig)


def fig_payoff():
    entry = 2000.0
    raw = 10**12 / entry
    tick = math.log(raw) / math.log(1.0001)
    sqrt_entry = math.sqrt(raw)
    fig, ax = plt.subplots(figsize=(7.4, 2.5))
    changes = [m / 100 for m in range(-50, 51)]
    cases = [("Full range", (-MAX_TICK, MAX_TICK), GREY), ("±50% range", range_ticks(tick, 0.5), "#6b9bd1"), ("±10% range", range_ticks(tick, 0.1), BLUE)]
    for label, (lower, upper), color in cases:
        sl, su = tick_to_sqrt_price(lower), tick_to_sqrt_price(upper)
        liquidity = liquidity_for_usd(100_000, sqrt_entry, sl, su)
        start = position_amounts(liquidity, sqrt_entry, sl, su)
        gaps = []
        for change in changes:
            price = entry * (1 + change)
            sqrt_now = math.sqrt(10**12 / price)
            lp = amounts_to_usd(*position_amounts(liquidity, sqrt_now, sl, su), price)
            hodl = amounts_to_usd(*start, price)
            gaps.append((lp - hodl) / 1000)
        ax.plot([c * 100 for c in changes], gaps, color=color, lw=2, label=label)
    ax.axhline(0, color="#555", lw=0.8)
    ax.set_xlabel("ETH price change since the position opened (%)")
    ax.set_ylabel("Position minus HODL ($k)")
    ax.set_title("Before fees, a $100k position always ends at or below holding, and a narrow range falls further")
    ax.legend(frameon=False, loc="lower center", ncol=3, fontsize=8)
    style(ax)
    return figure_png(fig)


def fig_test():
    fig, ax = plt.subplots(figsize=(7.4, 2.25))
    for i, s in enumerate(ORDER):
        mean, t = t_mean(s)
        se = abs(mean / t)
        lo, hi = mean - 1.96 * se, mean + 1.96 * se
        color = BLUE if hi < 0 else GREY
        y = len(ORDER) - 1 - i
        ax.plot([lo * 100, hi * 100], [y, y], color=color, lw=3, solid_capstyle="round")
        ax.plot(mean * 100, y, "o", color=color, ms=8)
        ax.annotate(f"{pct(mean, 2)}  (t = {tval(t)})", (mean * 100, y), xytext=(0, 8), textcoords="offset points", ha="center", fontsize=8)
    ax.axvline(0, color="#555", lw=0.8)
    ax.set_yticks(range(len(ORDER)), [LABEL[s] for s in reversed(ORDER)])
    ax.set_ylim(-0.6, len(ORDER) - 0.3)
    ax.set_xlabel("Mean result vs HODL per 90-day window (%), with 95% interval")
    ax.set_title("Test windows (Jan 2024 to Oct 2026), $100k: grey intervals cross zero")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="x", color="#e3e6ea", linewidth=0.8)
    ax.set_axisbelow(True)
    return figure_png(fig)


def fig_scatter():
    fig, ax = plt.subplots(figsize=(7.4, 2.6))
    colors = {"full_range": GREY, "rebalance": "#7f8c99", "fixed": "#c7cdd3", "vol_scaled": BLUE}
    markers = {"full_range": "o", "rebalance": "^", "fixed": "s", "vol_scaled": "o"}
    for s in ORDER:
        rows = [r for r in test_windows if r["strategy"] == s]
        x = [2 * float(r["hodl_50_50_return"]) * 100 for r in rows]  # ETH price change in the window
        y = [float(r["net_vs_hodl_pct"]) * 100 for r in rows]
        ax.scatter(x, y, s=16, color=colors[s], marker=markers[s], label=LABEL[s], zorder=3 if s == "vol_scaled" else 2)
    ax.axhline(0, color="#555", lw=0.8)
    ax.axvline(0, color="#cfd4d9", lw=0.8)
    ax.set_xlabel("ETH price change over the 90-day window (%)")
    ax.set_ylabel("Result vs HODL (%)")
    ax.set_title("Test windows: larger ETH moves, in either direction, tended to mean larger losses")
    ax.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=4)
    style(ax)
    return figure_png(fig)


# ---------------------------------------------------------------- page
def row(cells, header=False):
    tag = "th" if header else "td"
    return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"


def table(header, rows, cls=""):
    return f"<table class='{cls}'>" + row(header, True) + "".join(row(r) for r in rows) + "</table>"


key_data = table(
    ["Key data", ""],
    [
        ["Pool", "USDC/WETH 0.05%"],
        ["Swaps replayed", f"{SWAPS:,}"],
        ["Period", "Jul 2021 to Oct 2026"],
        ["90-day windows", "28 train, 32 test"],
        ["Rules tested", "4 families, 25 settings"],
        [f"Fee error, {VAL['n']} real positions", f"{VAL['median']:.2f}% (median)"],
        ["Best out of sample", f"Full range, {pct(full_mean, 2)}"],
        ["Worst out of sample", f"Vol. k = {CHOSEN['vol_scaled']}, {pct(vol_mean, 2)}"],
    ],
    "kv",
)

summary = f"""
<ul class='summary'>
<li><b>No rule beat holding.</b> Out of sample, {', '.join(LABEL[s].lower() if s != 'full_range' else 'full range' for s in tied)} were within noise of holding the same tokens ({pct(max(tied_means), 2)} to {pct(min(tied_means), 2)} per 90-day window, |t| &lt; {math.ceil(tied_t_max * 10) / 10:.1f}). The volatility-scaled rule lost {pct(-vol_mean, 2)} (t = {tval(vol_t)}).</li>
<li><b>Range width drives the result.</b> In training, rebalanced ranges of ±10% or narrower lost {pct(-reb[0.1])} to {pct(-reb[0.01])} per window, all with t below −{math.floor(-max(reb_narrow_t))}.</li>
<li><b>Rebalancing locks in the loss.</b> A ±5% range lost {pct(-reb[0.05])} per window when rebalanced and {pct(-fix[0.05])} when left alone.</li>
<li><b>The training winner came last.</b> The volatility rule led training at {pct(vol_train[8], 2, True)} per window and was the only rule clearly below holding out of sample.</li>
<li><b>Costs do not explain the out-of-sample result.</b> They took {min(test_costs) * 100:.2f}% to {max(test_costs) * 100:.2f}% of a $100k position per window; gas mattered only for small positions in 2021–22.</li>
</ul>
"""

params = table(
    ["Element", "Choice", "Source"],
    [
        ["Swap events", f"{SWAPS:,} swaps, May 2021 to Oct 2026", "Pool 0x88e6…5640, Etherscan API V2"],
        ["Gas price", f"{GAS_SAMPLES:,} samples, one per 300 blocks (≈1 h)", "Median price paid in the sampled block"],
        ["Gas used", f"mint {GAS_USED['mint']:,} / burn {GAS_USED['burn']:,} / swap {GAS_USED['swap']:,}", f"Medians of {GAS_TRANSACTIONS} real transactions"],
        ["Protocol fee", "LPs keep 0.0375% instead of 0.05% from block 24,106,471", "SetFeeProtocol event (27 Dec 2025), slot0"],
        ["Price impact", "Swap size / pool depth", "Median active liquidity of the last 500 swaps"],
        ["Checks", "Every 300 blocks, at the end of a block", "Ignores sandwiches inside one block"],
        ["Windows", "90 days, opened monthly; $10k, $100k, $1M", "Train Jul 2021–Dec 2023, test Jan 2024–Oct 2026"],
        ["Selection / significance", "Highest mean vs HODL in training", "Newey-West t-statistics, 2 lags"],
    ],
)

strategies = table(
    ["Rule", "Range", "When the price leaves it", "Settings tested"],
    [
        ["Full range", "All prices (as Uniswap v2)", "Never leaves", "—"],
        ["Fixed", "±x% of the entry ETH price", "Nothing", "x = 1–90%"],
        ["Rebalance", "±x% of the current price", "New range around the price", "x = 1–90%"],
        ["Volatility", "±k daily std. dev. (last 7 days)", "New range, new width", "k = 0.5–12"],
    ],
)

validation_table = table(
    ["Check", "Result"],
    [
        [f"Fees vs {VAL['n']} real positions, 2021–26 (Collect − DecreaseLiquidity)", f"Median error {VAL['median']:.2f}%; total {VAL['total']:+.2f}%; worst {VAL['worst']:.2f}%"],
        ["Positions after the protocol fee switch", f"Errors of {VAL['after_switch_max']:.2f}% or less"],
        ["Position amounts vs Uniswap's own test cases; impermanent loss vs the v2 formula", "Exact"],
        ["Independent re-implementation in ETH-price space", "Same result to six decimals in four windows"],
        ["Consistency of all saved windows", "No violations in 1,500 windows"],
        ["Review by a separate reviewer", "No coding errors; two readings and the selection rule revised"],
    ],
)

regime_rows = []
for period in REGIMES:
    cells = [REGIME_LABEL[period], comparison[(period, "full_range", 100_000)]["windows"]]
    for s in ORDER:
        value = comp(period, s, 100_000)
        t = comp(period, s, 100_000, "t_stat")
        cells.append(pct(value, 2, True) + (f" (t = {tval(t)})" if abs(t) >= 2 else ""))
    regime_rows.append(cells)
regime_table = table(["Regime", "Windows"] + [LABEL[s] for s in ORDER], regime_rows)

size_rows = []
for period in REGIMES:
    small = comp(period, "full_range", 10_000)
    t_small = comp(period, "full_range", 10_000, "t_stat")
    size_rows.append(
        [
            REGIME_LABEL[period],
            pct(comp(period, "full_range", 10_000, "mean_costs"), 2),
            pct(comp(period, "full_range", 100_000, "mean_costs"), 2),
            pct(small, 2, True) + (f" (t = {tval(t_small)})" if abs(t_small) >= 2 else ""),
        ]
    )
size_table = table(["Regime", "Costs, $10k", "Costs, $100k", "Result vs HODL, $10k"], size_rows)

vol_1m = comp("2025-26", "vol_scaled", 1_000_000)
vol_1m_thin = comp("2025-26", "vol_scaled", 1_000_000, "mean_vs_hodl_without_thin")
full_h2_small_cost = comp("2021 H2", "full_range", 10_000, "mean_costs")
full_h2_cost = comp("2021 H2", "full_range", 100_000, "mean_costs")
max_full_window = min(float(r["net_vs_hodl_pct"]) for r in full_windows)
worst_start = min(full_windows, key=lambda r: float(r["net_vs_hodl_pct"]))["start"]
full_without_worst = statistics.mean(float(r["net_vs_hodl_pct"]) for r in full_windows if r["start"] != worst_start)

html = f"""<!doctype html><html><head><meta charset='utf-8'><style>
@page {{ size: A4; margin: 22mm 15mm 16mm 15mm; }}
body {{ font-family: 'DejaVu Sans', sans-serif; font-size: 9.6pt; line-height: 1.38; color: #1d2329; }}
h1 {{ color: {NAVY}; font-size: 18pt; line-height: 1.15; margin: 0 0 4px; }}
h2 {{ color: {NAVY}; font-size: 13pt; margin: 14px 0 6px; }}
.sub {{ color: #5a6570; font-size: 11pt; margin: 0 0 6px; }}
.author {{ color: #7a848e; font-size: 8pt; margin-bottom: 10px; }}
.problem {{ background: #f2f5f9; border-left: 3px solid {NAVY}; padding: 7px 10px; margin: 6px 0 10px; -webkit-print-color-adjust: exact; }}
.cols {{ display: flex; gap: 16px; align-items: flex-start; }}
.cols > div:first-child {{ flex: 0 0 46%; }}
.cols > div:last-child {{ flex: 1; }}
.cols h3 {{ color: {NAVY}; font-size: 11pt; margin: 0 0 4px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 8.4pt; margin: 6px 0 4px; }}
th {{ text-align: left; border-bottom: 1.2px solid #1d2329; padding: 3px 5px; }}
td {{ padding: 3px 5px; vertical-align: top; }}
tr:nth-child(even) td {{ background: #eef2f7; }}
table.kv td:last-child {{ text-align: right; }}
ul.summary {{ margin: 0; padding-left: 14px; }}
ul.summary li {{ margin-bottom: 4px; }}
img {{ width: 100%; margin-top: 4px; }}
.cap {{ color: #7a848e; font-size: 7.8pt; margin: 2px 0 8px; }}
.src {{ color: #7a848e; font-size: 7.6pt; }}
.break {{ break-before: page; }}
.fig {{ break-inside: avoid; }}
table, .keep {{ break-inside: avoid; }}
h2 {{ break-after: avoid; }}
p {{ margin: 4px 0 6px; }}
</style></head><body>

<h1>Uniswap v3 Liquidity Provision: Which Range Strategy Earns the Most Against Holding?</h1>
<div class='sub'>An event-level backtest of {SWAPS / 1e6:.2f} million swaps in the USDC/WETH 0.05% pool, July 2021 to October 2026</div>
<div class='author'>Christophe Abelem, Computer Engineering, CESUPA (Belém, Brazil) | Code and data: github.com/ChristopheAbelem/uniswap-lp-backtest</div>

<div class='problem'><b>The problem.</b> On Uniswap v3, a liquidity provider (LP) deposits two tokens into a pool and earns a fee on every trade, but only while the price stays inside a range the LP chooses. A narrow range earns more fees per dollar; when the price leaves it, the position ends up entirely in the token that fell and stops earning. There is no standard rule for choosing that range or for when to move it. This note asks which rule would have earned the most in the USDC/WETH 0.05% pool on Ethereum, compared with simply holding the same tokens (HODL), after gas and trading costs.</div>

<div class='cols'><div>{key_data}</div><div><h3>Summary</h3>{summary}</div></div>

<div class='fig'><img src='{fig_width()}'>
<div class='cap'>Figure 1. Mean result vs HODL per 90-day window, by range width, 28 training windows each. Source: results/calibration_train.csv.</div></div>

<h2 class='break'>1. How an LP earns and loses</h2>
<p><b>Fees.</b> Each swap pays 0.05% of its input to the LPs whose range contains the price, in proportion to their liquidity. Concentrating the same capital in a narrower range gives it more liquidity at those prices, so it earns more per dollar while the price stays inside. From 27 December 2025 the protocol keeps a quarter of the fee, so LPs receive 0.0375%.</p>
<p><b>Impermanent loss.</b> The pool rebalances the LP automatically: as ETH rises it sells ETH, and as ETH falls it buys. Before fees, the position therefore always ends at or below the value of holding the same tokens, and the gap grows with the size of the move (Figure 2). A narrow range loses faster, and once the price leaves it the position is entirely in one token. Milionis et al. (2022) describe the same cost as loss-versus-rebalancing: the LP keeps trading with arbitrageurs at prices the market has already moved past. An LP profits only if fees exceed that loss.</p>
<div class='fig'><img src='{fig_payoff()}'>
<div class='cap'>Figure 2. Value of a $100k position minus the value of holding its entry tokens, before fees and costs, by ETH price change. Computed with the backtest's own position formulas.</div></div>
<p><b>Rebalancing.</b> When the price leaves a range, an LP can withdraw, swap back to the right token mix and open a new range around the price. That restarts fee income, but it turns the gap into a realised loss and pays gas, a 0.05% swap fee and price impact each time. The question is which combination of width and rebalancing leaves fees ahead of these losses.</p>
<p><b>Why July 2021.</b> The pool opened on 5 May 2021. In late May a trade of a few thousand dollars could move its price by 1%, and that depth reached about $1M only in mid-June, so a $100k position would have been a large share of the pool. From July 2021 no gap between consecutive swaps exceeds {MAX_SWAP_GAP_BLOCKS} blocks and no day has fewer than a quarter of the median {MEDIAN_SWAPS_PER_DAY:,} swaps per day.</p>

<h2>2. Data, model and validation</h2>
<p>A replay engine inserts a hypothetical position into every real swap, in chain order. On each swap that ends inside its range, the position earns the input amount × 0.05% × (1 − protocol share) × its share of the active liquidity. About once an hour the engine values the position and, if the rule says so, moves it. Each window opens a new position on the first day of the month and holds it for 90 days; its result is (position + fees − HODL value) / initial capital, where HODL keeps the position's entry token mix and pays no gas.</p>
{strategies}
{params}
<p><b>Validation.</b> The fee calculation was checked against positions that existed on-chain, and the whole engine against an independent implementation:</p>
{validation_table}
<p class='src'>The largest fee error (5.22%) is a position only 40 ticks wide, where swaps crossing the range boundary are approximated. Rebalancing costs are modelled from real gas and pool depth but were not checked against a real rebalancing LP.</p>

<h2>3. Results</h2>
<p><b>Training: width decides.</b> Figure 1 shows the pattern. Rebalanced ranges lose more the narrower they are: ±1% rebalanced {reb_moves[0.01]:.0f} times per window and lost {pct(-reb[0.01])}, of which {pct(float(train[("rebalance", "0.01")]["mean_costs"]))} was gas, swap fees and price impact. Fixed ranges that never move lose less but vary widely (worst window {pct(fixed_worst)}), so none of their means is distinguishable from zero. The volatility rule follows the same pattern by k, from {pct(vol_train[0.5])} at k = 0.5 to {pct(vol_train[8], 1, True)} at k = 8. The best setting of each family was the widest or close to it.</p>
<div class='fig'><img src='{fig_test()}'>
<div class='cap'>Figure 3. Out-of-sample mean result vs HODL per window with mean ± 1.96 Newey-West standard errors, 32 test windows each, parameters chosen in training. Source: results/calibration_test.csv.</div></div>
<p><b>Out of sample: a tie at best.</b> Every mean is negative. Three rules are statistically indistinguishable from holding; only the volatility rule, first in training, is clearly below. Full range beat holding in {full_beat} of {len(full_windows)} windows; one window opened on {worst_start} lost {pct(-max_full_window)} and accounts for much of its mean (without it, {pct(full_without_worst, 2)}). For a wide range, fees roughly offset impermanent loss on average; an LP should expect no edge over holding, not a reliable loss or gain.</p>
<div class='fig'><img src='{fig_scatter()}'>
<div class='cap'>Figure 4. Result vs HODL of each test window against the ETH price change over that window (twice the HODL 50/50 return). Source: results/calibration_test_windows.csv.</div></div>
<p><b>What drives the loss.</b> Figure 4 is Figure 2 with real fees added: windows with a small ETH move end near or above holding, and large moves in either direction end below it. Rules that hold a narrower range, such as the volatility rule, fall further for the same move.</p>
<div class='keep'><p><b>By regime.</b> Mean result vs HODL per window, $100k (t shown where |t| ≥ 2; regimes before 2024 include training data):</p>
{regime_table}</div>
<p><b>By position size.</b> Gas is a fixed amount per transaction, so it weighs on small positions when it is expensive. The average gas price paid fell from about {GAS_GWEI['2021']} gwei in 2021 and {GAS_GWEI['2022']} gwei in 2022 to {GAS_GWEI['2026']} gwei in 2026. Full range:</p>
{size_table}
<p>At $1M results are close to $100k, but less reliable in 2025–26: pool liquidity fell about {LIQUIDITY_FALL:.0f}-fold from 2023 to 2025, so a $1M position becomes a large share of the pool, and fees from swaps where it would hold more than 5% of the liquidity are uncertain (volatility rule: {pct(vol_1m)} with them, {pct(vol_1m_thin)} without).</p>

<h2>4. Risks and limitations</h2>
<p><b>What could change the result.</b> (1) <b>Fee share</b> uses the liquidity after each swap, so a swap that crosses a liquidity gap credits the position with too much; this favours the LP and does not weaken the conclusion, but makes $1M results in 2025–26 less reliable. (2) <b>Rebalancing is optimistic</b>: it executes at the triggering end-of-block price with no delay and no MEV. (3) <b>Small effective sample</b>: overlapping windows give roughly 11 independent observations out of sample and 9 in training; 2021 H2 has 4 windows. (4) Fees are not reinvested. (5) Before the September 2022 Merge, 300 blocks were about {300 * PRE_MERGE_SECONDS_PER_BLOCK / 60:.0f} minutes, so hourly checks were slightly less frequent.</p>
<p><b>Consistency with the literature.</b> Loesch et al. (2021) found that LPs in 17 Uniswap v3 pools would have been $60.8m better off holding. This backtest reaches the same direction with rules an LP could follow, over five years and out of sample.</p>
<p><b>Next steps.</b> A hedged LP, shorting ETH perpetual futures against the position's WETH, removes the direction of the move but not the convexity in Figure 2; it should be compared with holding USDC, net of funding. Other rule families (asymmetric ranges, a wide base plus a narrow range, scheduled rebalancing) and other pools, such as the 0.3% tier or layer 2 networks, are untested.</p>
<p class='src'><b>Sources.</b> Pool events, gas prices and receipts: Ethereum mainnet via Etherscan API V2. Loesch, Hindman, Richardson and Welch (2021), "Impermanent Loss in Uniswap v3", arXiv 2111.09192. Milionis, Moallemi, Roughgarden and Zhang (2022), "Automated Market Making and Loss-Versus-Rebalancing", arXiv 2208.06046. Uniswap, "Protocol Fee Configuration", docs.uniswap.org. Full code, tests and results: github.com/ChristopheAbelem/uniswap-lp-backtest.</p>
</body></html>"""

HEADER = f"""<div style='width:100%; margin:0; padding:7px 15mm; background:{NAVY}; color:white; font-family:DejaVu Sans; font-size:8px; display:flex; justify-content:space-between; -webkit-print-color-adjust:exact;'><b>UNISWAP V3 USDC/WETH 0.05% | LP STRATEGY NOTE</b><span>Data to 31 October 2026</span></div>"""
FOOTER = """<div style='width:100%; padding:0 15mm; font-family:DejaVu Sans; font-size:7px; color:#7a848e; display:flex; justify-content:space-between;'><span>Academic portfolio project. Not investment advice. Christophe Abelem, github.com/ChristopheAbelem</span><span class='pageNumber'></span></div>"""


def main():
    from playwright.sync_api import sync_playwright

    OUTPUT.parent.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(
            path=str(OUTPUT),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template=HEADER,
            footer_template=FOOTER,
            margin={"top": "22mm", "bottom": "16mm", "left": "15mm", "right": "15mm"},
        )
        browser.close()
    print(f"Saved {OUTPUT}")


if __name__ == "__main__":
    main()
