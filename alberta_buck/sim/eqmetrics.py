"""Equilibrium acceptance metrics over a sim vector.

The acceptance target (parity-with-interior-K) for an equilibrium run:

  * tail basketValueInBuck within 1.00 +/- TOL (default 2%),
  * K interior over the tail: never pinned at a rail,
  * the issuance/redemption channels ALIVE in the tail (supply still moves,
    the throttle is not permanently clamping),
  * bounded volatility.

`summarize(vector_path)` -> dict of the stats; `accept(stats)` -> (ok,
reasons).  CLI:  python -m alberta_buck.sim.eqmetrics VECTOR [VECTOR...]
prints one row per vector plus the acceptance verdict.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

E6 = 10 ** 6
E18 = 10 ** 18

TAIL_FRAC = 0.25          # tail window = last 25% of frames
PARITY_TOL = 0.02         # |basketVal - 1| acceptance
RAIL_EPS = 1e-6           # K within this of a rail counts as railed


def _col(frames, key, default=0):
    return [f.get(key, default) for f in frames]


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if not xs:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def summarize(path: str | Path, tail_frac: float = TAIL_FRAC) -> dict:
    d = json.loads(Path(path).read_text())
    frames = d["frames"]
    if not frames:
        return {"path": str(path), "frames": 0}
    meta = d.get("meta", {})
    exp = meta.get("experiment", {})

    n = len(frames)
    t0 = max(0, int(n * (1.0 - tail_frac)))
    tail = frames[t0:]

    bv = [f["basketVal"] / E18 for f in frames]
    bv_t = bv[t0:]
    bk = [f["buckK"] / E18 for f in frames]
    bk_t = bk[t0:]

    # Rails from the resolved experiment (fall back to the coded defaults).
    dep = exp.get("deploy", {})
    kmin = float(dep.get("kmin", 0.0))
    kmax = float(dep.get("kmax", 0.95))
    rail_t = [k for k in bk_t
              if k <= kmin + RAIL_EPS or k >= kmax - RAIL_EPS]

    supply = [f["supply"] for f in frames]
    sup_t = supply[t0:]
    # Channel liveness: issuance and retirement cumulative flows must still
    # move across the tail (a frozen channel shows zero deltas), and the
    # throttle must not fire on a large share of tail frames.
    iss_t = _col(tail, "fat_issued")
    ret_t = _col(tail, "fat_retired")
    thr_t = _col(tail, "fat_throttled")
    thr_hits = sum(1 for a, b in zip(thr_t, thr_t[1:]) if b > a)
    saver_t = _col(tail, "saver_hold")

    stats = {
        "path": str(path),
        "name": exp.get("name", ""),
        "seed": exp.get("scenario", {}).get("seed"),
        "frames": n,
        "days": frames[-1].get("day", n),
        "bv_last": bv[-1],
        "bv_tail_mean": _mean(bv_t),
        "bv_tail_std": _std(bv_t),
        "bv_tail_dev": abs(_mean(bv_t) - 1.0),
        "bv_max": max(bv),
        "bv_min": min(bv),
        "k_last": bk[-1],
        "k_tail_mean": _mean(bk_t),
        "k_tail_rail_frac": len(rail_t) / max(1, len(bk_t)),
        "kmin": kmin,
        "kmax": kmax,
        "supply_last_m": supply[-1] / E6 / 1e6,
        "supply_tail_delta_m": (sup_t[-1] - sup_t[0]) / E6 / 1e6,
        "issued_tail_m": (iss_t[-1] - iss_t[0]) / E6 / 1e6,
        "retired_tail_m": (ret_t[-1] - ret_t[0]) / E6 / 1e6,
        "throttle_tail_frac": thr_hits / max(1, len(tail) - 1),
        "saver_tail_mean_m": _mean(saver_t) / E6 / 1e6,
        "iv_applied": len(meta.get("interventions_applied", [])),
        "iv_failed": sum(1 for iv in meta.get("interventions_applied", [])
                         if not iv.get("ok", True)),
    }
    return stats


def accept(stats: dict, tol: float = PARITY_TOL) -> tuple[bool, list[str]]:
    """The parity-with-interior-K acceptance gate."""
    reasons = []
    if stats.get("frames", 0) == 0:
        return False, ["empty vector"]
    if stats["bv_tail_dev"] > tol:
        reasons.append(f"tail basketVal {stats['bv_tail_mean']:.4f} "
                       f"off parity by {stats['bv_tail_dev']:.3f} > {tol}")
    if stats["k_tail_rail_frac"] > 0.0:
        reasons.append(f"K railed {100 * stats['k_tail_rail_frac']:.0f}% "
                       f"of the tail")
    if stats["issued_tail_m"] <= 0.0 and stats["retired_tail_m"] <= 0.0:
        reasons.append("issuance/redemption channels dead in the tail")
    if stats["iv_failed"]:
        reasons.append(f"{stats['iv_failed']} interventions FAILED")
    return (not reasons), reasons


HEADERS = ("name", "seed", "days", "bv_tail_mean", "bv_tail_std", "k_tail_mean",
           "k_tail_rail_frac", "issued_tail_m", "retired_tail_m",
           "throttle_tail_frac", "verdict")


def row(stats: dict) -> str:
    ok, reasons = accept(stats)
    return ("{name:<18.18} {seed!s:<7} {days:>5} "
            "{bv_tail_mean:>8.4f} {bv_tail_std:>7.4f} {k_tail_mean:>7.3f} "
            "{k_tail_rail_frac:>5.0%} {issued_tail_m:>8.2f} "
            "{retired_tail_m:>9.2f} {throttle_tail_frac:>6.0%}  "
            .format(**stats)
            + ("PASS" if ok else "FAIL: " + "; ".join(reasons)))


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    print(f"{'name':<18} {'seed':<7} {'days':>5} {'bv~':>8} {'bv sd':>7} "
          f"{'K~':>7} {'rail':>5} {'iss$M':>8} {'ret$M':>9} {'thr':>6}  verdict")
    worst = 0
    for p in args:
        st = summarize(p)
        ok, _ = accept(st)
        print(row(st))
        worst = max(worst, 0 if ok else 1)
    return worst


if __name__ == "__main__":
    sys.exit(main())
