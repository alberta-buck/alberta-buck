"""Matched 120-day basket/arb ownership experiments with passive noise diagnostics."""
import argparse
import copy
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
import multiprocessing
from pathlib import Path

from .block_pairs import PROFILES, simulate_observed
from .cli import atomic_json, fingerprint
from .model import STAGES, dependencies, digest, feed_rows, load_config

ARMS = {
    "maintenance-external": (False, "external", "01 · Maintenance; external arb"),
    "rebalance-external": (True, "external", "02 · Rebalancing; external arb"),
    "maintenance-owned": (False, "basket", "03 · Maintenance; basket-owned arb"),
    "rebalance-owned": (True, "basket", "04 · Rebalancing and basket-owned arb"),
}
HISTORICAL = dict(name="historical-bridge", history=dict(
    files=["hist-paxg.csv", "hist-food.csv", "hist-nrgc.csv"],
    labels=["Gold (PAXG)", "Food (FOOD)", "Energy (NRGC)"], start_day=1706))


def worker(cfg, profile, stage, arm, key, code, path):
    active, owner, _ = ARMS[arm]
    result = simulate_observed(cfg, profile, stage, basket_mode="pair", maintenance_only=not active)
    result.update(cache_key=key, implementation=code, experiment_arm=arm,
                  discretionary_rebalancing=active, arb_beneficiary=owner, sensor_details=True)
    atomic_json(path,result)
    return dict(profile=profile["name"], stage=arm, cache_key=key,
                file=Path(path).name, summary=result["summary"])


def attribution(runs):
    """Factorial effects include market feedback; they are not fill cost bases."""
    assert set(runs) == set(ARMS)
    base = runs["maintenance-external"]
    initial = base["config"]["market"]["basket_equity_usd"]
    reference_cfg = copy.deepcopy(base["config"])
    reference_cfg["market"].pop("arb_beneficiary", None)
    for r in runs.values():
        cfg = copy.deepcopy(r["config"]);cfg["market"].pop("arb_beneficiary", None)
        assert cfg == reference_cfg and r["profile"] == base["profile"]
        assert [f["target_usd"] for f in r["frames"]] == [f["target_usd"] for f in base["frames"]]
    frames = []
    for index, f in enumerate(base["frames"]):
        row = dict(day=f["day"])
        for currency in ("buck","usd"):
            v = {}
            for arm,r in runs.items():
                frame = r["frames"][index]
                assert frame["day"] == f["day"]
                v[arm] = frame["basket"]["equity"]*(frame["buck_usdc"] if currency=="usd" else 1)
            b = v["maintenance-external"]
            row[currency] = dict(baseline=b-initial,
                rebalancing=v["rebalance-external"]-b,
                arb_ownership=v["maintenance-owned"]-b,
                interaction=v["rebalance-owned"]-v["rebalance-external"]-v["maintenance-owned"]+b,
                total=v["rebalance-owned"]-initial)
            assert abs(sum(x for k,x in row[currency].items() if k!="total")-row[currency]["total"]) < 1e-6
        frames.append(row)
    return dict(initial=initial, frames=frames,
        method="Matched effects of enabling rebalancing and crediting arb income, including feedback on prices, "
               "LP placement and debt. Baseline + rebalancing + arb ownership + interaction = combined gain. "
               "These are counterfactual system effects, not realized profits from individual rebalancing fills.")


def render(root):
    from .interactive import basket_performance, render as interactive
    from .noise import PARAMS, shadow_frames
    root=Path(root);m=json.loads((root/"manifest.json").read_text())
    runs={key:json.loads((root/e["file"]).read_text()) for key,e in m["entries"].items()}
    m["comparisons"]={};m["diagnostics"]={}
    (root/"diagnostics").mkdir(exist_ok=True)
    shadow_source=hashlib.sha256(Path(__file__).with_name("noise.py").read_bytes()).hexdigest()
    for key,r in runs.items():
        name=f"diagnostics/{key[:16]}.json"
        atomic_json(root/name,dict(params=PARAMS, source_sha256=shadow_source, frames=shadow_frames(r)))
        m["diagnostics"][key]=name
    comparisons={}
    for profile in m["axes"][0]["values"]:
        p=profile["id"]
        selected={r["experiment_arm"]:r for r in runs.values() if r["profile"]["name"]==p}
        comparisons[p]=attribution(selected)
        name=f"{p}-attribution.json";atomic_json(root/name,comparisons[p])
        key=m["cases"]["rebalance-owned"][f"{p}:0:medium:medium"]
        m["comparisons"][key]=name
    atomic_json(root/"manifest.json",m)
    interactive(root)
    report=["#+title: Basket returns, arbitrage income and shadow price estimators",
        "#+options: toc:2 num:nil", "", "[[file:index.html][Interactive comparison]]", "",m["method"],"",
        "* Net returns and actual arb income", "",
        "Arb credited to the basket is already included in net equity. Do not add it a second time. Net credits measure the signed BUCK account plus accrued relief immediately before/after each arb transaction; settlement of existing relief is not new income. USD credits use the receipt-time exchange rate. Remaining portfolio gain includes inventory marks, LP fees, relief and rebalancing. Caller gas and liquidation costs are excluded.","",
        "| Profile | Arm | Net BUCK | Return BUCK % | Net USD | Return USD % | Arb credited BUCK | Arrival fee BUCK |",
        "|---------+-----+----------+---------------+---------+--------------+-------------------+------------------|"]
    for r in sorted(runs.values(),key=lambda r:(r["profile"]["name"],r["experiment_arm"])):
        p=basket_performance(r);income=r["frames"][-1]["basket"]["arb_income"]
        report.append(f"| {r['profile']['name']} | {r['experiment_arm']} | {p['final_net_buck']:,.2f} | {p['return_pct']:+.3f} | {p['final_net_usd']:,.2f} | {p['return_usd_pct']:+.3f} | {income['net_buck']:,.2f} | {income['arrival_fees_buck']:,.2f} |")
    report += ["", "* Separate the effects of rebalancing and arb ownership", "",
        next(iter(comparisons.values()))["method"], "",
        "| Profile | Currency | Maintenance baseline gain | Rebalancing effect | Arb ownership effect | Interaction | Combined gain |",
        "|---------+----------+---------------------------+--------------------+----------------------+-------------+---------------|"]
    for profile,c in comparisons.items():
        for currency in ("buck","usd"):
            f=c["frames"][-1][currency]
            report.append("| "+" | ".join([profile,currency.upper()]+[f"{f[k]:+,.2f}" for k in ("baseline","rebalancing","arb_ownership","interaction","total")])+" |")
    report += ["", "* Estimator comparison", "",
        "Python diagnostics read the same aligned cumulative-tick intervals as the Solidity observer. No estimator has execution authority. Both routes must be available and warmed up. The simple second-difference proxy assumes locally linear motion and white measurement noise; the route-disagreement proxy misses noise shared by both venues. The small Kalman model retains two independent relative coordinates, their velocities and full within-route covariance. Its route envelope does not assume independent evidence from BUCK and USDC. Process variance is fixed; measurement covariance adapts from prior roughness, after the current sample's update. Innovations and uncertainty are displayed, not advertised as calibrated confidence.", "",
        "* Historical scenario", "",
        "TOKEN A/B/C use the committed gold/food/energy quote files. These contain historical anchors with generated daily texture between monthly observations. They are not three independent observed intraday feeds. The last 120-day window is normalized by strictly preceding historical means; a linear log-price endpoint correction makes each TOKEN end where it began. This is a constructed closed-path scenario, not an unmodified historical backtest or a zero-time-average guarantee. Its opening prices can differ from the historical means; the maintenance control captures that initial repricing. Raw unadjusted normalized prices, source hashes, window indices and the removed log changes are saved in JSON. The estimator receives none of the future endpoint information.", ""]
    (root/"results.org").write_text("\n".join(report))
    print(f"Rendered {len(runs)} EVM runs with causal shadow diagnostics -> {root/'index.html'}")


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config",default="doc/control-lab.toml")
    ap.add_argument("--out",default="doc/control-lab/basket-research/current")
    ap.add_argument("--profiles",default="three-phase,noise20,noise80,flat-noise80,historical-bridge")
    ap.add_argument("--stage",choices=("basket","k","all"),default="basket")
    ap.add_argument("--jobs",type=int,default=4)
    ap.add_argument("--days",type=int,default=120)
    ap.add_argument("--render-only",action="store_true")
    args=ap.parse_args(argv);root=Path(args.out);root.mkdir(parents=True,exist_ok=True)
    if args.render_only:return render(root)
    if args.jobs<1:ap.error("jobs must be positive")
    if args.days<1:ap.error("days must be positive")
    from alberta_buck.sim.chain import load_artifact
    cfg=load_config(args.config);cfg["days"]=args.days;cfg["basket"]["tilt"]=0;cfg["feed"]["walk_amplification"]=0
    cfg["bootstrap"]["basket_warmup_days"]=120
    available={p["name"]:p for p in [*PROFILES,HISTORICAL]}
    names=args.profiles.split(",")
    if len(set(names))!=len(names) or any(n not in available for n in names):ap.error("unknown or duplicate profile")
    profiles=[available[n] for n in names]
    code={**fingerprint(),"basket_research":digest(dict(source=Path(__file__).read_text(),
        control=load_artifact("PairBasketMaintenanceControl")))}
    m=dict(schema=1,kind="basket-research",status="running",requested_config=cfg,implementation=code,
        market_stage=args.stage,entries={},cases={arm:{} for arm in ARMS},
        stages={arm:{**STAGES[args.stage],"title":values[2]} for arm,values in ARMS.items()},
        unique_runs=4*len(profiles),visible_cases=4*len(profiles),
        axes=[dict(id="profile",label="Price signal",values=[dict(id=p["name"],label=p["name"]) for p in profiles]),
              dict(id="walk",label="Random walk",values=[dict(id="0",label="0×",value=0)]),
              *[dict(id=n,label=n.title(),requires="demand" if n=="buyer" else "supply",
                 values=[dict(id="medium",label="Medium",value=5000)]) for n in ("buyer","seller")]],
        method=f"{args.days}-day matched real-EVM runs: independently enable discretionary paired rebalancing and basket ownership of arb receipts. All arms retain LP maintenance, safety overrides and the same market-linking arb rules. Credited income enters the basket's real BUCK account and may repay debt or fund later LP placement. Stage: {args.stage}. Same 120-day mean-price prelude, then selected opening prices. Shadow estimators are causal Python diagnostics only.",
        explanations={arm:f"{values[2]}. Compare the same profile in all four arms. Safety and maintenance remain active." for arm,values in ARMS.items()})
    pending=[]
    for p in profiles:
        for arm,(_,owner,_) in ARMS.items():
            selected=copy.deepcopy(cfg);selected["market"]["arb_beneficiary"]=owner
            if p["name"]=="flat-noise80":selected["feed"]["amplitude"]=0
            feed_rows(selected,p)
            key=digest(dict(inputs=dependencies(selected,p,args.stage),arm=arm,implementation=code))
            path=root/f"{p['name']}-{arm}-{key[:16]}.json"
            m["cases"][arm][f"{p['name']}:0:medium:medium"]=key
            if path.exists():
                r=json.loads(path.read_text())
                assert r["cache_key"]==key and len(r["frames"])==args.days*cfg["ticks_per_day"]+1
                m["entries"][key]=dict(profile=p["name"],stage=arm,cache_key=key,file=path.name,summary=r["summary"])
            else:pending.append((selected,p,args.stage,arm,key,code,str(path)))
    atomic_json(root/"progress.json",m)
    with ProcessPoolExecutor(max_workers=args.jobs,mp_context=multiprocessing.get_context("spawn")) as pool:
        tasks=[pool.submit(worker,*job) for job in pending]
        for done in as_completed(tasks):
            e=done.result();m["entries"][e["cache_key"]]=e
            atomic_json(root/"progress.json",m)
            print(e["profile"],e["stage"],e["summary"]["seconds"],flush=True)
    m["status"]="complete";atomic_json(root/"manifest.json",m)
    render(root)


if __name__=="__main__":main()
