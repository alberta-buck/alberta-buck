"""Basket-first evidence, from saved frames and actual receipt swaps."""
import json
import os
from pathlib import Path
import tempfile

from .cli import atomic_json
from .pair_basket_render import metrics
from .interactive import render as interactive
from .event_view import price_markers


def render(root):
    root=Path(root);m=json.loads((root/"manifest.json").read_text())
    runs=[json.loads((root/e["file"]).read_text()) for e in m["entries"].values()]
    rows=[]
    for r in runs:
        control=next(c for c in runs if c["profile"]==r["profile"] and c["basket_mode"]=="legacy")
        assert r["config"]==control["config"] and r["bootstrap"]==control["bootstrap"]
        assert [f["target_usd"] for f in r["frames"]]==[f["target_usd"] for f in control["frames"]]
        s=metrics(r);s["wrong_side_pair_legs"]=0;s["paired_legs"]=0
        s["missing_responses"]=[]
        s["material_pair_candidates"]=0; s["detected_material_pair_candidates"]=0
        s["confirmed_closure_legs"]=0; s["braking_release_legs"]=0
        for f in r["frames"]:
            traces=f.get("pair_decisions",[])
            if r["basket_mode"] == "pair":
                assert len(traces)<=1, f"Duplicate response trace at day {f['day']}"
                if not traces:
                    check=f.get("pair_response_check")
                    assert check is not None, f"Unexplained missing response trace at day {f['day']}"
                    s["missing_responses"].append(dict(day=f["day"],**check))
            if not traces or traces[0]["proposed"]["fallbackRequired"]: continue
            assert len(traces)==1
            proposal=traces[0]["proposed"]; d=traces[0]["input"]
            gaps=[int(v)-int(t) for v,t in zip(d["values"],d["targets"])];n=len(gaps)
            for i in range(n):
                for j in range(n):
                    if gaps[i]>0 and gaps[j]<0 and max(gaps[i],-gaps[j])>int(d["band"]):
                        s["material_pair_candidates"]+=1
                        s["detected_material_pair_candidates"]+=int(d["desire"][i*n+j])>0
            planned=[j for j,v in enumerate(proposal["recipients"]) if int(v)]
            if not planned: continue
            i=int(proposal["donor"])
            assert sum(map(int,proposal["recipients"]))==int(proposal["total"])<=gaps[i]
            for j in planned:
                assert gaps[j]<0 and int(proposal["recipients"][j])<=-gaps[j]
                assert max(gaps[i],-gaps[j])>int(d["band"]) and int(d["desire"][i*n+j])>0
                assert int(d["readyMask"])==3
                if int(d["high"][i*n+j])<0: s["confirmed_closure_legs"]+=1
                else: s["braking_release_legs"]+=1
                price=f["before_policy"]["token_buck"];base=r["config"]["feed"]["base"]
                s["paired_legs"]+=1
                s["wrong_side_pair_legs"]+=price[i]/base[i]<=price[j]/base[j]
            events=[e for e in f["events"] if e["actor"]=="basket"]
            buys={t["token"] for e in events for t in e["trades"] if t["side"]=="buy"}
            sells={t["token"] for e in events for t in e["trades"] if t["side"]=="sell"}
            assert set(planned)==buys and sells=={i},"Plan did not become the intended swaps"
        mature=[p for p in s["periods"] if p["start"]>=30]
        s["mature_both_sides_each_token_each_cycle"]=all(all(p["token_buys"]) and all(p["token_sells"]) for p in mature)
        if m["market_stage"]=="basket":
            assert not any(r["flags"].get(k) for k in ("k","desk","demand","supply"))
            assert all(f["k"]==r["frames"][0]["k"] for f in r["frames"])
            assert r["summary"]["desk_ops"]==0 and r["summary"]["demand_trades"]==0 and r["summary"]["supply_trades"]==0
        rows.append(s)
    atomic_json(root/"comparison.json",dict(stage=m["market_stage"],summaries=rows,paired_inputs=True))
    warm=m["requested_config"]["bootstrap"].get("basket_warmup_days",0)
    band=m["requested_config"]["basket"].get("weight_band_bp",200)
    detail=f" Allocation trigger band: {band/100:g} percentage points. Common mean-price LP prelude: {warm} days, excluded from the 120 live days."
    if detail not in m["method"]:
        m["method"]+=detail; atomic_json(root/"manifest.json",m)
    interactive(root)
    report=["#+title: Basket before monetary and persistent private actors","#+options: toc:2 num:nil",
        "#+html_head: <style>body{font-family:system-ui;line-height:1.5}#content{max-width:1150px;margin:auto;padding:1rem}img{max-width:100%;height:auto}</style>","",
        "[[file:index.html][Interactive plots]]", "", m["method"],"",
        "| Profile | Policy | Pair transfers | Transfers per 30-day cycle | Wrong-side pair legs | Both sides of every TOKEN in each mature cycle | Allocation error, pp | End marked equity, USD | End lien, BUCK |",
        "|---------+--------+----------------+----------------------------+----------------------+-----------------------------------------------+----------------------+------------------------+----------------|" ]
    for s in sorted(rows,key=lambda s:(s["profile"],s["mode"])):
        report.append(f"| {s['profile']} | {s['mode']} | {s['pair_transfers']} | {', '.join(str(p['pair_transfers']) for p in s['periods'])} | {s['wrong_side_pair_legs']} | {s['mature_both_sides_each_token_each_cycle']} | {s['mean_weight_error_pp']:.3f} | {s['equity_usd']:,.2f} | {s['lien_buck']:,.2f} |")
    report += ["", "Wrong-side counts apply to graded paired transfers: the sold TOKEN's pre-policy normalized BUCK-pool price must exceed its recipient's. Legacy risk/bootstrap work is separate. Marked equity includes inventory and leverage; it is not liquidation proceeds or realized net ROI. Pool fees affect outcomes; caller gas is not subtracted. Source fingerprints and all pre-response books remain in JSON.", ""]
    report += ["* Trade conditions and signal strength", "",
        "Every planned pair leg is checked against its exact pre-response book: both allocations improve, at least one exceeds its band, capacities are not exceeded, both quote routes are ready, intent is positive, and the intended swaps appear in receipts. The following counts distinguish conservative braking participation from an error envelope wholly on the closing side.", "",
        "| Profile | Policy | Material allocation pair-samples | Of those, positive intent | Executed legs: braking | Executed legs: confirmed closure |",
        "|---------+--------+----------------------------------+---------------------------+------------------------+-----------------------------------|"]
    for s in rows:
        if s["mode"]=="pair":
            report.append(f"| {s['profile']} | {s['mode']} | {s['material_pair_candidates']} | {s['detected_material_pair_candidates']} | {s['braking_release_legs']} | {s['confirmed_closure_legs']} |")
    report += ["", "A material allocation pair-sample alone is not a price opportunity. Positive intent may still be below the minimum executable amount or lose to another donor's larger bounded request. Counts are samples/legs, not independent profitable round trips.", ""]
    if warm:
        opening=runs[0]["bootstrap"]["basket_prelude"]
        report += ["* Common opening book", "",
            f"The same production LP placement ran at known mean prices for {warm} days in both arms, before enabling the live signals. K and private actors were idle. Opening weights: {', '.join(str(x/100)+'%' for x in opening['weights'])}; equity {opening['equity_buck']:,.2f} BUCK; account {opening['account_buck']:,.2f} BUCK. Raw inventory and liquidity quantities are in each JSON bootstrap.basket_prelude. This is a settled-book experiment, separate from the retained cold-start runs.", ""]
    for s in rows:
        if s["missing_responses"]:
            report += [f"* Missing response traces: {s['profile']} / {s['mode']}", "",
                "The wheel catches component reverts. Each missing trace records a diagnostic replay and its method in JSON; a replay does not substitute for a successful execution trace. The current probe temporarily authorizes an EOA wheel inside a discarded EVM snapshot, avoiding pyrevm's contract-caller rejection.", ""]
            for c in s["missing_responses"]:
                report.append(f"- Day {c['day']}: {c['error'] or ('replay work '+str(c['work']))}")
            report.append("")
    os.environ.setdefault("MPLCONFIGDIR",str(Path(tempfile.gettempdir())/"alberta-buck-matplotlib"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    for profile in m["axes"][0]["values"]:
        selected=sorted([r for r in runs if r["profile"]["name"]==profile["id"]],key=lambda r:r["basket_mode"])
        fig,axes=plt.subplots(6,2,figsize=(14,15),sharex=True,constrained_layout=True)
        for col,r in enumerate(selected):
            frames=r["frames"];days=[f["day"] for f in frames];marks=price_markers(r,native=True)
            for i in range(3):
                ax=axes[i,col];ax.plot(days,[f["token_buck"][i]/r["config"]["feed"]["base"][i] for f in frames],color="#526975",lw=1)
                for g in marks:
                    if g["id"] not in ("basket-buy","basket-sell"):continue
                    pts=[p for p in g["points"] if p["token"]==i]
                    ax.scatter([days[p["index"]] for p in pts],[p["value"] for p in pts],color=g["color"],marker="^" if g["shape"]=="up" else "v",s=14)
                ax.set_title(f"{r['basket_mode']} · TOKEN {'ABC'[i]}",fontsize=10);ax.set_ylabel("BUCK price / baseline")
            for i,color in enumerate(("#246da4","#bf6430","#548948")):
                axes[3,col].plot(days,[f["basket"]["weights"][i]/100 for f in frames],color=color,label="ABC"[i])
            band=r["config"]["basket"].get("weight_band_bp",200)/100
            for y in (100/3-band,100/3,100/3+band):axes[3,col].axhline(y,color="#aaa",ls=":")
            axes[3,col].set_ylabel("Allocation, %");axes[3,col].legend(ncol=3,fontsize=7)
            axes[4,col].plot(days,[f["basket"]["equity"]*f["buck_usdc"]/1e6 for f in frames],color="#158067");axes[4,col].set_ylabel("Marked equity, M USD")
            axes[5,col].plot(days,[max(0,-f["basket"]["account"])/1e6 for f in frames],color="#b56c2f");axes[5,col].set_ylabel("Lien, M BUCK");axes[5,col].set_xlabel("Days")
            for ax in axes[:,col]:ax.grid(alpha=.15)
        for row in axes:
            lo=min(ax.get_ylim()[0] for ax in row);hi=max(ax.get_ylim()[1] for ax in row)
            for ax in row:ax.set_ylim(lo,hi)
        name=profile["id"]+"-comparison"
        fig.suptitle(profile["id"]+" · executed buys ▲ and sells ▼")
        fig.savefig(root/(name+".png"),dpi=120);fig.savefig(root/(name+".svg"));plt.close(fig)
        report += ["* "+profile["id"],"",f"[[file:{name}.png]]",""]
    (root/"results.org").write_text("\n".join(report))
    print(json.dumps(rows,indent=2))
