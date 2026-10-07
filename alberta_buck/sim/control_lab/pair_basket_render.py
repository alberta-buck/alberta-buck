"""Matched basket outcomes and execution plots, from cached JSON."""
import json
import os
from pathlib import Path
import tempfile

from .cli import atomic_json
from .events import execution_counts
from .event_view import price_markers
from .interactive import render as render_interactive


def metrics(r):
    frames = r["frames"]; end = frames[-1]
    active = [f for f in frames if f["day"] >= 10]
    # Reference quantities are the contract's actual 3333/3333/3334 weights.
    values = [int(q) * p for q, p in zip(r["bootstrap"]["reference_amounts"], r["config"]["feed"]["base"])]
    targets = [10000 * v / sum(values) for v in values]
    mean_gap = sum(sum(abs(w-t) for w,t in zip(f["basket"]["weights"], targets))/len(targets)/100
                   for f in active) / max(1, len(active))
    plans = [p for f in frames for p in f.get("pair_policy", {}).get("plans", [])]
    for p in plans:
        assert p["bought"] <= min(p["planned"], p["proceeds"])
    periods = []
    for start in range(0, r["config"]["days"], 30):
        window = [f for f in frames if start < f["day"] <= start + 30]
        trades = [t for f in window for e in f["events"] if e["actor"] == "basket" for t in e["trades"]]
        periods.append(dict(start=start, end=min(start+30, r["config"]["days"]),
            pair_transfers=sum(len(f.get("pair_policy", {}).get("plans", [])) for f in window),
            token_buys=[sum(t["token"]==i and t["side"]=="buy" for t in trades) for i in range(3)],
            token_sells=[sum(t["token"]==i and t["side"]=="sell" for t in trades) for i in range(3)]))
    return dict(profile=r["profile"]["name"], mode=r["basket_mode"], counts=execution_counts(frames),
        pair_transfers=len(plans), after_day30=execution_counts([f for f in frames if f["day"] > 30]),
        periods=periods, mean_weight_error_pp=mean_gap,
        equity_buck=end["basket"]["equity"], equity_usd=end["basket"]["equity"]*end["buck_usdc"],
        lien_buck=max(0,-end["basket"]["account"]), mean_abs_bvib=r["summary"]["mean_abs_parity_error"],
        end_k=end["k"], total_gas=r["summary"]["gas"], desk_capital=end["desk"]["capital"],
        seller_headroom=end["agents"]["supply_headroom"])


def render(root):
    root=Path(root); m=json.loads((root/"manifest.json").read_text())
    runs=[json.loads((root/e["file"]).read_text()) for e in m["entries"].values()]
    rows=[]
    for r in runs:
        control=next(c for c in runs if c["profile"]==r["profile"] and c["basket_mode"]=="legacy")
        assert r["config"]==control["config"] and r["bootstrap"]==control["bootstrap"]
        assert [f["target_usd"] for f in r["frames"]]==[f["target_usd"] for f in control["frames"]]
        rows.append(metrics(r))
    lookup = {(s["profile"], s["mode"]):s for s in rows}
    clean, old = lookup["three-phase", "pair"], lookup["three-phase", "legacy"]
    flat, old_flat = lookup["flat-noise80", "pair"], lookup["flat-noise80", "legacy"]
    checks = dict(clean_both_sides_each_token_each_30_day_window=all(
        all(p["token_buys"]) and all(p["token_sells"]) for p in clean["periods"]),
        noisy_flat_mean_allocation_error_not_worse=flat["mean_weight_error_pp"] <= old_flat["mean_weight_error_pp"])
    atomic_json(root/"comparison.json", dict(paired_inputs=True, summaries=rows, checks=checks,
        decision="basket-review-required", arb_and_desk_changed=False))
    render_interactive(root)
    os.environ.setdefault("MPLCONFIGDIR",str(Path(tempfile.gettempdir())/"alberta-buck-matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    report=["#+title: Graded TOKEN-pair basket: matched execution experiment", "#+options: toc:2 num:nil",
      "#+html_head: <style>body{font-family:system-ui;line-height:1.5}#content{max-width:1150px;margin:auto;padding:1rem}img{max-width:100%;height:auto}</style>","",
      "[[file:index.html][Interactive JavaScript comparison]] · [[file:../pair-opportunities/results.org][Exact opportunity-to-execution audit]]", "",
      "Both arms use declared weights (target lean is zero), the same allocation band, initial books, price feed and agents. The desk, K and arb implementations are unchanged. The pair arm adds graded departure/closure intent and records its own immediate price impact as uncertainty. Hard liquidity, exposure and credit overrides retain the existing response.","",
      "* Decision: review the basket before extending execution", "",
      f"The clean arm executes {clean['pair_transfers']} paired transfers versus the legacy arm's {old['counts']['basket_buys']} buys and {old['counts']['basket_sells']} sells. Mean absolute bvib error changes from {old['mean_abs_bvib']*10000:.2f} to {clean['mean_abs_bvib']*10000:.2f} bp. This does not establish repeated harvest: the new arm's paired transfers in successive 30-day windows are {', '.join(str(p['pair_transfers']) for p in clean['periods'])}.", "",
      f"In noisy flat prices the new arm executes {flat['pair_transfers']} paired transfers, while mean allocation error changes from {old_flat['mean_weight_error_pp']:.2f} to {flat['mean_weight_error_pp']:.2f} percentage points. That is an adverse result even though some terminal equity marks improve. Noise, asymmetric LP placement and allocation gating remain confounded; these data do not establish that the observer alone produced false turns.", "",
      "The two review checks are visible in comparison.json: both trade directions for each TOKEN in each 30-day window, and noisy-flat allocation error no worse than the matched legacy arm. Neither is a profitability certificate. Arb and desk changes are deferred while the basket result is re-evaluated.", "",
      "* Matched outcomes", "",
      "| Profile | Response | TOKEN buys / sells | Paired transfers | Swaps after day 30 | Mean allocation error, pp | End equity, USD | End lien, BUCK | Mean abs bvib error | End K |",
      "|---------+----------+--------------------+------------------+-------------------+---------------------------+-----------------+----------------+---------------------+-------|"]
    for s in sorted(rows,key=lambda r:(r["profile"],r["mode"])):
        c=s["counts"]; late=s["after_day30"]
        report.append(f"| {s['profile']} | {s['mode']} | {c['basket_buys']} / {c['basket_sells']} | {s['pair_transfers']} | {late['basket_buys']+late['basket_sells']} | {s['mean_weight_error_pp']:.3f} | {s['equity_usd']:,.0f} | {s['lien_buck']:,.0f} | {s['mean_abs_bvib']:.3%} | {s['end_k']:.4f} |")
    report += ["", "Equity is a terminal on-chain mark converted at BUCK/USDC, including LP inventory. It is not liquidation proceeds or realized ROI. Gas is reported in comparison.json but not subtracted from equity. Endogenous private flows and leverage must be read alongside any change in the equity mark.", "",
      "| Profile | Incremental run gas, pair minus legacy | Change in end lien, BUCK |",
      "|---------+---------------------------------------+--------------------------|"]
    for profile in m["axes"][0]["values"]:
        a,b = lookup[profile["id"],"pair"], lookup[profile["id"],"legacy"]
        report.append(f"| {profile['id']} | {a['total_gas']-b['total_gas']:,} | {a['lien_buck']-b['lien_buck']:+,.0f} |")
    report += ["", "Gas covers the simulated run's receipts, including the observer and endogenous actor/arb work; deployment is separately recorded as bootstrap_gas in raw JSON. No gas price or dollar gas valuation is assumed.", "",
      "* Corrected mechanics and remaining constraint", "",
      "The first basket version reused the bookkeeping timestamp for its elapsed trade budget. Frequent mark-only work repeatedly discarded that budget. It also blocked ordinary LP placement while waiting for a pair transfer. The retained baseline-v1-manifest.json records that failed version. Its higher idle-inventory mark was not evidence of harvesting.", "",
      "The corrected implementation consumes elapsed budget only after an executed paired transfer, and permits legacy LP placement when no pair transfer is ready. Both legs are atomic: a refused recipient rolls back the donor and every preceding recipient. Discretionary paired transfers spend no more than actual donor proceeds, although independent LP placement can still draw on the basket's credit.", "",
      "The 2 percentage point allocation band applies independently to donor and recipient. Each released leg must also exceed 100 ppm of the gross book, with at most one day of elapsed catch-up. Full-range LP holdings have roughly half the TOKEN price sensitivity of idle TOKEN, so the original 12% price oscillation can leave only a small gap beyond the band. A visible closure signal therefore need not yield an executable request. Post-action rates are shown in the interactive panes; they are not a ledger of rejected pre-trade plans.", "",
      "* Next experiments", "",
      "- [ ] Freeze the opening LP/idle composition and credit utilization in a separate comparison, so bootstrap placement cannot mask the turn rule.",
      "- [ ] Record pre-trade candidate gaps, release sizes and the exact binding cap; distinguish no counterpart, minimum-size suppression, LP work and execution refusal.",
      "- [ ] Test band/minimum-size compatibility and elapsed participation as an explicit design choice; do not tune only for the clean trace.",
      "- [ ] Implement and isolate standalone cash divestment/accumulation when no counter-TOKEN qualifies. The current experiment tests paired transfers only.",
      "- [ ] Separate noisy cessation evidence from deliberate stationary rebalancing; measure churn and book recovery in noisy-flat prices.",
      "- [ ] Measure repeat-cycle value after execution fees, liquidation and caller costs before promoting the response.",
      "- [ ] Then compare arb shock capture and outer-band desk behavior in separate matched campaigns.", ""]
    for profile in m["axes"][0]["values"]:
        pair=sorted([r for r in runs if r["profile"]["name"]==profile["id"]],key=lambda r:r["basket_mode"])
        fig,axes=plt.subplots(7,2,figsize=(14,17),sharex=True,constrained_layout=True)
        for col,r in enumerate(pair):
            f=r["frames"]; days=[x["day"] for x in f]
            markers=price_markers(r,native=True)
            for i in range(3):
                ax=axes[i,col]
                ax.plot(days,[x["token_buck"][i]/r["config"]["feed"]["base"][i] for x in f],color="#526975",lw=1)
                for g in markers:
                    if g["id"] not in ("basket-buy","basket-sell"): continue
                    pts=[p for p in g["points"] if p["token"]==i]
                    ax.scatter([days[p["index"]] for p in pts],[p["value"] for p in pts],color=g["color"],marker="^" if g["shape"]=="up" else "v",s=12)
                ax.set_title(f"{r['basket_mode']} · TOKEN {'ABC'[i]}",fontsize=10)
                ax.set_ylabel("BUCK price / baseline")
            for i,color in enumerate(("#246da4", "#bf6430", "#548948")):
                axes[3,col].plot(days,[x["basket"]["weights"][i]/100 for x in f],color=color,label="ABC"[i])
            axes[3,col].axhline(100/3-2,color="#999",ls=":"); axes[3,col].axhline(100/3+2,color="#999",ls=":")
            axes[3,col].set_ylabel("Book allocation, %"); axes[3,col].legend(ncol=3,fontsize=7)
            axes[4,col].plot(days,[x["basket"]["equity"]*x["buck_usdc"]/1e6 for x in f],color="#158067")
            axes[4,col].set_ylabel("Marked equity, M USD")
            axes[5,col].plot(days,[max(0,-x["basket"]["account"])/1e6 for x in f],color="#b56c2f")
            axes[5,col].set_ylabel("Lien, M BUCK")
            axes[6,col].plot(days,[x["bvib"] for x in f],color="#7653ab")
            axes[6,col].axhline(1,color="#999",ls=":"); axes[6,col].set_ylabel("bvib"); axes[6,col].set_xlabel("Elapsed days")
            for ax in axes[:,col]: ax.grid(alpha=.15)
        # Matching axes expose magnitude differences rather than autoscaling them away.
        for row in axes:
            lo=min(ax.get_ylim()[0] for ax in row); hi=max(ax.get_ylim()[1] for ax in row)
            for ax in row: ax.set_ylim(lo,hi)
        name=profile["id"]+"-comparison"
        fig.suptitle(profile["id"]+" · executed basket swaps (up buys, down sells)")
        fig.savefig(root/(name+".png"),dpi=120); fig.savefig(root/(name+".svg")); plt.close(fig)
        report += ["* "+profile["id"],"",f"[[file:{name}.png]]",""]
    (root/"results.org").write_text("\n".join(report))
    print(json.dumps(rows,indent=2))
