"""Ad-hoc BuckBasket flow simulator over the commodity price CSVs.

This is intentionally simpler than the Anvil-backed rebalancing sim:

* day-0 prices define the basket recipe and target value weights;
* investor deposits buy the most underweight commodity;
* investor redemptions sell overweight commodities in the amounts that move
  the post-redemption basket closest to target;
* shares are minted/redeemed at current basket NAV per share.

The investment approach is constant-mix rebalancing implemented through
cash-flow rebalancing: new deposits buy underweight constituents and exits
sell overweight constituents.  The expected edge is usually described as a
rebalancing premium, diversification return, or volatility harvesting.  It is
a contrarian strategy: it tries to capture relative commodity volatility and
mean reversion by systematically selling relative winners and buying relative
losers.

Potential future tests/optimizations:

* compare constant-value, fixed-unit recipe, and BuckBasket inverse-price
  target definitions;
* compare against passive buy-and-hold, periodic full rebalancing, and pure
  proportional deposit/withdrawal baselines;
* sweep rebalance bands, small-redemption fast-path thresholds, and trade
  sizing rules;
* add transaction costs, AMM slippage, oracle lag, liquidity limits, MEV/tax
  haircuts, and custody/storage costs;
* stress different volatility/correlation regimes and investor flow/holding
  time distributions;
* optimize target weights for expected diversification return net of costs
  and risk constraints.

Run:

    python -m alberta_buck.sim.basket_flow
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CSV_DIR = HERE / "prices"
DEFAULT_OUT = REPO / "test" / "vectors" / "basket-flow-sim.json"

DEFAULT_TOKENS = [
    ("PAXG", "paxg.csv"),
    ("cbBTC", "cbbtc.csv"),
    ("AOIL", "aoil.csv"),
]


@dataclass
class Investor:
    investor_id: int
    entry_day: int
    exit_day: int
    shares: float
    invested: float
    entry_share_price: float
    entry_index: float


@dataclass
class ExitResult:
    investor_id: int
    entry_day: int
    exit_day: int
    hold_days: int
    invested: float
    returned: float
    benchmark_returned: float
    roi: float
    benchmark_roi: float
    sold_token: str


@dataclass
class Trade:
    day: int
    kind: str
    token: str
    amount_usd: float
    units: float
    investor_id: int


def _read_prices(files: Sequence[str]) -> list[list[float]]:
    series: list[list[float]] = []
    for filename in files:
        rows: list[float] = []
        with (CSV_DIR / filename).open() as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(int(row["close_usd_micro"]) / 1_000_000)
        series.append(rows)
    if not series:
        raise ValueError("no price series")
    days = min(len(s) for s in series)
    return [s[:days] for s in series]


def _parse_weights(raw: str, n: int) -> list[float]:
    parts = [float(p.strip()) for p in raw.split(",") if p.strip()]
    if len(parts) != n:
        raise ValueError(f"expected {n} weights, got {len(parts)}")
    total = sum(parts)
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
    if any(p < 0 for p in parts):
        raise ValueError("weights must be non-negative")
    return [p / total for p in parts]


def _values(units: Sequence[float], prices: Sequence[float]) -> list[float]:
    return [u * p for u, p in zip(units, prices)]


def _nav(units: Sequence[float], prices: Sequence[float]) -> float:
    return sum(_values(units, prices))


def _basket_index(weights: Sequence[float],
                  initial_prices: Sequence[float],
                  prices: Sequence[float]) -> float:
    return sum(w * p / p0 for w, p0, p in zip(weights, initial_prices, prices))


def _target_values(mode: str,
                   nav: float,
                   total_shares: float,
                   weights: Sequence[float],
                   recipe_units_per_share: Sequence[float],
                   prices: Sequence[float]) -> list[float]:
    if mode == "constant-value":
        return [nav * w for w in weights]
    if mode == "unit-recipe":
        return [
            total_shares * units_per_share * price
            for units_per_share, price in zip(recipe_units_per_share, prices)
        ]
    raise ValueError(f"unknown target mode: {mode}")


def _ratios(values: Sequence[float], targets: Sequence[float]) -> list[float]:
    return [
        (value / target) if target > 0 else float("inf")
        for value, target in zip(values, targets)
    ]


def _choose_extreme(rng: random.Random, ratios: Sequence[float], want_min: bool) -> int:
    best = min(ratios) if want_min else max(ratios)
    # Avoid hard-coding a first-token tie break when the basket starts
    # exactly on target.
    eps = 1e-12
    candidates = [
        i for i, ratio in enumerate(ratios)
        if abs(ratio - best) <= eps
    ]
    return rng.choice(candidates)


def _weights(values: Sequence[float]) -> list[float]:
    total = sum(values)
    if total <= 0:
        return [0.0 for _ in values]
    return [v / total for v in values]


def _redemption_allocations(redeem_value: float,
                            values: Sequence[float],
                            target_values: Sequence[float],
                            small_redeem_bp: float) -> list[float]:
    """Allocate a redemption using BuckBasket._allocateRedemption's shape.

    Ideal post-redemption state:

        alloc_i = value_i - target_weight_i * (NAV - redeem_value)

    Positive ideal allocations are sold first.  If the redemption is too large
    for those positive excesses, the remainder is taken proportionally from all
    current holdings.
    """
    nav = sum(values)
    alloc = [0.0 for _ in values]
    if redeem_value <= 0 or nav <= 0:
        return alloc
    redeem_value = min(redeem_value, nav)

    total_target = sum(target_values)
    if total_target <= 0:
        return [redeem_value * value / nav for value in values]

    post_nav = max(nav - redeem_value, 0.0)
    positive = [0.0 for _ in values]
    total_positive = 0.0
    most_ov_idx = 0
    most_ov_abs_excess = 0.0

    for i, value in enumerate(values):
        target_post = target_values[i] * post_nav / total_target
        if value > target_post:
            positive[i] = value - target_post
            total_positive += positive[i]

        target_now = target_values[i] * nav / total_target
        excess_now = value - target_now
        if excess_now > most_ov_abs_excess:
            most_ov_abs_excess = excess_now
            most_ov_idx = i

    if (most_ov_abs_excess > 0
            and redeem_value * 10_000 <= small_redeem_bp * values[most_ov_idx]):
        alloc[most_ov_idx] = redeem_value
        return alloc

    from_positive = min(redeem_value, total_positive)
    if from_positive > 0 and total_positive > 0:
        for i, value in enumerate(positive):
            if value > 0:
                alloc[i] = value * from_positive / total_positive

    if redeem_value > total_positive:
        remainder = redeem_value - total_positive
        for i, value in enumerate(values):
            alloc[i] += value * remainder / nav

    return alloc


def _frame(day: int,
           symbols: Sequence[str],
           units: Sequence[float],
           prices: Sequence[float],
           initial_prices: Sequence[float],
           total_shares: float,
           weights: Sequence[float],
           recipe_units_per_share: Sequence[float],
           target_mode: str,
           active: Sequence[Investor],
           cumulative_invested: float,
           cumulative_returned: float,
           realized: Sequence[ExitResult]) -> dict:
    nav = _nav(units, prices)
    actual_values = _values(units, prices)
    target_values = _target_values(
        target_mode, nav, total_shares, weights, recipe_units_per_share, prices)
    target_total = sum(target_values)
    share_price = nav / total_shares if total_shares else 0.0
    realized_invested = sum(e.invested for e in realized)
    realized_returned = sum(e.returned for e in realized)
    realized_benchmark = sum(e.benchmark_returned for e in realized)
    return {
        "day": day,
        "prices": dict(zip(symbols, prices)),
        "units": dict(zip(symbols, units)),
        "nav": nav,
        "shares": total_shares,
        "sharePrice": share_price,
        "passiveIndex": _basket_index(weights, initial_prices, prices),
        "actualWeights": dict(zip(symbols, _weights(actual_values))),
        "targetWeights": dict(zip(symbols, _weights(target_values))),
        "targetRatios": dict(zip(symbols, _ratios(actual_values, target_values))),
        "targetValueTotal": target_total,
        "activeInvestors": len(active),
        "cumulativeInvested": cumulative_invested,
        "cumulativeReturned": cumulative_returned,
        "realizedInvestorRoi": (
            realized_returned / realized_invested - 1.0
            if realized_invested else 0.0
        ),
        "realizedBenchmarkRoi": (
            realized_benchmark / realized_invested - 1.0
            if realized_invested else 0.0
        ),
    }


def run(days: int | None = None,
        seed: int = 0xA1BC,
        weights: Sequence[float] | None = None,
        initial_nav: float = 1_000_000.0,
        entry_amount: float = 25_000.0,
        entry_interval_days: int = 7,
        hold_days: int = 180,
        hold_jitter_days: int = 90,
        trade_cost_bp: float = 0.0,
        small_redeem_bp: float = 100.0,
        target_mode: str = "constant-value") -> dict:
    symbols = [t[0] for t in DEFAULT_TOKENS]
    files = [t[1] for t in DEFAULT_TOKENS]
    prices_by_token = _read_prices(files)
    max_days = min(len(s) for s in prices_by_token)
    if days is None:
        days = max_days
    days = max(1, min(days, max_days))
    prices_by_token = [s[:days] for s in prices_by_token]
    if weights is None:
        weights = [1.0 / len(symbols) for _ in symbols]
    weights = list(weights)
    initial_prices = [s[0] for s in prices_by_token]

    if initial_nav <= 0:
        raise ValueError("initial_nav must be positive")
    if entry_amount < 0:
        raise ValueError("entry_amount must be non-negative")
    if entry_interval_days <= 0:
        raise ValueError("entry_interval_days must be positive")
    if hold_days <= 0:
        raise ValueError("hold_days must be positive")
    if hold_jitter_days < 0:
        raise ValueError("hold_jitter_days must be non-negative")
    if trade_cost_bp < 0:
        raise ValueError("trade_cost_bp must be non-negative")
    if small_redeem_bp < 0:
        raise ValueError("small_redeem_bp must be non-negative")

    rng = random.Random(seed)
    total_shares = initial_nav
    recipe_units_per_share = [
        weight / price for weight, price in zip(weights, initial_prices)
    ]
    units = [
        initial_nav * weight / price
        for weight, price in zip(weights, initial_prices)
    ]

    active: list[Investor] = []
    realized: list[ExitResult] = []
    trades: list[Trade] = []
    frames: list[dict] = []
    next_investor_id = 1
    cumulative_invested = 0.0
    cumulative_returned = 0.0
    cost_frac = trade_cost_bp / 10_000.0
    low_hold = max(1, hold_days - hold_jitter_days)
    high_hold = hold_days + hold_jitter_days

    for day in range(days):
        prices = [series[day] for series in prices_by_token]

        # Redemptions happen before new deposits for the day. This makes
        # exits rely only on already-committed basket inventory.
        exiting = [investor for investor in active if investor.exit_day <= day]
        if exiting:
            active = [investor for investor in active if investor.exit_day > day]
        for investor in exiting:
            nav_before = _nav(units, prices)
            share_price = nav_before / total_shares if total_shares else 0.0
            redeem_value = investor.shares * share_price
            actual_values = _values(units, prices)
            targets = _target_values(
                target_mode, nav_before, total_shares, weights,
                recipe_units_per_share, prices)
            allocations = _redemption_allocations(
                redeem_value, actual_values, targets, small_redeem_bp)
            gross_sold = 0.0
            sold_tokens: list[str] = []
            for sell_idx, gross_sale in enumerate(allocations):
                available = units[sell_idx] * prices[sell_idx]
                gross_sale = min(gross_sale, available)
                if gross_sale <= 1e-9:
                    continue
                units_sold = (
                    gross_sale / prices[sell_idx] if prices[sell_idx] else 0.0
                )
                units[sell_idx] -= units_sold
                gross_sold += gross_sale
                sold_tokens.append(symbols[sell_idx])
                trades.append(Trade(
                    day=day,
                    kind="sell",
                    token=symbols[sell_idx],
                    amount_usd=gross_sale,
                    units=units_sold,
                    investor_id=investor.investor_id,
                ))
            total_shares -= investor.shares
            returned = gross_sold * (1.0 - cost_frac)
            cumulative_returned += returned
            benchmark_returned = (
                investor.invested
                * _basket_index(weights, initial_prices, prices)
                / investor.entry_index
            )
            realized.append(ExitResult(
                investor_id=investor.investor_id,
                entry_day=investor.entry_day,
                exit_day=day,
                hold_days=day - investor.entry_day,
                invested=investor.invested,
                returned=returned,
                benchmark_returned=benchmark_returned,
                roi=returned / investor.invested - 1.0,
                benchmark_roi=benchmark_returned / investor.invested - 1.0,
                sold_token=",".join(sold_tokens),
            ))

        if entry_amount > 0 and day % entry_interval_days == 0:
            nav_before = _nav(units, prices)
            share_price = nav_before / total_shares if total_shares else 1.0
            actual_values = _values(units, prices)
            targets = _target_values(
                target_mode, nav_before, total_shares, weights,
                recipe_units_per_share, prices)
            ratios = _ratios(actual_values, targets)
            buy_idx = _choose_extreme(rng, ratios, want_min=True)
            net_investment = entry_amount * (1.0 - cost_frac)
            bought_units = (
                net_investment / prices[buy_idx] if prices[buy_idx] else 0.0
            )
            units[buy_idx] += bought_units
            shares = net_investment / share_price if share_price else 0.0
            total_shares += shares
            sampled_hold = rng.randint(low_hold, high_hold)
            exit_day = day + sampled_hold
            active.append(Investor(
                investor_id=next_investor_id,
                entry_day=day,
                exit_day=exit_day,
                shares=shares,
                invested=entry_amount,
                entry_share_price=share_price,
                entry_index=_basket_index(weights, initial_prices, prices),
            ))
            cumulative_invested += entry_amount
            trades.append(Trade(
                day=day,
                kind="buy",
                token=symbols[buy_idx],
                amount_usd=net_investment,
                units=bought_units,
                investor_id=next_investor_id,
            ))
            next_investor_id += 1

        frames.append(_frame(
            day, symbols, units, prices, initial_prices, total_shares, weights,
            recipe_units_per_share, target_mode, active, cumulative_invested,
            cumulative_returned, realized))

    final_prices = [series[-1] for series in prices_by_token]
    final_nav = _nav(units, final_prices)
    final_share_price = final_nav / total_shares if total_shares else 0.0
    final_index = _basket_index(weights, initial_prices, final_prices)
    realized_invested = sum(e.invested for e in realized)
    realized_returned = sum(e.returned for e in realized)
    realized_benchmark = sum(e.benchmark_returned for e in realized)
    active_value = sum(i.shares * final_share_price for i in active)
    active_benchmark = sum(
        i.invested * final_index / i.entry_index
        for i in active
    )
    all_invested = realized_invested + sum(i.invested for i in active)
    all_strategy_value = realized_returned + active_value
    all_benchmark_value = realized_benchmark + active_benchmark

    buys = {sym: 0 for sym in symbols}
    sells = {sym: 0 for sym in symbols}
    for trade in trades:
        if trade.kind == "buy":
            buys[trade.token] += 1
        elif trade.kind == "sell":
            sells[trade.token] += 1

    summary = {
        "days": days,
        "targetMode": target_mode,
        "seed": seed,
        "entries": next_investor_id - 1,
        "exits": len(realized),
        "active": len(active),
        "initialNav": initial_nav,
        "finalNav": final_nav,
        "finalShares": total_shares,
        "finalSharePrice": final_share_price,
        "passiveFinalIndex": final_index,
        "sharePriceExcessVsPassive": (
            final_share_price / final_index - 1.0 if final_index else 0.0
        ),
        "realizedInvestorRoi": (
            realized_returned / realized_invested - 1.0
            if realized_invested else 0.0
        ),
        "realizedBenchmarkRoi": (
            realized_benchmark / realized_invested - 1.0
            if realized_invested else 0.0
        ),
        "allInvestorRoi": (
            all_strategy_value / all_invested - 1.0 if all_invested else 0.0
        ),
        "allBenchmarkRoi": (
            all_benchmark_value / all_invested - 1.0 if all_invested else 0.0
        ),
        "buysByToken": buys,
        "sellsByToken": sells,
    }

    return {
        "tokens": symbols,
        "weights": dict(zip(symbols, weights)),
        "initialPrices": dict(zip(symbols, initial_prices)),
        "recipeUnitsPerShare": dict(zip(symbols, recipe_units_per_share)),
        "config": {
            "initialNav": initial_nav,
            "entryAmount": entry_amount,
            "entryIntervalDays": entry_interval_days,
            "holdDays": hold_days,
            "holdJitterDays": hold_jitter_days,
            "tradeCostBp": trade_cost_bp,
            "smallRedeemBp": small_redeem_bp,
            "targetMode": target_mode,
            "seed": seed,
        },
        "summary": summary,
        "frames": frames,
        "exits": [asdict(e) for e in realized],
        "trades": [asdict(t) for t in trades],
    }


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _pct(v: float) -> str:
    return f"{v * 100:+.2f}%"


def _print_summary(result: dict, out: Path | None) -> None:
    summary = result["summary"]
    if out is not None:
        print(f"[basket-flow] wrote {out.relative_to(REPO)}")
    print(
        f"  mode={summary['targetMode']}  days={summary['days']}  "
        f"entries={summary['entries']}  exits={summary['exits']}  "
        f"active={summary['active']}"
    )
    print(
        "  final share price "
        f"{_money(summary['finalSharePrice'])} vs passive recipe index "
        f"{summary['passiveFinalIndex']:.4f} "
        f"({_pct(summary['sharePriceExcessVsPassive'])} excess)"
    )
    print(
        "  realized investors "
        f"{_pct(summary['realizedInvestorRoi'])} vs passive "
        f"{_pct(summary['realizedBenchmarkRoi'])}"
    )
    print(
        "  realized + active mark "
        f"{_pct(summary['allInvestorRoi'])} vs passive "
        f"{_pct(summary['allBenchmarkRoi'])}"
    )
    print(f"  buys:  {summary['buysByToken']}")
    print(f"  sells: {summary['sellsByToken']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m alberta_buck.sim.basket_flow")
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--seed", type=lambda s: int(s, 0), default=0xA1BC)
    parser.add_argument("--weights", default="1,1,1",
                        help="comma-separated target weights")
    parser.add_argument("--initial-nav", type=float, default=1_000_000.0)
    parser.add_argument("--entry-amount", type=float, default=25_000.0)
    parser.add_argument("--entry-interval-days", type=int, default=7)
    parser.add_argument("--hold-days", type=int, default=180)
    parser.add_argument("--hold-jitter-days", type=int, default=90)
    parser.add_argument("--trade-cost-bp", type=float, default=0.0)
    parser.add_argument(
        "--small-redeem-bp",
        type=float,
        default=100.0,
        help="single-overweight-commodity fast path threshold",
    )
    parser.add_argument(
        "--target-mode",
        choices=("constant-value", "unit-recipe"),
        default="constant-value",
        help=("constant-value sells winners toward fixed value weights; "
              "unit-recipe targets the fixed commodity units per share"),
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="JSON output path; pass '' to skip writing")
    args = parser.parse_args(argv)

    weights = _parse_weights(args.weights, len(DEFAULT_TOKENS))
    result = run(
        days=args.days,
        seed=args.seed,
        weights=weights,
        initial_nav=args.initial_nav,
        entry_amount=args.entry_amount,
        entry_interval_days=args.entry_interval_days,
        hold_days=args.hold_days,
        hold_jitter_days=args.hold_jitter_days,
        trade_cost_bp=args.trade_cost_bp,
        small_redeem_bp=args.small_redeem_bp,
        target_mode=args.target_mode,
    )

    out = Path(args.out) if args.out else None
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
    _print_summary(result, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
