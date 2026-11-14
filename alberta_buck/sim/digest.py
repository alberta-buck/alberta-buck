"""A one-screen digest of a completed world's vector: K, BVIB and price, where new supply came from,
who holds it, what the households did, and what was refused.

    python -m alberta_buck.sim.digest build/sim/economy-smoke-365.json [more.json ...]

Reads the snapshot vector the runner writes (report.finish); sends nothing and needs no EVM. Every
figure comes from recorded frames: =supply=, =eq_lien= (the basket's own principal), the households'
aggregate =drawn= and per-member =octl= marks (sampled every fifth day), and the Carrying accounts'
nominal BUCK in =net.carrying= (the BUCK/USDC pool, the TOKEN/BUCK pools, the basket). Money is in
whole BUCK or dollars; ratios are plain floats.
"""
from collections import Counter
import json
import sys

RAW = 10**6
SCALE = 10**36


def holders(frame):
    """Nominal BUCK held by the Carrying accounts the frame records, and by everyone else (households,
    arbitrageurs, keepers, the bootstrap lender). None when the frame has no =net.carrying=."""
    carrying = (frame.get("net") or {}).get("carrying")
    if not carrying or frame.get("supply") is None:
        return None
    whole = lambda row: int((row or {}).get("nominal_scaled") or 0) / SCALE / RAW
    out = dict(dollar_pool=whole(carrying.get("buck_usdc")),
               token_pools=sum(whole(r) for r in carrying.get("buck_tokens") or []),
               basket=whole(carrying.get("basket")))
    out["others"] = frame["supply"] / RAW - sum(out.values())
    return out


def _ratio(value):
    return None if value is None else value / 1e18


def _unbacked(frame):
    """Supply less open and deferred principal, in whole BUCK: zero when every BUCK is someone's
    principal (the canonical books). None when the frame has no =net= record."""
    net = frame.get("net")
    if not net or frame.get("supply") is None:
        return None
    return (frame["supply"] - (net.get("active_principal") or 0) - (net.get("deferred_principal") or 0)) / RAW


def digest(vector):
    """Summarise one vector (a parsed JSON mapping with "frames"). Returns a plain dict."""
    frames = vector["frames"]
    if not frames:
        raise ValueError("vector has no frames")
    first, last = frames[0], frames[-1]
    marks = [a for f in frames for a in f.get("octl", []) if "member" in a]
    final = [a for a in last.get("octl", []) if "member" in a]
    errors = Counter((a.get("error") or "")[:80] for a in marks if a.get("error"))
    step = max(1, len(frames) // 6)
    path = [dict(day=f["day"], k=_ratio(f.get("buckK")), bvib=_ratio(f.get("basketVal")),
                 buck_usd=f["buck_usd"] / RAW if f.get("buck_usd") is not None else None,
                 supply=f["supply"] / RAW if f.get("supply") is not None else None)
            for f in frames[::step] + ([last] if (len(frames) - 1) % step else [])]
    households = lambda f, key: (f.get("households") or {}).get(key, 0) or 0
    change = lambda key: ((last.get(key) or 0) - (first.get(key) or 0)) / RAW
    supply = change("supply")
    basket_lien = change("eq_lien")
    household_principal = (households(last, "drawn") - households(first, "drawn")) / RAW
    roles = {}
    for role in sorted({a.get("role") for a in final}):
        mine = [a for a in final if a.get("role") == role]
        roles[role] = dict(
            count=len(mine), exited=sum(bool(a.get("exited")) for a in mine),
            drawn=sum(a.get("drawn") or 0 for a in mine) / RAW,
            basket=sum(a.get("basket") or 0 for a in mine) / RAW,
            held_buck=sum(a.get("held_buck") or 0 for a in mine) / RAW,
            vs_counterfactual=sorted(round(((a.get("nw") or 0) - (a.get("hypo") or 0)) / RAW) for a in mine),
            actions=dict(Counter(a.get("action") for a in mine).most_common(5)))
    ks = [f["buckK"] for f in frames if f.get("buckK") is not None]
    bvibs = [f["basketVal"] for f in frames if f.get("basketVal") is not None]
    return dict(
        days=len(frames), households=len(final), roles=roles,
        k=dict(first=_ratio(ks[0]) if ks else None, last=_ratio(ks[-1]) if ks else None,
               low=_ratio(min(ks)) if ks else None, high=_ratio(max(ks)) if ks else None),
        bvib=dict(low=_ratio(min(bvibs)) if bvibs else None, high=_ratio(max(bvibs)) if bvibs else None,
                  above_one=sum(b > 10**18 for b in bvibs) / len(bvibs) if bvibs else None),
        supply=dict(change=supply, basket_lien=basket_lien, household_principal=household_principal,
                    other=supply - basket_lien - household_principal),
        holders=dict(first=holders(first), last=holders(last)),
        unbacked=_unbacked(last),
        observations=len(marks), errors=sum(errors.values()), error_kinds=dict(errors.most_common(8)),
        relief_unavailable=sum(bool(a.get("relief_unavailable")) for a in marks),
        faults=last.get("household_faults") or 0, path=path)


def render(name, d):
    out = [f"{name}: {d['days']} days, {d['households']} households, {d['observations']} household marks"]
    k, b, s = d["k"], d["bvib"], d["supply"]
    out.append(f"  K {k['first']:.4f} -> {k['last']:.4f} (low {k['low']:.4f}, high {k['high']:.4f})" if k["first"] is not None
               else "  K unavailable")
    if b["low"] is not None:
        out.append(f"  BVIB {b['low']:.4f} .. {b['high']:.4f}; above 1 on {100 * b['above_one']:.0f}% of days")
    out.append(f"  supply {s['change']:+,.0f} BUCK = basket lien {s['basket_lien']:+,.0f} + household principal "
               f"{s['household_principal']:+,.0f} + other {s['other']:+,.0f}")
    h = d.get("holders") or {}
    if h.get("first") and h.get("last"):
        names = dict(dollar_pool="BUCK/USDC pool", token_pools="TOKEN/BUCK pools", basket="basket",
                     others="everyone else")
        out.append("  held: " + "; ".join(f"{label} {h['first'][key]:,.0f} -> {h['last'][key]:,.0f}"
                                         for key, label in names.items()))
    for p in d["path"]:
        out.append(f"    day {p['day']:4d}  K {p['k'] or 0:.4f}  BVIB {p['bvib'] or 0:.4f}  BUCK/USD {p['buck_usd'] or 0:.4f}"
                   f"  supply {p['supply'] or 0:,.0f}")
    for role, r in d["roles"].items():
        out.append(f"  {role}: {r['count']} ({r['exited']} exited)  drawn {r['drawn']:,.0f}  basket {r['basket']:,.0f}"
                   f"  held {r['held_buck']:,.0f}  actions {r['actions']}")
        if role != "investor":
            out.append(f"    net worth against counterfactual ($): {r['vs_counterfactual']}")
    if d.get("unbacked") is not None:
        out.append(f"  supply less open and deferred principal: {d['unbacked']:+,.6f} BUCK")
    out.append(f"  errors {d['errors']} (relief unavailable on {d['relief_unavailable']} marks, faults {d['faults']})")
    for kind, n in d["error_kinds"].items():
        out.append(f"    {n:5d}  {kind}")
    return "\n".join(out)


def main(argv=None):
    paths = (sys.argv[1:] if argv is None else argv)
    if not paths:
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    for path in paths:
        with open(path) as f:
            print(render(path, digest(json.load(f))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
