"""Paired legacy/graded basket executions with the same declared targets."""
import argparse
import copy
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import multiprocessing
from pathlib import Path

from .block_pairs import PARAMS, PROFILES, run_case
from .cli import atomic_json, fingerprint
from .model import STAGES, digest, load_config, validate


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="doc/control-lab.toml")
    ap.add_argument("--out", default="doc/control-lab/pair-basket")
    ap.add_argument("--days", type=int)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--render-only", action="store_true")
    args = ap.parse_args(argv)
    if args.jobs < 1: ap.error("--jobs must be positive")
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    if args.render_only:
        from .pair_basket_render import render
        return render(out)
    from alberta_buck.sim.chain import load_artifact
    cfg = load_config(args.config)
    if args.days is not None: cfg["days"] = args.days
    cfg["feed"]["walk_amplification"] = 0
    cfg["basket"]["tilt"] = 0
    validate(cfg)
    code = fingerprint()
    code["pair_basket"] = digest(dict(observer=load_artifact("BlockPairSignals"),
        policy=load_artifact("PairRebalancePolicy"), response=load_artifact("PairBasketEquityResponse"),
        sources=[Path(__file__).read_text(), Path(__file__).with_name("block_pairs.py").read_text()], params=PARAMS))
    modes = {"legacy": "Legacy quorum · declared targets", "pair": "Graded TOKEN-pair response"}
    m = dict(schema=1, kind="pair-basket", status="running", requested_config=cfg,
        implementation=code, entries={}, stages={k: {**STAGES["basket"], "title":v} for k,v in modes.items()},
        cases={k:{} for k in modes}, unique_runs=8, visible_cases=8,
        axes=[dict(id="profile", label="Price signal", values=[dict(id=p["name"], label=p["name"]) for p in PROFILES]),
              dict(id="walk", label="Random walk", values=[dict(id="0", label="0×", value=0)]),
              *[dict(id=n, label=n.title(), requires=f, values=[dict(id="medium", label="Medium", value=5000)])
                for n,f in (("buyer","demand"),("seller","supply"))]],
        method="Eight real pyrevm worlds: four identical price paths with two basket responses. Target lean is zero in both arms, so declared weights define allocation gaps. The arb, desk, K and private agents are unchanged. The new observer runs before basket actions. Own basket impact widens the policy's uncertainty for two days. Static and interactive figures are rendered from saved JSON.",
        explanations={"legacy":"Existing EMA quorum response, with target lean disabled for this matched experiment. All agents and the desk are active.",
          "pair":"A graded pair response transfers one donor's released value to eligible underweights in deficit proportions. It retains legacy liquidity, exposure and credit overrides. A refused planned recipient reverts the entire transfer. No new credit draw is added to discretionary paired transfers."})
    tasks=[]
    for p in PROFILES:
        selected=copy.deepcopy(cfg)
        if p["name"]=="flat-noise80": selected["feed"]["amplitude"]=0
        for mode in modes:
            key=digest(dict(config=selected,profile=p,mode=mode,implementation=code))
            filename=f"{p['name']}-{mode}-{key[:16]}.json"
            m["cases"][mode][f"{p['name']}:0:medium:medium"]=key
            tasks.append((selected,p,"basket",key,code,str(out/filename),True,mode))
    with ProcessPoolExecutor(max_workers=args.jobs,mp_context=multiprocessing.get_context("spawn")) as pool:
        futures={}
        for task in tasks:
            if Path(task[5]).exists():
                r=json.loads(Path(task[5]).read_text())
                m["entries"][task[3]]=dict(profile=task[1]["name"],stage=task[7],cache_key=task[3],file=Path(task[5]).name,summary=r["summary"])
            else: futures[pool.submit(run_case,*task)]=task[7]
        for f in as_completed(futures):
            e=f.result(); e["stage"]=futures[f]; m["entries"][e["cache_key"]]=e
            atomic_json(out/"progress.json",m)
            print(f"{e['profile']}/{e['stage']}: {e['summary']['seconds']:.1f}s",flush=True)
    m["status"]="complete"; atomic_json(out/"manifest.json",m)
    from .pair_basket_render import render
    render(out)


if __name__=="__main__": main()
