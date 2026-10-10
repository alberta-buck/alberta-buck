"""What the machine's work costs in gas, by the kind of call a keeper would make.

Runs two 120-day lab worlds with every actor active (the three-phase cycle and the
commodity profile) and totals the gas of each transaction by who it was sent to. The
lab records only a run's total gas; this wraps the simulator's send to split it, so it
reaches into the lab's internals and should be replaced once frames record gas by kind.

Run inside the Nix shell with the Python environment active:
    python doc/review/wheel-gas.py /tmp/wheel-gas
"""
import collections, json, multiprocessing, statistics, sys
from concurrent.futures import ProcessPoolExecutor

def run(profile_name, stage):
    from eth_utils import keccak
    from alberta_buck.sim import chain as chain_mod
    from alberta_buck.sim.control_lab import engine
    from alberta_buck.sim.control_lab.block_pairs import PROFILES, simulate_observed
    from alberta_buck.sim.control_lab.model import load_config
    cfg = load_config("doc/control-lab.toml")
    cfg["basket"]["tilt"] = 0; cfg["feed"]["walk_amplification"] = 0; cfg["bootstrap"]["basket_warmup_days"] = 120
    profiles = {p["name"]: p for p in [*PROFILES, *cfg["profiles"]]}
    calls = []                                   # (address, fn name, args, gas, topics)
    live = dict(on=False, roles={})
    original_send = chain_mod.Chain.send
    def send(self, fn, **kw):
        rc = original_send(self, fn, **kw)
        if live["on"]:
            calls.append((fn.address, fn.fn_name, tuple(a for a in fn.args if isinstance(a, int)), int(rc["gasUsed"]),
                          {bytes(l["topics"][0]).hex() for l in rc["logs"] if l["topics"]}))
        return rc
    chain_mod.Chain.send = send
    original_run = engine.World.run
    def world_run(self, rows):
        d = self.d
        live["roles"] = {d.wheel.address: "policy wheel", self.arb.address: "arb wheel", self.pair_wheel.address: "observer wheel",
                         d.simlp.address: "feed driver", self.buyer.address: "buyer", self.supplier.address: "seller",
                         self.signals.address: "parity probe"}
        live["on"] = True
        return original_run(self, rows)
    engine.World.run = world_run
    r = simulate_observed(cfg, profiles[profile_name], stage, basket_mode="pair")
    transfer = keccak(text="PairResponse(uint256,uint256,uint256,uint256)").hex()
    desk_op = None
    buckets = collections.defaultdict(list)
    for address, name, args, gas, topics in calls:
        role = live["roles"].get(address, "other")
        key = f"{role}.{name}"
        if role == "policy wheel" and name == "tick":
            key += " (with a paired transfer)" if transfer in topics else " (no transfer)"
        buckets[key].append(gas)
    days = cfg["days"]
    out = {k: dict(calls=len(v), total=sum(v), mean=round(statistics.mean(v)), median=round(statistics.median(v)), max=max(v),
                   per_day=round(sum(v) / days)) for k, v in buckets.items()}
    return profile_name, stage, out, r["summary"]["gas"], r["frames"][-1]["cumulative"]["arb_cycles"]

if __name__ == "__main__":
    jobs = [("three-phase", "all"), ("commodities", "all")]
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        for profile, stage, out, total, cycles in pool.map(run, *zip(*jobs)):
            print(f"\n{profile} / {stage}: {total:,} gas in all live transactions; {cycles} arbitrage cycles")
            print(f"  {'transaction':48s} {'calls':>6s} {'mean':>9s} {'median':>9s} {'max':>10s} {'gas/day':>11s}")
            for k, v in sorted(out.items(), key=lambda kv: -kv[1]["total"]):
                print(f"  {k:48s} {v['calls']:6d} {v['mean']:9,d} {v['median']:9,d} {v['max']:10,d} {v['per_day']:11,d}")
            json.dump(out, open(sys.argv[1] + f"-{profile}.json", "w"), indent=1)
