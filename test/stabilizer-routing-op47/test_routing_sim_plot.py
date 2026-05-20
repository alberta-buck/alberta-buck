"""Render the routing-sim result to images/routing-sim.png.

Workflow:
  1.  forge test --match-contract RoutingSimTest
        # writes test/vectors/routing-sim.json
  2.  python -m pytest test/stabilizer-routing-op47/test_routing_sim_plot.py -v -s
        # reads JSON, writes images/routing-sim.png

JSON schema (parallel per-frame, N=3 tokens [PAXG, cbBTC, AOIL]):
  frames[i] = { day, refUsd[3], spotUsdc[3], spotBuck[3],
                basketVal, buckK, supply,
                directTrades, cycleTrades, aggPnl }

Units: refUsd/spotUsdc are USDC micro-dollars / token (1e6 == $1.00).
BUCK is NOT pegged -- it floats.  spotBuck is raw 6-dec BUCK / token; its
USD value is obtained *indirectly* through the live floating BUCK/USDC
pool: USD/token = spotBuck * (buckUsd / 1e6) / 1e6, where buckUsd is
USDC-micro per BUCK.  The headline result: even though BUCK floats
freely, the TOKEN/BUCK pool valued through the instantaneous BUCK/USDC
price tracks the same market reference as the direct TOKEN/USDC pool --
driven only by BUCK-unaware routed arbitrage.
"""

import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DATA = REPO / "test" / "vectors" / "routing-sim.json"
OUT = REPO / "images" / "routing-sim.png"

E6 = 10 ** 6
E18 = 10 ** 18


def _i(v):
    return int(v)


@pytest.mark.skipif(
    not DATA.exists(),
    reason="routing-sim.json not generated yet; run: "
           "forge test --match-contract RoutingSimTest",
)
def test_routing_sim_plot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = json.loads(DATA.read_text())
    names = d["tokens"]
    dec = d.get("decimals", [18, 8, 18])
    fr = d["frames"]
    days = [f["day"] for f in fr]

    def col(key, t):
        return [f[key][t] for f in fr]

    fig, axes = plt.subplots(5, 1, figsize=(13, 18), sharex=True)
    colors = ["tab:orange", "tab:blue", "tab:green"]

    # buckUsd = USDC-micro per 1 BUCK from the *live* floating BUCK/USDC
    # pool (it floats; BUCK is NOT assumed ~$1).
    bu = [f.get("buckUsd", 0) for f in fr]

    # ---- Panels 1-3: per token, both pools vs market reference -------- #
    for t in range(3):
        ax = axes[t]
        ref = [v / E6 for v in col("refUsd", t)]
        su = [v / E6 for v in col("spotUsdc", t)]
        # Indirect USD price of the TOKEN/BUCK pool, valued through the
        # instantaneous BUCK/USDC pool price:
        #   USD/token = (raw BUCK/token) * (USD/BUCK)
        #             = spotBuck * (buckUsd/1e6) / 1e6
        sbk = col("spotBuck", t)
        sb = [(sbk[i] * bu[i] / 1e12) if bu[i] else float("nan")
              for i in range(len(fr))]
        ax.plot(days, ref, color="black", linestyle="--", linewidth=1.4,
                label="market reference (CSV)")
        ax.plot(days, su, color="tab:red", linewidth=1.2,
                label=f"{names[t]}/USDC pool (direct)")
        ax.plot(days, sb, color="tab:purple", linewidth=1.2,
                label=f"{names[t]}/BUCK -> USD via live BUCK/USDC (indirect)")
        ax.set_ylabel(f"{names[t]}  USD")
        ax.grid(True, alpha=0.3)
        if t == 0:
            ax.set_title("Direct (TOKEN/USDC) vs indirect (TOKEN/BUCK valued "
                         "through the floating BUCK/USDC pool) vs CSV")

        # ---- right axes: TOKEN/BUCK pool balances -------------------- #
        bal_tok = [f["poolBal"][t][0] / (10 ** dec[t]) for f in fr]
        bal_buck = [f["poolBal"][t][1] / E6 for f in fr]

        ax2 = ax.twinx()
        l_tok = ax2.plot(days, bal_tok, color="tab:orange", linewidth=1.0,
                         linestyle="--",
                         label=f"{names[t]} in {names[t]}/BUCK pool")
        ax2.set_ylabel(f"{names[t]} balance", color="tab:orange")
        ax2.tick_params(axis="y", labelcolor="tab:orange")

        ax3 = ax.twinx()
        ax3.spines.right.set_position(("axes", 1.12))
        l_buck = ax3.plot(days, bal_buck, color="tab:green", linewidth=1.0,
                          linestyle=":",
                          label=f"BUCK in {names[t]}/BUCK pool")
        ax3.set_ylabel("BUCK balance", color="tab:green")
        ax3.tick_params(axis="y", labelcolor="tab:green")

        # Combine legends from all three axes.
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        lines3, labels3 = ax3.get_legend_handles_labels()
        ax.legend(lines1 + lines2 + lines3, labels1 + labels2 + labels3,
                  loc="upper left", fontsize=7)

    # ---- Panel 4: cumulative route usage + floating BUCK price ------- #
    ax = axes[3]
    direct = [f["directTrades"] for f in fr]
    cycle = [f["cycleTrades"] for f in fr]
    ub = [f.get("ubTrades", 0) for f in fr]
    l1, = ax.plot(days, direct, color="tab:red", linewidth=1.4,
                  label="market-maker snaps (TOKEN/USDC)")
    l2, = ax.plot(days, cycle, color="tab:purple", linewidth=1.4,
                  label="BUCK-routed trades (total)")
    l3, = ax.plot(days, ub, color="tab:brown", linewidth=1.4, linestyle="--",
                  label="trades via the floating BUCK/USDC pool")
    ax.set_ylabel("cumulative trades")
    ax.grid(True, alpha=0.3)
    ax.set_title("Route usage + the floating (uncontrolled) BUCK/USDC pool "
                 "price")

    # BUCK per US$ on the right axis (buckUsd = USDC-micro per 1 BUCK;
    # BUCK per $ = 1e6 / buckUsd).  The pool floats on supply/demand --
    # it is never controlled; this is just the gauge-breaking liquidity.
    bpd = [(1_000_000 / v if v else float("nan"))
           for v in (f.get("buckUsd", 0) for f in fr)]
    axr = ax.twinx()
    l4, = axr.plot(days, bpd, color="tab:cyan", linewidth=1.2,
                   label="BUCK per US$ (floating)")
    axr.set_ylabel("BUCK per US$", color="tab:cyan")
    axr.tick_params(axis="y", labelcolor="tab:cyan")
    ax.legend(handles=[l1, l2, l3, l4], loc="upper left", fontsize=8)

    # ---- Panel 5: controller + annualized returns (APR) -------------- #
    # Absolute PnL is uninformative without the capital base, so show
    # annualized return on the capital actually deployed:
    #   arb APR     = realized PnL / arb capital  (USDC seeds, day-0)
    #   LP APR (g)  = cumulative fees / capital deployed in that pool group
    # (LP profit is extracted from the V3 positions: uncollected fees via
    #  feeGrowth + tokensOwed -- see snapshot._lp_groups.)
    ax = axes[4]
    bk = [_i(f["buckK"]) / E18 for f in fr]
    bv = [_i(f["basketVal"]) / E18 for f in fr]
    l1, = ax.plot(days, bk, color="tab:green", linewidth=1.5, label="buckK")
    l2, = ax.plot(days, bv, color="tab:gray", linewidth=1.2, linestyle="--",
                  label="basketValueInBuck")
    ax.axhline(1.0, color="black", alpha=0.2, linewidth=0.5)
    ax.set_ylabel("BUCK (18d)")
    ax.set_xlabel("Day")
    ax.grid(True, alpha=0.3)

    def apr(series_roi):
        # annualize; suppress the noisy first few days (small denominator)
        return [100 * r * 365 / d if d >= 5 else float("nan")
                for r, d in zip(series_roi, days)]

    inv = [f.get("invested", 0) or 1 for f in fr]
    arb_roi = [f["aggPnl"] / iv for f, iv in zip(fr, inv)]

    def lp_roi(group):
        out = []
        for f in fr:
            fee, cap = f.get("lp", {}).get(group, [0, 0])
            out.append(fee / cap if cap else 0.0)
        return out

    ax2 = ax.twinx()
    handles = [l1, l2]
    handles.append(ax2.plot(days, apr(arb_roi), color="tab:cyan",
                            linewidth=1.4, label="arb agents APR")[0])
    handles.append(ax2.plot(days, apr(lp_roi("buck")), color="tab:purple",
                            linewidth=1.2,
                            label="direct-mint TOKEN/BUCK LP APR")[0])
    handles.append(ax2.plot(days, apr(lp_roi("ub")), color="tab:brown",
                            linewidth=1.2, label="BUCK/USDC LP APR")[0])
    handles.append(ax2.plot(days, apr(lp_roi("usdc")), color="tab:olive",
                            linewidth=1.0, linestyle=":",
                            label="TOKEN/USDC LP APR")[0])
    ax2.set_ylabel("APR (%)", color="tab:cyan")
    ax2.tick_params(axis="y", labelcolor="tab:cyan")
    ax.legend(handles=handles, loc="upper left", fontsize=8, ncol=2)
    ax.set_title("Controller + annualized return on deployed capital "
                 "(arb agents & LP positions)")

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=120)
    plt.close(fig)

    # convergence summary
    print(f"\nWrote {OUT.relative_to(REPO)}  ({len(days)} days)")
    for t in range(3):
        ref = col("refUsd", t)[-1] / E6
        su = col("spotUsdc", t)[-1] / E6
        sb = col("spotBuck", t)[-1] * (bu[-1] / 1e6) / E6 if bu[-1] else float("nan")
        print(f"  {names[t]:5s}  ref ${ref:,.2f}  "
              f"USDC-pool ${su:,.2f} ({100*(su-ref)/ref:+.2f}%)  "
              f"BUCK-pool->USD ${sb:,.2f} ({100*(sb-ref)/ref:+.2f}%)")
    print(f"  direct trades: {fr[-1]['directTrades']}  "
          f"BUCK-routed trades: {fr[-1]['cycleTrades']}")
    last, dN = fr[-1], days[-1] or 1
    iv = last.get("invested", 0) or 1
    r = last["aggPnl"] / iv
    print(f"  arb agents:        capital ${iv/E6:,.0f}  "
          f"PnL ${last['aggPnl']/E6:,.0f}  ROI {100*r:+.2f}%  "
          f"APR {100*r*365/dN:+.1f}%")
    for g, lbl in (("buck", "TOKEN/BUCK direct-mint"),
                   ("ub", "BUCK/USDC outsider   "),
                   ("usdc", "TOKEN/USDC truth     ")):
        fee, cap = last.get("lp", {}).get(g, [0, 0])
        rr = fee / cap if cap else 0.0
        print(f"  {lbl} LP: capital ${cap/E6:,.0f}  fees ${fee/E6:,.0f}  "
              f"ROI {100*rr:+.3f}%  APR {100*rr*365/dN:+.2f}%")
