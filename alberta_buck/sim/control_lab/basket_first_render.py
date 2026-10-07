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
        for f in r["frames"]:
            traces=f.get("pair_decisions",[])
            if r["basket_mode"] == "pair":
                assert len(traces)==1, f"Missing or duplicate response trace at day {f['day']}"
            if not traces or traces[0]["proposed"]["fallbackRequired"]: continue
            assert len(traces)==1
            proposal=traces[0]["proposed"]
            planned=[j for j,v in enumerate(proposal["recipients"]) if int(v)]
            if not planned: continue
            i=int(proposal["donor"])
            for j in planned:
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
    interactive(root)
    report=["#+title: Basket before monetary and persistent private actors","#+options: toc:2 num:nil",
        "#+html_head: <style>body{font-family:system-ui;line-height:1.5}#content{max-width:1150px;margin:auto;padding:1rem}img{max-width:100%;height:auto}</style>","",
        "[[file:index.html][Interactive plots]]", "", m["method"],"",
        "| Profile | Policy | Pair transfers | Transfers per 30-day cycle | Wrong-side pair legs | Both sides of every TOKEN in each mature cycle | Allocation error, pp | End marked equity, USD | End lien, BUCK |",
        "|---------+--------+----------------+----------------------------+----------------------+-----------------------------------------------+----------------------+------------------------+----------------|" ]
    for s in sorted(rows,key=lambda s:(s["profile"],s["mode"])):
        report.append(f"| {s['profile']} | {s['mode']} | {s['pair_transfers']} | {', '.join(str(p['pair_transfers']) for p in s['periods'])} | {s['wrong_side_pair_legs']} | {s['mature_both_sides_each_token_each_cycle']} | {s['mean_weight_error_pp']:.3f} | {s['equity_usd']:,.2f} | {s['lien_buck']:,.2f} |")
    report += ["", "Wrong-side counts apply to graded paired transfers: the sold TOKEN's pre-policy normalized BUCK-pool price must exceed its recipient's. Legacy risk/bootstrap work is separate. Marked equity includes inventory and leverage; it is not liquidation proceeds or realized net ROI. Pool fees affect outcomes; caller gas is not subtracted. Source fingerprints and all pre-response books remain in JSON.", ""]
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
            for y in (100/3-2,100/3,100/3+2):axes[3,col].axhline(y,color="#aaa",ls=":")
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
