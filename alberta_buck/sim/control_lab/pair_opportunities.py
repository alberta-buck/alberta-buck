"""Replay saved basket cases with pre-response diagnostics, without changing policy."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path

from .block_pairs import run_case
from .cli import atomic_json, fingerprint
from .model import digest


def economics(value):
    """Receipt offsets and gas change when a lab-only event is inserted."""
    ignored = {"gas", "transactions", "transaction", "log_index", "pair_decisions"}
    if isinstance(value, dict):
        return {k: economics(v) for k,v in value.items() if k not in ignored}
    if isinstance(value, list): return [economics(v) for v in value]
    return value


def replay(baseline_path, out, code):
    from alberta_buck.sim.chain import load_artifact
    baseline = json.loads(Path(baseline_path).read_text())
    key = digest(dict(baseline=baseline["cache_key"], implementation=code,
        harness=load_artifact("PairBasketDecisionTrace")))
    path = Path(out) / f"{baseline['profile']['name']}-{key[:16]}.json"
    if not path.exists():
        run_case(baseline["config"], baseline["profile"], "basket", key, code, str(path), True, "pair", True)
    r = json.loads(path.read_text())
    assert len(r["frames"]) == len(baseline["frames"])
    for f, b in zip(r["frames"], baseline["frames"]):
        assert economics(f) == economics(b), f"Diagnostic replay changed economics at day {f['day']}"
    coverage = [len(f["pair_decisions"]) for f in r["frames"]]
    # In this rearmed six-hour campaign every response must be observable.
    # A swallowed revert would remove its trace and fail this check.
    assert all(n == 1 for n in coverage), f"Incomplete response coverage: {coverage}"
    print(f"{baseline['profile']['name']}: exact economic equality, {len(coverage)} traced responses", flush=True)
    return dict(profile=baseline["profile"]["name"], file=path.name, cache_key=key,
        baseline=str(baseline_path), economic_equality=True, traced_responses=len(coverage))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="doc/control-lab/pair-basket")
    ap.add_argument("--out", default="doc/control-lab/pair-opportunities")
    ap.add_argument("--profiles", default="three-phase,flat-noise80")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--render-only", action="store_true")
    args = ap.parse_args(argv)
    if args.jobs < 1: ap.error("--jobs must be positive")
    root = Path(args.out); root.mkdir(parents=True, exist_ok=True)
    if not args.render_only:
        source = Path(args.source)
        m = json.loads((source/"manifest.json").read_text())
        names = args.profiles.split(",")
        entries = [e for e in m["entries"].values() if e["stage"] == "pair" and e["profile"] in names]
        if len(entries) != len(names): ap.error("profiles must name distinct saved pair-basket cases")
        code = {**fingerprint(), "audit": digest(dict(runner=Path(__file__).read_text(),
            observer_runner=Path(__file__).with_name("block_pairs.py").read_text()))}
        with ProcessPoolExecutor(max_workers=args.jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
            futures = [pool.submit(replay, str(source/e["file"]), str(root), code) for e in entries]
            results = [f.result() for f in futures]
        atomic_json(root/"manifest.json", dict(implementation=code, entries=results))
    from .pair_opportunities_render import render
    render(root)


if __name__ == "__main__": main()
