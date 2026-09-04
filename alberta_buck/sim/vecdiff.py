"""Frame-by-frame difference of two sim vectors -- the L3 drift gauge.

    python -m alberta_buck.sim.vecdiff A.json B.json [--keys k1,k2,...]

`cmp` answers byte-identity; this answers "how far, and did anyone ACT
differently" when it is not: the max and mean |delta| of bvib (basketVal /
1e18) and of K (buckK / 1e18), then every scalar frame field whose series
differs, with its last value in A and B and its max |delta|.  A field that
never falls in either vector is reported as a cumulative COUNTER
(saver_buys, cycleTrades, exc_entries, ...): a changed counter is an agent
that acted differently; the rest are gauges and prices.  Lists (per-token
series) are compared elementwise.  Exit status 0 when the frames are
identical, 1 otherwise.

Written for WAVE3.org decision 8 (wp/d8-slot0), whose identity pair is
expected to drift by the pool fees a balance quote counted as reserves.
"""

from __future__ import annotations

import argparse
import json
import sys


def _load(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)["frames"]


def _series(frames: list[dict], key: str):
    return [f.get(key) for f in frames]


def _scalar(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _monotone(s) -> bool:
    vals = [x or 0 for x in s]
    return all(x <= y for x, y in zip(vals, vals[1:]))


def summarize(a: list[dict], b: list[dict], keys: list[str] | None = None) -> dict:
    n = min(len(a), len(b))
    out = {"frames": (len(a), len(b)), "identical": a[:n] == b[:n] and len(a) == len(b),
           "bvib": None, "buckK": None, "changed": []}
    for name, key, scale in (("bvib", "basketVal", 1e18), ("buckK", "buckK", 1e18)):
        da = [abs((x or 0) - (y or 0)) / scale
              for x, y in zip(_series(a, key), _series(b, key))]
        out[name] = {"max": max(da) if da else 0.0,
                     "mean": sum(da) / len(da) if da else 0.0,
                     "argmax": (da.index(max(da)) if da else -1)}
    all_keys = keys or sorted(set(a[0]) | set(b[0]))
    for key in all_keys:
        sa, sb = _series(a, key)[:n], _series(b, key)[:n]
        if sa == sb:
            continue
        if all(_scalar(x) or x is None for x in sa + sb):
            dmax = max(abs((x or 0) - (y or 0)) for x, y in zip(sa, sb))
            first = next(i for i, (x, y) in enumerate(zip(sa, sb)) if x != y)
            ints = all(isinstance(x, int) or x is None for x in sa + sb)
            # A cumulative action counter never falls; a gauge does.
            mono = ints and all(_monotone(s) for s in (sa, sb))
            kind = "counter" if mono else ("int" if ints else "float")
            out["changed"].append({"key": key, "kind": kind, "first": first,
                                   "last_a": sa[-1], "last_b": sb[-1], "dmax": dmax})
        elif all(isinstance(x, list) or x is None for x in sa + sb):
            dmax = 0
            first = None
            for i, (x, y) in enumerate(zip(sa, sb)):
                if x != y:
                    first = i if first is None else first
                    for p, q in zip(x or [], y or []):
                        if _scalar(p) and _scalar(q):
                            dmax = max(dmax, abs(p - q))
            out["changed"].append({"key": key, "kind": "list", "first": first,
                                   "last_a": sa[-1], "last_b": sb[-1], "dmax": dmax})
        else:
            first = next(i for i, (x, y) in enumerate(zip(sa, sb)) if x != y)
            out["changed"].append({"key": key, "kind": "other", "first": first,
                                   "last_a": None, "last_b": None, "dmax": None})
    return out


def _fmt(v):
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, list):
        if not all(_scalar(x) for x in v):
            return f"list[{len(v)}]"
        return "[" + ", ".join(_fmt(x) for x in v[:6]) + (", ..." if len(v) > 6 else "") + "]"
    if isinstance(v, dict):
        return f"dict[{len(v)}]"
    return str(v)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--keys", default="", help="comma-separated frame fields (default: all)")
    args = ap.parse_args(argv)
    a, b = _load(args.a), _load(args.b)
    s = summarize(a, b, [k for k in args.keys.split(",") if k] or None)
    print(f"frames {s['frames'][0]} vs {s['frames'][1]}; identical: {s['identical']}")
    for name in ("bvib", "buckK"):
        m = s[name]
        print(f"|delta {name}|  max {m['max']:.6g} (day {m['argmax']})  mean {m['mean']:.6g}")
    counts = [c for c in s["changed"] if c["kind"] == "counter"]
    values = [c for c in s["changed"] if c["kind"] != "counter"]
    print(f"changed cumulative counters (agents acted differently): {len(counts)}; "
          f"changed gauges/values: {len(values)}")
    for c in counts + values:
        print(f"  {c['key']:<28} {c['kind']:<6} first day {c['first']!s:<4} "
              f"A {_fmt(c['last_a'])}  B {_fmt(c['last_b'])}  max|d| {_fmt(c['dmax'])}")
    return 0 if s["identical"] else 1


if __name__ == "__main__":
    sys.exit(main())
