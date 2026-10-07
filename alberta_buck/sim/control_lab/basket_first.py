"""Small matched basket campaigns, before monetary and private actors."""
import argparse
import copy
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path

from .block_pairs import PARAMS, PROFILES, run_case
from .cli import atomic_json, fingerprint
from .model import STAGES, digest, load_config


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config",default="doc/control-lab.toml")
    ap.add_argument("--out",default="doc/control-lab/basket-first/00-isolated")
    ap.add_argument("--profiles",default="three-phase")
    ap.add_argument("--stage",choices=("basket","k","all"),default="basket")
    ap.add_argument("--jobs",type=int,default=2)
    ap.add_argument("--seed",type=int)
    ap.add_argument("--render-only",action="store_true")
    args=ap.parse_args(argv); root=Path(args.out);root.mkdir(parents=True,exist_ok=True)
    if args.jobs<1: ap.error("jobs must be positive")
    if args.render_only: return render(root)
    from alberta_buck.sim.chain import load_artifact
    cfg=load_config(args.config);cfg["basket"]["tilt"]=0;cfg["feed"]["walk_amplification"]=0
    if args.seed is not None: cfg["seed"]=args.seed
    names=args.profiles.split(","); profiles=[p for p in PROFILES if p["name"] in names]
    if len(profiles)!=len(names): ap.error("unknown or duplicate profile")
    code={**fingerprint(),"basket_first":digest(dict(params=PARAMS,
        artifacts=[load_artifact(n) for n in ("BlockPairSignals","PairRebalancePolicy","PairBasketDecisionTrace")],
        sources=[Path(__file__).read_text(),Path(__file__).with_name("block_pairs.py").read_text()]))}
    modes={"legacy":"Legacy EMA basket","pair":"TOKEN-pair basket"}
    m=dict(schema=1,kind="basket-first",status="complete",requested_config=cfg,implementation=code,
        market_stage=args.stage,entries={},cases={mode:{} for mode in modes},
        stages={mode:{**STAGES[args.stage],"title":title} for mode,title in modes.items()},
        unique_runs=2*len(profiles),visible_cases=2*len(profiles),
        axes=[dict(id="profile",label="Price signal",values=[dict(id=p["name"],label=p["name"]) for p in profiles]),
              dict(id="walk",label="Random walk",values=[dict(id="0",label="0×",value=0)]),
              *[dict(id=n,label=n.title(),requires=n=="buyer" and "demand" or "supply",
                values=[dict(id="medium",label="Medium",value=5000)]) for n in ("buyer","seller")]],
        method=f"120-day real-EVM comparison at stage {args.stage}. Declared target weights, identical initial capital and price paths. The basket stage keeps K fixed and has no desk or persistent buyer/seller. Pair decisions are recorded immediately before execution by a test-only facet. Saved JSON drives every graph.",
        explanations={"legacy":"Existing EMA response with target lean disabled.",
            "pair":"Current TOKEN-pair response. Inspect prices, actual swaps, allocation, debt and the pre-response decisions in JSON."})
    with ProcessPoolExecutor(max_workers=args.jobs,mp_context=multiprocessing.get_context("spawn")) as pool:
        futures=[]
        for p in profiles:
            selected=copy.deepcopy(cfg)
            if p["name"]=="flat-noise80": selected["feed"]["amplitude"]=0
            for mode in modes:
                key=digest(dict(config=selected,profile=p,mode=mode,stage=args.stage,implementation=code))
                path=root/f"{p['name']}-{mode}-{key[:16]}.json"
                m["cases"][mode][f"{p['name']}:0:medium:medium"]=key
                if path.exists():
                    r=json.loads(path.read_text())
                    m["entries"][key]=dict(profile=p["name"],stage=mode,cache_key=key,file=path.name,summary=r["summary"])
                else: futures.append((mode,pool.submit(run_case,selected,p,args.stage,key,code,str(path),True,mode,mode=="pair")))
        for mode,f in futures:
            e=f.result();e["stage"]=mode;m["entries"][e["cache_key"]]=e
            print(e["profile"],mode,e["summary"]["seconds"],flush=True)
    atomic_json(root/"manifest.json",m);render(root)


def render(root):
    from .basket_first_render import render as render_saved
    return render_saved(root)


if __name__=="__main__": main()
