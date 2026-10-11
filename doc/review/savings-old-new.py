"""The savings world for five years, on the older upkeep and on the basket machine.

The savings demonstration's world (alberta_buck/sim/experiments/demo-savings.toml): five
commodities, depositors, retirees, the desk, the undertakings and K, two ticks a day. Each
upkeep runs it on the real five-year history and on its reverting twin, from one seed. The
older upkeep is the director with its Deploy, Fund and Trim steps (basket "equity-ops"); the
machine is doc/BASKET-MACHINE.org (basket "equity-machine").

Run inside the Nix shell with the Python environment active. A world takes about 35 minutes;
one that is already saved is read, not run again:
    python doc/review/savings-old-new.py /tmp/savings [years] [set ...]
"""
import json, multiprocessing, sys, time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

EXPERIMENT = "alberta_buck/sim/experiments/demo-savings.toml"
UPKEEPS = {"older": "equity-ops", "machine": "equity-machine"}
PRICES = ("history", "revert")
SETTLED = 7                             # the day the page's own comparison starts from: the pools have settled


def run(upkeep, prices, years, sets, out):
    from alberta_buck.sim import experiment as expmod
    from alberta_buck.sim.loop import run as simulate
    from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
    exp = expmod.load(EXPERIMENT, sets=[f"scenario.basket={UPKEEPS[upkeep]}", f"scenario.prices={prices}",
                                        f"scenario.years={years}", *sets])
    started = time.time()
    with PyrevmAnvil() as evm:
        simulate(expmod.build(exp), evm, out_path=str(out), verbose=False, basket_impl=exp.scenario["basket"])
    return upkeep, prices, time.time() - started


def waiting(frame, decimals):
    """The share of the book's TOKEN still waiting in the basket's wallets, at the TOKEN/BUCK pools' prices."""
    idle = [int(x) / 10**d * p / 1e6 for x, d, p in zip(frame["eq_idle"], decimals, frame["spotBuck"])]
    return sum(idle) / (int(frame["eq_equity"]) / 1e6)


def report(path):
    vector = json.loads(Path(path).read_text())
    frames, decimals = vector["frames"], vector["decimals"]
    first, last = frames[min(SETTLED, len(frames) - 1)], frames[-1]
    days = last["day"] - first["day"]
    price = lambda f: int(f["eq_share_price"]) / 1e18
    index, held = price(last) / price(first), last["eq_hold"] / first["eq_hold"]
    off = [sum(abs(w - t) for w, t in zip(f["eq_response_w"], f["eq_decl"])) / 2 for f in frames[SETTLED:]]
    parity = [abs(int(f["basketVal"]) / 1e18 - 1) for f in frames[SETTLED:]]
    k = [int(f["buckK"]) / 1e18 for f in frames]
    work = {name: (last.get(f"wh_eq_{name}", 0) / 1e6, last.get(f"wh_eq_{name}_n", 0))
            for name in ("deploy", "fund", "trim", "place", "transfer", "raise", "buy")}
    desk = (int(last["desk_nav"]) - int(last["desk_hold"])) / 1e6 if last.get("desk_nav") and last.get("desk_hold") else None
    return dict(
        days=len(frames), index=index, yearly=index ** (365 / days) - 1 if days else 0., over_holding=index / held - 1,
        equity=int(last["eq_equity"]) / 1e6, parity=sum(parity) / len(parity), worst_parity=max(parity),
        k=(min(k), max(k), k[-1]), off_weight=sum(off) / len(off) / 100,
        waiting={d: waiting(frames[d], decimals) for d in (30, 90, 365, len(frames) - 1) if d < len(frames)},
        fees=last.get("wh_eq_sync", 0) / 1e6, harvest=last.get("wh_sol_credited_usd", 0.), gas=last.get("wh_sol_gas", 0),
        ticks=last.get("wh_sol_ticks", 0), work={n: w for n, w in work.items() if w[1]}, desk=desk,
        desk_moved=sum(last.get(f"dk_q{q}", 0) for q in (1, 2, 3, 4)) / 1e6, entries=last.get("dmEntries", 0),
        exits=last.get("dmExits", 0), stale=sum(1 for f in frames if f.get("eq_signal_fresh") is False))


if __name__ == "__main__":
    out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
    years = float(sys.argv[2]) if len(sys.argv) > 2 else 5.
    sets = sys.argv[3:]
    name = lambda upkeep, prices: out / f"savings-{upkeep}-{prices}-{years:g}y.json"
    jobs = [(u, p) for p in PRICES for u in UPKEEPS if not name(u, p).exists()]
    with ProcessPoolExecutor(max_workers=4, max_tasks_per_child=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        for done in as_completed([pool.submit(run, u, p, years, sets, name(u, p)) for u, p in jobs]):
            print("ran %s on %s in %.0f s" % done.result(), flush=True)
    for prices in PRICES:
        print(f"\n{prices}: {years:g} years")
        for upkeep in UPKEEPS:
            r = report(name(upkeep, prices))
            print(f"  {upkeep:8s} savings index x{r['index']:.4f} from day {SETTLED} ({100 * r['yearly']:+.2f}% a year), "
                  f"{100 * r['over_holding']:+.2f}% over holding; equity {r['equity']:,.0f}")
            print(f"           basket's cost in BUCK off 1: mean {100 * r['parity']:.2f}%, "
                  f"worst {100 * r['worst_parity']:.2f}%; K {r['k'][0]:.3f} to {r['k'][1]:.3f}, ends {r['k'][2]:.3f}; "
                  f"shares off declared {r['off_weight']:.2f} points")
            print("           TOKEN waiting, of equity: "
                  + ", ".join(f"day {d} {100 * w:.1f}%" for d, w in r["waiting"].items()))
            print("           wheel: " + ", ".join(f"{n} {v:,.0f} in {c}" for n, (v, c) in r["work"].items())
                  + f"; pool fees {r['fees']:,.0f}; harvest ${r['harvest']:,.0f}; "
                  + f"{r['gas'] / 1e9:.2f} billion gas in {r['ticks']} turns")
            print(f"           desk {'-' if r['desk'] is None else format(r['desk'], '+,.0f')} over holding, "
                  f"{r['desk_moved']:,.0f} moved; depositors {r['entries']} in, {r['exits']} out; "
                  f"days with a stale parity reference {r['stale']}")
