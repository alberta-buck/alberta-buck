"""Render who collects the rebalancing premium, on the reverting regime.

    make nix-sim-plot-basket-split      # -> images/basket-split-revert.png

The rebalancing article's Figure 1 (`rebalance-mechanism.png`, from
plot_rebalance_article.fig_mechanism) shows one constituent's deviation from
its target weight with buy and sell markers on the turns.  It is a picture of
a POLICY acting on an excursion, drawn from the Python model, where the
policy is the only actor.

This is the same picture for the chain simulation, where the policy is not
the only actor, and it answers a question that figure cannot: buying low and
selling high in a mean-reverting commodity should make money, so why does the
BuckBasket depositor end 5.62%/yr down on price series that finish exactly
where they began?

The headline number turns out to be the wrong question, and the panels are
what show it.  THE DEPOSITOR IS UP ABOUT ONE MILLION DOLLARS AT DAY 500, with
the treasury up 460k BUCK beside it.  Both legs are positive for two thirds
of the run.  The loss is not a slow bleed; it is a cliff, and the last panel
dates it.

WHAT BREAKS

Redeem settles a claim R -- the BUCK principal minted at deposit -- out of
the Bw BUCK that withdrawing the depositor's liquidity actually yields.
BuckBasketProRata._redeem, phase 2:

    if (Bw >= R) { burned = R; treasuryBuck = Bw - R; }   // gain -> treasury
    else         { convertIntoBucks(perPoolTok, R - Bw);  // loss -> depositor
                   perPoolTok = inv; }

The payoff is asymmetric by construction.  Excess BUCK is treasury equity;
a shortfall is covered by converting the depositor's own TOKEN away.  That
is deliberate -- paying BUCK to a depositor would oblige them to hold a BUCK
identity, and a commodity LP is supposed to need no credentials.

While BUCK holds parity with the basket, the asymmetry costs almost nothing
and both sides earn.  What flips it is BUCK strengthening against the
basket: then withdrawing a position yields fewer BUCK than R, EVERY redeem
takes the deflation branch, and the depositor pays for it in TOKEN while the
treasury stops accruing entirely.

WHAT MADE IT BREAK, AND IT WAS NOT THE REGIME

The demand leg.  It bought 13.27M BUCK over the run, 2.6M of that between
days 500 and 560, and that sustained bid drove basketValueInBuck from 0.965
to 0.815 -- BUCK about 19% strong against its own anchor.  buckK ran to its
0.95 clamp trying to answer and could not.  A 19% strengthening against a
~$10M book over ~300 redemptions accounts for the $1.54M swing.

So this figure is not evidence that BuckBasket depositors lose in a
reverting market.  It is evidence that one agent can push BUCK far enough
off parity to make every redemption expensive, and that the cost of doing so
lands on depositors rather than on the agent causing it.  That is worth
knowing on its own, and it is a different claim.

A CONFOUND WORTH KEEPING IN VIEW

`DirectMintAgent` is a "probabilistic LP provider": it enters with
probability 1e-3 per tick and exits with probability 5e-3, both Bernoulli and
both blind to price.  Its entries and exits scatter uniformly across the
excursions rather than clustering at the turns -- which is what the event rug
is for.  Whatever these depositors earn, they do not earn it by timing
reversion.  The actors that buy low and sell high are the redemption
allocator (`_allocateSellHigh` draws from overweight pools), `sweepTreasury`
(re-invests into underweight ones) and the director.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

DATA = Path(os.environ.get(
    "SPLIT_VECTOR", REPO / "test" / "vectors" / "rebalancing-sim-revert.json"))
OUT = Path(os.environ.get(
    "SPLIT_OUT", REPO / "images" / "basket-split-revert.png"))

E6 = 10 ** 6


def _rel(p: Path):
    """Repo-relative when it can be, absolute otherwise.

    REB_OUT / SPLIT_OUT are deliberately overridable so a run in
    flight can be plotted to a scratch path; relative_to() raises on
    anything outside the repo, which crashed the summary AFTER the
    figure had already been written.
    """
    try:
        return p.relative_to(REPO)
    except ValueError:
        return p


INK = "#20242c"
INK2 = "#77808f"
SELL = "#e34948"          # same palette as plot_rebalance_article
BUY = "#008300"
TREASURY = "#0b6e4f"
DEPOSITOR = "#b3541e"


@pytest.mark.skipif(not DATA.exists(),
                    reason="run: make nix-sim-rebalancing-revert")
def test_basket_split_plot():
    cache = Path(os.environ.get("TMPDIR", "/tmp")) / "alberta-buck-mpl"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = json.loads(DATA.read_text())
    fr = d["frames"]
    names = d["tokens"]
    days = [f["day"] for f in fr]
    n = len(names)

    fig, axes = plt.subplots(n + 2, 1, figsize=(11.5, 3.1 * (n + 2)),
                             sharex=True)

    # Where BUCK left parity with the basket badly enough to flip every
    # redeem into the deflation branch.  Marked on every panel, because it
    # is the event that explains the split below.
    bvib = [f["basketVal"] / 1e18 for f in fr]
    brk = next((f["day"] for f, v in zip(fr, bvib) if v < 0.90), None)

    # ---- one panel per commodity: the excursion the rebalancer harvests --
    for t in range(n):
        ax = axes[t]
        dev = [f["poolWeights"][t][0] - f["poolWeights"][t][1] for f in fr]
        ax.axhline(0, color=INK, linewidth=0.8, alpha=0.35)
        ax.fill_between(days, 0, dev, where=[v > 0 for v in dev],
                        color=SELL, alpha=0.16, linewidth=0,
                        label="overweight -- the basket sells here")
        ax.fill_between(days, 0, dev, where=[v < 0 for v in dev],
                        color=BUY, alpha=0.16, linewidth=0,
                        label="underweight -- the basket buys here")
        ax.plot(days, dev, color=INK, linewidth=1.1,
                label="deviation from target weight")
        ax.set_ylabel(f"{names[t]}\ndeviation")
        ax.grid(True, alpha=0.25)
        if brk is not None:
            ax.axvline(brk, color=SELL, linewidth=1.2, linestyle=":", alpha=0.8)

        ax2 = ax.twinx()
        px = [f["refUsd"][t] / E6 for f in fr]
        ax2.plot(days, px, color=INK2, linewidth=1.0, linestyle="--",
                 label=f"{names[t]} price")
        ax2.set_ylabel("USD", color=INK2)
        ax2.tick_params(axis="y", labelcolor=INK2)
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=7, ncol=2)
        if t == 0:
            ax.set_title(
                "The excursions are real and they come home: price series "
                "with zero net drift, ending exactly where they began")

    # ---- the cause: BUCK left parity, and the clamp ran out --------------
    #
    # A depositor's claim R is the BUCK principal minted at deposit.  Redeem
    # withdraws their liquidity, yielding Bw BUCK.  When BUCK strengthens
    # against the basket -- basketValueInBuck below 1 -- that withdrawal is
    # worth fewer BUCK than R, so `Bw < R` and the contract converts the
    # depositor's own TOKEN to cover the burn.  Everything below follows from
    # this line crossing down.
    ax = axes[n]
    ax.axhline(1.0, color=INK, linewidth=0.9, linestyle="-.", alpha=0.5)
    ax.plot(days, bvib, color=INK, linewidth=1.6,
            label="basketValueInBuck (1.0 = parity; below it, redeems eat "
                  "depositor TOKEN)")
    ax.set_ylabel("basket value\nin BUCK")
    ax.grid(True, alpha=0.25)
    ax2 = ax.twinx()
    ax2.plot(days, [f["buckK"] / 1e18 for f in fr], color=TREASURY,
             linewidth=1.3, linestyle="--", label="buckK (clamped at 0.95)")
    ax2.axhline(0.95, color=TREASURY, linewidth=0.8, alpha=0.4)
    ax2.set_ylabel("buckK", color=TREASURY)
    ax2.tick_params(axis="y", labelcolor=TREASURY)
    if brk is not None:
        ax.axvline(brk, color=SELL, linewidth=1.2, linestyle=":", alpha=0.8)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="lower left", fontsize=7)
    ax.set_title("The demand leg bought BUCK until it left parity, and the "
                 "controller hit its clamp trying to answer")

    # ---- who collected it ------------------------------------------------
    ax = axes[n + 1]
    prof = [f.get("dmProfitUsd", 0) / E6 for f in fr]
    treas = [f.get("treasuryBuck", 0) / E6 for f in fr]
    ax.axhline(0, color=INK, linewidth=0.8, alpha=0.35)
    ax.plot(days, prof, color=DEPOSITOR, linewidth=1.9,
            label="DEPOSITOR: realized profit on redeemed round-trips (USD)")
    ax.plot(days, treas, color=TREASURY, linewidth=1.9,
            label="TREASURY: retained BUCK profit")
    ax.set_ylabel("cumulative")
    ax.set_xlabel("day")
    ax.grid(True, alpha=0.25)

    # The event rug: every day a deposit or a redemption happened.  Bernoulli
    # timing, so these sit uniformly across the excursions above rather than
    # on their turns -- the depositor is not trading the reversion.
    ent = [f.get("dmEntries", 0) for f in fr]
    ext = [f.get("dmExits", 0) for f in fr]
    lo, hi = ax.get_ylim()
    span = hi - lo
    for series, colour, base, mark in ((ent, BUY, 0.02, "^"),
                                       (ext, SELL, 0.07, "v")):
        xs = [days[i] for i in range(1, len(fr)) if series[i] > series[i - 1]]
        ax.scatter(xs, [lo + base * span] * len(xs), marker=mark, s=7,
                   color=colour, alpha=0.55, linewidths=0)
    ax.set_ylim(lo, hi)

    if brk is not None:
        ax.axvline(brk, color=SELL, linewidth=1.2, linestyle=":", alpha=0.8)
        peak = max(range(len(prof)), key=lambda i: prof[i])
        ax.annotate(f"depositor peaks +${prof[peak]:,.0f}\nwhile BUCK is near parity",
                    xy=(days[peak], prof[peak]), xytext=(0.30, 0.88),
                    textcoords="axes fraction", fontsize=8, color=DEPOSITOR,
                    arrowprops=dict(arrowstyle="->", color=DEPOSITOR, lw=0.9))

    last = fr[-1]
    dd = last.get("dmDollarDays", 0) or 1
    dep_apr = 100.0 * 365.0 * last.get("dmProfitUsd", 0) / dd
    tre_apr = 100.0 * 365.0 * last.get("treasuryBuck", 0) / dd
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title(
        f"Both are positive while BUCK holds parity.  Rug: deposit (green) "
        "and redemption (red) days -- Bernoulli, never on the turns")

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=140)
    plt.close(fig)

    print(f"\nWrote {_rel(OUT)}  ({len(days)} days)")
    print(f"  depositor  {dep_apr:+.2f}% APR   "
          f"(${last.get('dmProfitUsd', 0) / E6:+,.0f} over "
          f"{last.get('dmRoundTrips', 0)} round-trips)")
    print(f"  treasury   {tre_apr:+.2f}% APR   "
          f"({last.get('treasuryBuck', 0) / E6:,.0f} BUCK retained)")
    for t in range(n):
        dev = [f["poolWeights"][t][0] - f["poolWeights"][t][1] for f in fr]
        px = [f["refUsd"][t] for f in fr]
        print(f"  {names[t]:6s} price {px[-1] / px[0]:.3f}x end/start   "
              f"weight deviation [{min(dev):+.4f}, {max(dev):+.4f}]")
