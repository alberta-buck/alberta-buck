"""Render the rebalancing-sim result to images/rebalancing-sim.png.

Workflow:
  1.  make sim-run-rebalancing   # writes test/vectors/rebalancing-sim.json
  2.  make sim-plot-rebalancing   # reads JSON, writes images/rebalancing-sim.png

JSON schema extends routing-sim.json with:
  poolWeights[i] = [actualWeight, targetWeight]
  rebalancerPnl: value of rebalancer portfolio - initial (USDC micro, day-0)
  rebalanceTrades: cumulative rebalance operations
  directMintPnl: current DirectMint agent value - initial value (USDC micro,
                 day-0 accounting)
  dmTotalInvested: cumulative DirectMint entry capital (USDC micro, day-0)
  treasuryBuck: retained basket profit from redeemed DirectMint receipts
"""

import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def _resolve(p: Path) -> Path:
    """Anchor a possibly-relative override against the repo root."""
    return p if p.is_absolute() else (REPO / p)


# Input vector / output image are overridable so the prorata vs traditional
# rebalancing runs can each be plotted independently (REB_VECTOR / REB_OUT).
DATA = _resolve(Path(os.environ.get("REB_VECTOR",
                                    REPO / "test" / "vectors" / "rebalancing-sim.json")))
OUT = _resolve(Path(os.environ.get("REB_OUT",
                                   REPO / "images" / "rebalancing-sim.png")))

# BUCK is USDC-compatible 6-decimal accounting (see BuckTypes.DECIMALS).
# Basket constituent TOKEN balances still use each token's own decimals from
# the sim JSON (`dec` below).
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



def _i(v):
    return int(v)


@pytest.mark.skipif(
    not DATA.exists(),
    reason="rebalancing-sim.json not generated yet; run: "
           "python -m alberta_buck.sim --scenario rebalancing",
)
def test_rebalancing_sim_plot():
    cache_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "alberta-buck-mpl"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    d = json.loads(DATA.read_text())
    names = d["tokens"]
    dec = d.get("decimals", [18, 8, 18])
    fr = d["frames"]
    days = [f["day"] for f in fr]

    def col(key, t):
        return [f.get(key, [])[t] for f in fr]

    fig, axes = plt.subplots(8, 1, figsize=(13, 28), sharex=True)

    # buckUsd = USDC-micro per 1 BUCK from the floating BUCK/USDC pool.
    bu = [f.get("buckUsd", 0) for f in fr]

    def treasury_buck(f):
        """Raw retained treasury BUCK, with fallback for older vectors."""
        if "treasuryBuck" in f:
            return f["treasuryBuck"]
        return int(f.get("treasuryShare", 0.0) * f.get("basketNav", 0))

    # ---- Panels 1-3: per token, both pools vs market reference -------- #
    for t in range(3):
        ax = axes[t]
        ref = [v / E6 for v in col("refUsd", t)]
        su = [v / E6 for v in col("spotUsdc", t)]
        sbk = col("spotBuck", t)
        sb = [(sbk[i] * bu[i] / 1e12) if bu[i] else float("nan")
              for i in range(len(fr))]
        ax.plot(days, ref, color="black", linestyle="--", linewidth=1.4,
                label="market reference (CSV)")
        ax.plot(days, su, color="tab:red", linewidth=1.2,
                label=f"{names[t]}/USDC pool (direct)")
        ax.plot(days, sb, color="tab:purple", linewidth=1.2,
                label=f"{names[t]}/BUCK -> USD via BUCK/USDC (indirect)")
        ax.set_ylabel(f"{names[t]}  USD")
        ax.grid(True, alpha=0.3)

        # Pool balance axes: TOKEN side uses each constituent's own decimals;
        # BUCK side is always 6-decimal BUCK.
        bal_tok = [f["poolBal"][t][0] / (10 ** dec[t]) for f in fr]
        bal_buck = [f["poolBal"][t][1] / E6 for f in fr]
        ax2 = ax.twinx()
        ax2.plot(days, bal_tok, color="tab:orange", linewidth=1.0,
                 linestyle="--", label=f"{names[t]} in TOKEN/BUCK pool")
        ax2.set_ylabel(f"{names[t]} bal", color="tab:orange")
        ax2.tick_params(axis="y", labelcolor="tab:orange")
        ax3 = ax.twinx()
        ax3.spines.right.set_position(("axes", 1.12))
        ax3.plot(days, bal_buck, color="tab:green", linewidth=1.0,
                 linestyle=":", label="BUCK in TOKEN/BUCK pool")
        ax3.set_ylabel("BUCK bal", color="tab:green")
        ax3.tick_params(axis="y", labelcolor="tab:green")

        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        lines3, labels3 = ax3.get_legend_handles_labels()
        ax.legend(lines1 + lines2 + lines3, labels1 + labels2 + labels3,
                  loc="upper left", fontsize=7)
        if t == 0:
            ax.set_title("Direct vs indirect price tracking "
                         "(BUCK-unaware arbs + basket rebalancer)")

    # ---- Panel 4: pool value-weight deviations ----------------------- #
    ax = axes[3]
    colors_w = ["tab:orange", "tab:blue", "tab:green"]
    handles = []
    for t in range(3):
        aw = [f.get("poolWeights", [[0, 0]] * 3)[t][0] for f in fr]
        tw = [f.get("poolWeights", [[0, 0]] * 3)[t][1] for f in fr]
        l1, = ax.plot(days, aw, color=colors_w[t], linewidth=1.4,
                      label=f"{names[t]} actual")
        l2, = ax.plot(days, tw, color=colors_w[t], linewidth=1.0,
                      linestyle="--",
                      label=f"{names[t]} target")
        handles.extend([l1, l2])
    ax.axhline(1.0 / 3, color="black", alpha=0.15, linewidth=0.5)
    ax.set_ylabel("value weight")
    ax.legend(handles=handles, loc="upper left", fontsize=7, ncol=2)
    ax.grid(True, alpha=0.3)
    ax.set_title("TOKEN/BUCK pool value weights: actual vs basket target")

    # ---- Panel 5: treasury compounding (NAV, outstanding, treasury) -- #
    ax = axes[4]
    handles5 = []
    nav = [f.get("basketNav", 0) / E6 for f in fr]
    l1, = ax.plot(days, nav, color="tab:blue", linewidth=1.5,
                  label="basket NAV (total LP BUCK value)")
    handles5.append(l1)
    out = [f.get("dmOutstanding", 0) / E6 for f in fr]
    l2, = ax.plot(days, out, color="tab:orange", linewidth=1.2,
                  linestyle="--",
                  label="DM outstanding (BUCK principal)")
    handles5.append(l2)
    tb = [treasury_buck(f) / E6 for f in fr]
    l3, = ax.plot(days, tb, color="tab:green", linewidth=1.4,
                  label="treasury BUCK (retained profit)")
    handles5.append(l3)
    ax.set_ylabel("BUCK")
    ax.set_xlabel("Day")
    ax.grid(True, alpha=0.3)

    ax2 = ax.twinx()
    ts = [f.get("treasuryShare", 0) * 100 for f in fr]
    l4, = ax2.plot(days, ts, color="tab:red", linewidth=1.0,
                   linestyle=":", label="treasury share (%)")
    handles5.append(l4)
    ax2.set_ylabel("treasury share (%)", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")

    ax.legend(handles=handles5, loc="upper left", fontsize=7)
    ax.set_title("Basket NAV, outstanding, treasury BUCK & share")

    # ---- Panel 6: BUCK_K controller ---------------------------------- #
    #
    # Does the value-balancing machinery actually reach its setpoint in this
    # regime?  The controller is
    #
    #     error     = 1.0 - basketValueInBuck          (setpoint is 1.0 BUCK)
    #     rawOutput = 1.0 + P*Kp + I*Ki + D*Kd         (all /UNIT)
    #     buckK     = clamp(rawOutput, buckKMin, buckKMax)
    #
    # so the pane needs the process variable, the lever, and which PID term
    # is moving it.  P is plotted on the left axis as a fraction because it
    # *is* the error -- P == 1 - basketValueInBuck exactly -- so it belongs on
    # the same dimensionless scale as the quantity it is derived from, where
    # the reader can see it close (or fail to close) the gap to the setpoint.
    #
    # I and D go on the right axis in their own units: I accumulates
    # ppm-seconds and is what winds a railed controller deep into its clamp,
    # D is ppm/second.  Once buckK sits on a rail the lever tells you nothing
    # more, and only the integral shows how far past the rail the controller
    # has wound -- i.e. how long a reversal would take to unwind.
    ax = axes[5]
    handles6 = []
    basket_val = [f.get("basketVal", 0) / 1e18 for f in fr]
    k = [f.get("buckK", 0) / 1e18 for f in fr]
    # P is stored in ppm of fractional error; show it as a fraction.
    p_frac = [f.get("pid_p", 0) / 1e6 for f in fr]

    l1, = ax.plot(days, basket_val, color="tab:blue", linewidth=1.5,
                  label="basketValueInBuck (process variable)")
    handles6.append(l1)
    l2, = ax.plot(days, k, color="tab:green", linewidth=1.8,
                  label="buckK (lever)")
    handles6.append(l2)
    l3, = ax.plot(days, p_frac, color="tab:purple", linewidth=1.0,
                  linestyle="--", label="P = error = 1 - basketValue")
    handles6.append(l3)
    # What the BuckDiscountBasketAgent actually trades against: BUCK's price
    # on the floating pool.  basketValueInBuck is BUCK against the commodity
    # basket; this is BUCK against the numeraire, and the gap between them is
    # where the demand leg finds its edge.
    l3b, = ax.plot(days, [f.get("buckUsd", 0) / 1e6 for f in fr],
                   color="tab:cyan", linewidth=1.1,
                   label="BUCK/USD (floating pool)")
    handles6.append(l3b)
    # The demand leg's actual fills, marked at the price they traded on.
    # BuckDiscountBasketAgent buys when BUCK/USD sits below par by DISCOUNT_BP
    # and unwinds above par by PREMIUM_BP, so the markers should straddle the
    # 1.0 line -- if they cluster on one side the leg is one-directional and
    # is not closing round-trips.
    ent = [f.get("dbb_bought", 0) + f.get("dba_bought", 0) for f in fr]
    sold = [f.get("dbb_sold", 0) + f.get("dba_sold", 0) for f in fr]
    bu_par = [f.get("buckUsd", 0) / 1e6 for f in fr]
    buy_d = [days[i] for i in range(1, len(fr)) if ent[i] > ent[i - 1]]
    buy_p = [bu_par[i] for i in range(1, len(fr)) if ent[i] > ent[i - 1]]
    sell_d = [days[i] for i in range(1, len(fr)) if sold[i] > sold[i - 1]]
    sell_p = [bu_par[i] for i in range(1, len(fr)) if sold[i] > sold[i - 1]]
    if buy_d:
        handles6.append(ax.scatter(
            buy_d, buy_p, marker="^", s=22, color="tab:olive", zorder=5,
            label=f"demand leg buys ({len(buy_d)} days, "
                  f"{ent[-1] / E6:,.0f} BUCK)"))
    if sell_d:
        handles6.append(ax.scatter(
            sell_d, sell_p, marker="v", s=22, color="tab:pink", zorder=5,
            label=f"demand leg sells ({len(sell_d)} days, "
                  f"{sold[-1] / E6:,.0f} BUCK)"))
    ax.axhline(1.0, color="black", alpha=0.35, linewidth=0.9, linestyle="-.")
    ax.axhline(0.0, color="black", alpha=0.2, linewidth=0.8)
    ax.annotate("setpoint 1.0", xy=(0.005, 1.0), xycoords=("axes fraction", "data"),
                fontsize=6, color="black", alpha=0.6, va="bottom")
    ax.set_ylabel("BUCK_K / basketValueInBuck / error")
    ax.grid(True, alpha=0.3)

    ax2 = ax.twinx()
    i_term = [f.get("pid_i", 0) for f in fr]
    d_term = [f.get("pid_d", 0) for f in fr]
    l4, = ax2.plot(days, i_term, color="tab:red", linewidth=1.2,
                   linestyle=":", label="I (integral, ppm*s)")
    handles6.append(l4)
    if any(v != 0 for v in d_term):
        l5, = ax2.plot(days, d_term, color="tab:brown", linewidth=1.0,
                       linestyle=":", label="D (derivative, ppm/s)")
        handles6.append(l5)
    else:
        # A flat-zero D is itself a finding: no derivative action is firing.
        handles6.append(Line2D([], [], color="tab:brown", linestyle=":",
                               label="D = 0 throughout (no derivative action)"))
    ax2.set_ylabel("PID integral (ppm*s)", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")

    ax.legend(handles=handles6, loc="upper left", fontsize=7)
    railed = sum(1 for v in k if v <= 0.0)
    suffix = (f"  --  buckK railed at 0 for {railed}/{len(k)} days"
              if railed else "")
    ax.set_title("BUCK_K controller: setpoint tracking and PID state" + suffix)

    # ---- Panel 7: what BUCK_K can actually reach, and director duty --- #
    #
    # Read directly under the controller pane: it shows why K moves the way
    # it does.  BUCK enters circulation by two routes with very different
    # relationships to the controller.
    #
    #   dmOutstanding  BuckBasket direct-mint, backed by deposited TOKEN.
    #                  Buck.mintFromBasket never consults creditLimit, so
    #                  BUCK_K has no lever on this at all.
    #   residual       supply - dmOutstanding: the credit-backed remainder,
    #                  the only part creditLimit -- and therefore BUCK_K --
    #                  gates.
    #
    # If the residual is flat while supply grows, the controller is pushing
    # on a channel that is not carrying the growth, and no amount of K
    # movement will close the error.
    ax = axes[6]
    handles7 = []
    supply = [f.get("supply", 0) / E6 for f in fr]
    dmo = [f.get("dmOutstanding", 0) / E6 for f in fr]
    residual = [s - o for s, o in zip(supply, dmo)]

    l1, = ax.plot(days, supply, color="tab:blue", linewidth=1.6,
                  label="BUCK totalSupply")
    handles7.append(l1)
    l2, = ax.plot(days, dmo, color="tab:orange", linewidth=1.3, linestyle="--",
                  label="basket direct-mint (BUCK_K has no lever)")
    handles7.append(l2)
    l3, = ax.plot(days, residual, color="tab:green", linewidth=1.5,
                  label="credit-backed residual (the only part BUCK_K gates)")
    handles7.append(l3)
    ax.set_ylabel("BUCK")
    ax.grid(True, alpha=0.3)

    ax2 = ax.twinx()
    pokes = [f.get("directorPokes", 0) for f in fr]
    dtrades = [f.get("directorTrades", 0) for f in fr]
    l4, = ax2.plot(days, pokes, color="tab:red", linewidth=1.0, linestyle=":",
                   label="director pokes (cumulative)")
    handles7.append(l4)
    l5, = ax2.plot(days, dtrades, color="tab:purple", linewidth=1.2,
                   label="director trades (cumulative)")
    handles7.append(l5)
    ax2.set_ylabel("director pokes / trades", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")

    duty = (100.0 * dtrades[-1] / pokes[-1]) if pokes and pokes[-1] else 0.0
    ax.legend(handles=handles7, loc="upper left", fontsize=7)
    ax.set_title("BUCK supply by backing, and rebalance-director duty cycle"
                 f"  --  director acted on {dtrades[-1]}/{pokes[-1]} pokes "
                 f"({duty:.1f}%)")

    # ---- Panel 8: BuckBasket return on capital-at-risk --------------- #
    #
    # Three series that ARE returns, and one that only looks like one.
    ax = axes[7]

    # A return needs a profit and the capital-time that earned it.
    #
    # `dmProfitUsd` / `dmDollarDays` is the sound pair: profit is booked only
    # when a deposit is actually REDEEMED, comparing proceeds against what was
    # deposited, and dollar-days count the capital that was deployed while it
    # was deployed.  Neither depends on how an agent's idle wealth is valued,
    # which is what made the old figure meaningless.
    #
    # `directMintPnl` is a mark, not a profit: _agent_value now counts USDC,
    # but the DM agents mint the USDC they spend inside `_buy_token_from_usdc`
    # and never hold a balance, so the value is still conjured at the purchase
    # site.  The tell survives: it tracks the NUMBER of deposits at a
    # near-constant fraction of each, while deployed capital is flat.  Kept
    # greyed, on its own axis, so the artifact stays visible without being
    # mistaken for a result.
    dollar_days = [f.get("dmDollarDays", 0) for f in fr]

    # An APR is a ratio whose denominator starts at nearly nothing.  On day 3
    # a few hundred dollar-days of capital turn a single lucky round trip into
    # four figures of "APR", and one such point sets the y-scale for the whole
    # two-year picture.  The early value is not a small annual rate, it is not
    # an annual rate at all -- so it is withheld rather than drawn, and the
    # curve begins where the denominator can carry it.  WARMUP_DAYS covers the
    # elapsed-time divisor; the dollar-day series additionally has to reach a
    # visible fraction of the capital it eventually deploys.
    WARMUP_DAYS = 45
    WARMUP_FRAC = 0.02
    dd_floor = WARMUP_FRAC * (dollar_days[-1] or 0)
    NA = float("nan")

    def pct(v):
        """Render a possibly-withheld rate; a run shorter than the warmup has
        no annual rate to report, and should say so rather than print nan."""
        return f"{v:.2f}%" if v == v else "n/a"

    def apr(series):
        """Annualize on deployed capital-time, blank until it means something."""
        return [100.0 * 365.0 * v / dd
                if (dd > 0 and dd >= dd_floor and dy >= WARMUP_DAYS) else NA
                for v, dd, dy in zip(series, dollar_days, days)]

    realized_apr = apr([f.get("dmProfitUsd", 0) for f in fr])
    treasury_apr = apr([treasury_buck(f) for f in fr])

    ax.axhline(0, color="black", alpha=0.25, linewidth=0.8)
    l1, = ax.plot(days, realized_apr, color="tab:blue", linewidth=1.8,
                  label="realized holder APR (redeemed round-trips / dollar-days)")
    l2, = ax.plot(days, treasury_apr, color="tab:green", linewidth=1.4,
                  label="treasury APR (retained basket profit / dollar-days)")
    ax.set_ylabel(f"APR (%)  [from day {WARMUP_DAYS}]")
    ax.set_xlabel("Day")
    ax.grid(True, alpha=0.3)

    # The demand leg's own book, split by variant, on the same axis because
    # these are the same KIND of number and need no caveat: each arb is
    # seeded with a FINITE USDC endowment and mints neither USDC nor BUCK,
    # so `arb_state` (cash + BUCK held at par, against endowment) is a real
    # mark.  Their capital is available from day 0, so elapsed days are the
    # capital-time.
    #
    # The two lines are the point of the pair.  A holder sits on loose BUCK
    # and pays demurrage; a basketeer parks it as TOKEN in the basket and
    # pays none while earning the rebalancing premium.  Same hurdle model,
    # opposite carry.
    def arb_apr(cls):
        """Mark an arb's book against its endowment.

        Two corrections matter here, and the first cut got both wrong:

        * BUCK is marked at the FLOATING price, not at par.  BUCK traded
          between 0.95 and 1.43 USDC over this run, so par understates a long
          position by up to 43% and reads it as a loss on entry.
        * `parked` is the BUCK principal in open BuckBasket receipts -- the
          capital a basketeer has actually put to work.  Omitting it books a
          loss the moment the agent deposits, which is the same mistake
          `directMintPnl` makes.  Older vectors carry only a receipt COUNT;
          their mark is short by the open positions, so it is drawn dashed
          rather than presented as complete.
        """
        out = []
        for f, dy in zip(fr, days):
            rows = [a for a in f.get("arb2", []) if a.get("cls") == cls]
            endow = sum(a.get("endow", 0) for a in rows)
            if not endow or dy < WARMUP_DAYS:
                out.append(NA)
                continue
            px = (f.get("buckUsd", 0) or E6) / E6      # USDC per BUCK
            buck = sum(a.get("held", 0) + a.get("parked", 0) for a in rows)
            val = sum(a.get("cash", 0) for a in rows) + buck * px
            out.append(100.0 * 365.0 * (val - endow) / (endow * dy))
        return out

    def arb_complete(cls):
        """True when every open position in the mark carries a value."""
        rows = [a for a in fr[-1].get("arb2", []) if a.get("cls") == cls]
        return bool(rows) and all("parked" in a for a in rows)

    basketeer = arb_apr("dbb")
    holder = arb_apr("dba")
    _bk_ok = arb_complete("dbb")
    l0, = ax.plot(days, basketeer, color="tab:olive", linewidth=1.6,
                  linestyle="-" if _bk_ok else "--",
                  label="demand leg: BASKETEER APR (parks in the basket)"
                        + ("" if _bk_ok else " -- open positions UNPRICED"))
    l0b, = ax.plot(days, holder, color="tab:brown", linewidth=1.2,
                   linestyle="-.",
                   label="demand leg: HOLDER APR (sits on BUCK, pays demurrage)")

    ax2 = ax.twinx()
    dm_apr = apr([f.get("directMintPnl", 0) for f in fr])
    l3, = ax2.plot(days, dm_apr, color="tab:gray", linewidth=1.0, linestyle="--",
                   label="directMintPnl APR -- ARTIFACT, not a return")
    ax2.set_ylabel("artifact APR (%)", color="tab:gray")
    ax2.tick_params(axis="y", labelcolor="tab:gray")

    # Scale to the series themselves rather than to whatever survived the
    # warmup filter first: a single outlier that clears the filter should not
    # be able to flatten everything else either.
    finite = [v for ser in (realized_apr, treasury_apr, basketeer, holder)
              for v in ser if v == v]
    if finite:
        finite.sort()
        lo = finite[int(0.01 * (len(finite) - 1))]
        hi = finite[int(0.99 * (len(finite) - 1))]
        pad = max(1.0, 0.15 * (hi - lo))
        ax.set_ylim(min(lo - pad, -pad), hi + pad)

    rt = fr[-1].get("dmRoundTrips", 0)
    ax.legend(handles=[l1, l2, l0, l0b, l3], loc="upper left", fontsize=7)
    ax.set_title("BuckBasket return on capital-at-risk  --  depositors "
                 f"{pct(realized_apr[-1])} APR over {rt} round-trips, "
                 f"treasury {pct(treasury_apr[-1])} APR, demand leg "
                 f"{pct(basketeer[-1])} basketeer / {pct(holder[-1])} holder")

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    # Convergence summary.
    print(f"\nWrote {_rel(OUT)}  ({len(days)} days)")
    for t in range(3):
        ref = col("refUsd", t)[-1] / E6
        su = col("spotUsdc", t)[-1] / E6
        sb = (col("spotBuck", t)[-1] * (bu[-1] / 1e6) / E6
              if bu[-1] else float("nan"))
        print(f"  {names[t]:5s}  ref ${ref:,.2f}  "
              f"USDC-pool ${su:,.2f} ({100*(su-ref)/ref:+.2f}%)  "
              f"BUCK-pool->USD ${sb:,.2f} ({100*(sb-ref)/ref:+.2f}%)")
    last = fr[-1]
    dm_entries = last.get("dmEntries", 0)
    dm_exits = last.get("dmExits", 0)
    nav_final = last.get("basketNav", 0) / E6
    out_final = last.get("dmOutstanding", 0) / E6
    tb_final = treasury_buck(last) / E6
    ts_final = last.get("treasuryShare", 0) * 100
    invested = last.get("dmTotalInvested", 0)
    dm_roi_final = (
        100 * last.get("directMintPnl", 0) / invested if invested else 0)
    treasury_roi_final = (
        100 * treasury_buck(last) / invested if invested else 0)
    print(f"  direct trades: {last.get('directTrades',0)}  "
          f"BUCK-routed: {last.get('cycleTrades',0)}  "
          f"rebalance: {last.get('rebalanceTrades',0)}")
    print(f"  Treasury BUCK: {tb_final:,.2f}  "
          f"share of NAV: {ts_final:.2f}%  "
          f"treasury ROI: {treasury_roi_final:.2f}%")
    print(f"  realized depositor return: {pct(realized_apr[-1])} APR "
          f"(${last.get('dmProfitUsd', 0) / E6:,.0f} booked over "
          f"{last.get('dmRoundTrips', 0)} redeemed round-trips)")
    print(f"  treasury return: {pct(treasury_apr[-1])} APR")
    print(f"  ... both on ${dollar_days[-1] / E6:,.0f} dollar-days "
          f"of capital-at-risk")
    print(f"  [directMintPnl {dm_roi_final:.2f}% of gross deposits / "
          f"{pct(dm_apr[-1])} APR -- ARTIFACT: the DM agents mint the USDC "
          f"they spend, so value is still conjured at the purchase site]")
    print(f"  demand leg BASKETEER: {last.get('dbb_bought',0)/E6:,.0f} BUCK "
          f"bought / {last.get('dbb_sold',0)/E6:,.0f} sold, "
          f"{last.get('dbb_parked',0)/E6:,.0f} parked, "
          f"{last.get('dbb_harvests',0)} harvests  ({pct(basketeer[-1])} APR)")
    print(f"  demand leg HOLDER:    {last.get('dba_bought',0)/E6:,.0f} BUCK "
          f"bought / {last.get('dba_sold',0)/E6:,.0f} sold"
          f"  ({pct(holder[-1])} APR)")
    if last.get("dbb_err"):
        print(f"  demand-leg error: {last['dbb_err']}")
    print(f"  direct-mint entries: {dm_entries}  exits: {dm_exits}")
    print(f"  basket NAV: {nav_final:,.2f} BUCK  "
          f"outstanding: {out_final:,.2f} BUCK  "
          f"treasury share: {ts_final:.2f}%")
    rp_final = last.get("rebalancerPnl", 0) / E6
    dp_final = last.get("directMintPnl", 0) / E6
    print(f"  rebalancer P&L: ${rp_final:,.0f}  "
          f"direct-mint P&L: ${dp_final:,.0f}")
    pw = last.get("poolWeights", [[0, 0]] * 3)
    for t in range(3):
        aw, tw = pw[t]
        dev = (aw - tw) * 100
        print(f"  {names[t]:5s}  weight actual={aw:.4f} target={tw:.4f}  "
              f"deviation {dev:+.2f}%")


if __name__ == "__main__":
    test_rebalancing_sim_plot()
