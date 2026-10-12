"""The five-year matrix on three baskets: does each world hold parity, where does K end, and what is a share worth.

The 14 worlds of alberta_buck/sim/experiments/rebalancing-eq-5yr*.toml, each run on the pro-rata
basket, on the equity basket's older upkeep and on the basket machine, with the five TOKENs. A
share is valued in BUCKs and in baskets (its price over the basket's cost in BUCK): the two part
when the BUCK leaves its basket. Reads a directory of finished runs named <world>-<basket>.json:
    python doc/review/matrix-baskets.py build/sim/matrix5
The runs are made one world at a time (each is an hour or two); with SIM_CREDIT_FRAMES=1 the
equity baskets' frames carry the lien and the BUCKs held:
    python -m alberta_buck.sim --experiment alberta_buck/sim/experiments/rebalancing-eq-5yr-portcast.toml \\
        --basket equity-machine --out build/sim/matrix5/portcast-equity-machine.json
(--basket equity-ops for the older upkeep; --basket prorata for the pro-rata basket.)
"""
import json, sys
from pathlib import Path
D = Path(sys.argv[1]); mean = lambda xs: sum(xs) / len(xs)
ARMS = ["base", "deep", "endow", "endow-deep", "capacity", "debtors", "debtors-mature", "portcast", "debtors-mature-basketeers",
        "debtors-mature-basketeers-originate", "debtors-mature-basketeers-endog", "debtors-mature-basketeers-endog-excursion",
        "debtors-mature-basketeers-endog-raid", "debtors-mature-basketeers-endog-excursion-raid"]
print(f"{'arm':40s} {'basket':8s} {'parity>d90':>10s} {'bvib end':>8s} {'K end':>6s} {'K at rail from':>14s} {'share, BUCK':>11s} {'share, baskets':>14s} {'issued/eq':>9s} {'supply $M':>9s}")
for arm in ARMS:
    for name, key in (("pro-rata", "prorata"), ("older", "equity-ops"), ("machine", "equity-machine")):
        p = D / f"{arm}-{key}.json"
        if not p.exists():
            print(f"{arm[:40]:40s} {name:8s} did not finish"); continue
        fr = json.loads(p.read_text())["frames"]
        per = max(1, round(len(fr) / (fr[-1]["day"] + 1))); last = fr[len(fr) - 90 * per:]
        bv = lambda f: int(f["basketVal"]) / 1e18
        par = [abs(bv(f) - 1) for f in fr if f.get("basketVal") and f["day"] >= 90]
        rail = next((f["day"] for f in fr if int(f["buckK"]) / 1e18 >= .9495), None)
        row = f"{arm[:40]:40s} {name:8s} {100 * mean(par):9.2f}% {mean([bv(f) for f in last]):8.3f} {int(fr[-1]['buckK']) / 1e18:6.3f} {('day %d' % rail) if rail is not None else 'never':>14s}"
        if key != "prorata":
            price = [int(f["eq_share_price"]) / 1e18 for f in last]
            real = [x / bv(f) for x, f in zip(price, last)]
            eq = int(fr[-1]["eq_equity"]) or 1
            issued = (fr[-1].get("eq_lien", 0) - fr[-1].get("eq_held_buck", 0)) / eq
            row += f" {mean(price):11.3f} {mean(real):14.3f} {100 * issued:+8.0f}%"
        else:
            row += f" {'':11s} {'':14s} {'':9s}"
        print(row + f" {int(fr[-1].get('supply', 0)) / 1e12:9.1f}")
    print()
