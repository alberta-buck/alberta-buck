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

Excursion response block (stats["excursions"], a list; never affects the
verdict): every injection in the vector -- a whale raid (raid_phase == 2
span), a price_shock, or any other applied intervention -- gets a window
(`excursion_windows`) and a response record (`excursion_response`): signed
peak |bv-1| and its day, %-days AUC, recovery time back inside a band, K
movement / rail time, buckUsd extreme, axis-2 weight-deviation excess, and
the excursion-arb / raid population deltas across the span.  A second CLI
table prints one row per window when any vector has one.

CLI flags (parsed out of argv before the paths; no flags = the historical
verdict table only):
    --resp-days N   response span after the injection end (default 90)
    --band X        recovery band on |bv-1| (default 0.01)
    --json          dump the full stats list as JSON instead of tables
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

RESP_DAYS = 90            # excursion response span past the injection end
EXC_BAND = 0.01           # |bv-1| recovery band
PRE_DAYS = 30             # pre-injection baseline span
RECOVERY_HOLD_DAYS = 5    # in-band hold required to call a recovery


def _col(frames, key, default=0):
    return [f.get(key, default) for f in frames]


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if not xs:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


# ---------------------------------------------------------------------------
# Excursion response
# ---------------------------------------------------------------------------

def _frame_days(frames) -> list[int]:
    """Per-frame day (never the frame index: vectors may be day-sparse)."""
    return [int(f.get("day", i)) for i, f in enumerate(frames)]


def _frame_deltas(days: list[int]) -> list[int]:
    """Rectangle-rule day width of each frame: next day - this day; the
    final frame reuses the previous width (1 for a single frame)."""
    n = len(days)
    if n == 0:
        return []
    out = [max(1, days[i + 1] - days[i]) for i in range(n - 1)]
    out.append(out[-1] if out else 1)
    return out


def _wdev(frame) -> float | None:
    """Axis-2 weight deviation: sum_i |actual_i - target_i| over
    poolWeights; None when the frame carries no weights."""
    pw = frame.get("poolWeights")
    if not pw:
        return None
    try:
        return sum(abs(float(a) - float(t)) for a, t in pw)
    except (TypeError, ValueError):
        return None


def _busd(frame):
    """BUCK/USD spot in micro-USD (buckUsd, fallback buck_usd); None/0 ->
    None."""
    v = frame.get("buckUsd")
    if v is None:
        v = frame.get("buck_usd")
    return v if v else None


def excursion_windows(d: dict, resp_days: int = RESP_DAYS) -> list[dict]:
    """Detect the injection windows of a loaded vector dict `d`.

    One record per injection (nothing merged), sorted by day0:

      * raid: each contiguous span of frames with raid_phase == 2 ->
        kind "raid:<side>" (side from the frames' raid_side, default
        "sell"), day0/day1 = first/last day of the injection span, src "raid".
      * price_shock: each ok meta.interventions_applied entry ->
        kind "shock:<token>x<mult>", day0 = the later of applied_day and
        from_day (the shock is live only from from_day on), day1 = until_day
        when present (the lift is the second edge) else day0, src "iv".
      * any other ok intervention (set_knob, fund, uptake_shock, add_agents,
        retune, ...): kind "iv:<action>[:<cls>]", day0 = day1 = applied_day,
        src "iv".

    Each record also carries resp_end = day1 + resp_days, the response span
    end `excursion_response` will measure over.
    """
    out = []
    frames = d.get("frames") or []
    days = _frame_days(frames)

    # -- raids: contiguous raid_phase == 2 spans ------------------------ #
    i, n = 0, len(frames)
    while i < n:
        if int(frames[i].get("raid_phase", 0) or 0) != 2:
            i += 1
            continue
        j = i
        side = None
        while j < n and int(frames[j].get("raid_phase", 0) or 0) == 2:
            side = side or frames[j].get("raid_side")
            j += 1
        out.append({"kind": f"raid:{side or 'sell'}",
                    "day0": days[i], "day1": days[j - 1], "src": "raid"})
        i = j

    # -- interventions ---------------------------------------------------- #
    meta = d.get("meta") or {}
    for iv in meta.get("interventions_applied") or []:
        if not iv.get("ok", True):
            continue
        act = str(iv.get("action", "?"))
        day = int(iv.get("applied_day", iv.get("day", 0)) or 0)
        if act == "price_shock":
            from_day = iv.get("from_day")
            day0 = max(day, int(from_day)) if from_day is not None else day
            until = iv.get("until_day")
            day1 = max(day0, int(until)) if until is not None else day0
            kind = f"shock:{iv.get('token', '?')}x{iv.get('mult', '?')}"
        else:
            day0 = day1 = day
            kind = f"iv:{act}" + (f":{iv['cls']}" if iv.get("cls") else "")
        out.append({"kind": kind, "day0": day0, "day1": day1, "src": "iv"})

    out.sort(key=lambda w: (w["day0"], w["day1"]))
    for w in out:
        w["resp_end"] = w["day1"] + int(resp_days)
    return out


def _signed_extreme(vals):
    """The signed value of largest magnitude (None for an empty list)."""
    best = None
    for v in vals:
        if best is None or abs(v) > abs(best):
            best = v
    return best


def excursion_response(frames: list, w: dict, resp_days: int = RESP_DAYS,
                       band: float = EXC_BAND, pre_days: int = PRE_DAYS,
                       kmin: float = 0.0, kmax: float = 0.95,
                       hold_days: int = RECOVERY_HOLD_DAYS) -> dict:
    """Measure the system's response to injection window `w`
    ({day0, day1}) over the frames.

    Spans (all by the frames' `day` field):
      pre   = [day0 - pre_days, day0)            baseline means
      span  = [day0, day1 + resp_days]           response measurement
    Integrals use the rectangle rule: each frame's value holds for
    delta = next frame's day - its day (the final frame reuses the previous
    delta), so day-sparse vectors integrate correctly.

    Returns (None where the inputs are absent on the vector):
      pre_bv, pre_k, pre_wdev, pre_buck_usd   baseline means (pre empty ->
                                              the first span frame)
      peak_dev        signed bv-1 at the argmax |bv-1| over span
                      (+ = discount / BUCK cheap, - = premium)
      peak_day, peak_offset (= peak_day - day0)
      auc_pct_days    sum |bv-1| * 100 * delta over span
      recovery_days   first day >= max(day1, peak_day) from which |bv-1| <
                      band holds for >= hold_days of data (rectangle
                      coverage), minus day1; searched to the vector end, so
                      None means "never recovered in the data"
      k_move          max |K - pre_k| over span
      rail_days       days (sum of deltas) with K on a rail over span
      buck_usd_ext    signed extreme of buckUsd / pre_buck_usd - 1 over span
      wdev_peak       max axis-2 weight deviation over span minus pre_wdev
      wdev_peak_day
      wdev_recovery_days  first day >= max(day1, wdev_peak_day) with
                      wdev < pre_wdev + band, minus day1; searched to the
                      vector end
      d_exc_pnl_m, d_raid_pnl_m   population P&L change across the span
      d_crb_pnl_m, crb_trades     differential-mode (CommodityRebalArb)
                                  P&L vs buy-and-hold and trade count
      d_exc_real_m                excursion REALIZED P&L booked on closes
                                  (par-marked d_exc_pnl_m includes the
                                  mark-to-market of held BUCK/baskets)
                      in $M (6-dec USD), baselined at the last frame BEFORE
                      day0 (so injection-day marks count), else the first
                      span frame
      exc_entries, exc_exits      counter deltas across the same span
      d_exc_q_m       per-quadrant deltas of exc_q in $M (None if absent)
      span_end, span_frames       the measured span's last day / frame count
    """
    day0, day1 = int(w["day0"]), int(w["day1"])
    end = day1 + int(resp_days)
    days = _frame_days(frames)
    deltas = _frame_deltas(days)
    n = len(frames)

    def bv(i):
        return frames[i].get("basketVal", E18) / E18

    def bk(i):
        return frames[i].get("buckK", 0) / E18

    pre_idx = [i for i in range(n) if day0 - pre_days <= days[i] < day0]
    span_idx = [i for i in range(n) if day0 <= days[i] <= end]
    if not span_idx:
        return {"span_end": None, "span_frames": 0}

    # -- baselines ---------------------------------------------------------- #
    ref_idx = pre_idx or [span_idx[0]]
    pre_bv = _mean([bv(i) for i in ref_idx])
    pre_k = _mean([bk(i) for i in ref_idx])
    pre_wd = [x for x in (_wdev(frames[i]) for i in ref_idx) if x is not None]
    pre_wdev = _mean(pre_wd) if pre_wd else None
    pre_bu = [x for x in (_busd(frames[i]) for i in ref_idx) if x]
    pre_busd = _mean(pre_bu) if pre_bu else None

    # -- bv excursion ------------------------------------------------------- #
    devs = [(bv(i) - 1.0, i) for i in span_idx]
    peak_dev, ipk = max(devs, key=lambda t: abs(t[0]))
    peak_day = days[ipk]
    auc = sum(abs(dv) * 100.0 * deltas[i] for dv, i in devs)

    # Recovery: from the later of the injection end and the peak, the first
    # frame opening an in-band run that covers >= hold_days of data.
    start = max(day1, peak_day)
    recovery_days = None
    i = next((k for k in range(n) if days[k] >= start), n)
    while i < n:
        if abs(bv(i) - 1.0) >= band:
            i += 1
            continue
        j = i
        while j + 1 < n and abs(bv(j + 1) - 1.0) < band:
            j += 1
        if days[j] + deltas[j] - days[i] >= hold_days:
            recovery_days = days[i] - day1
            break
        i = j + 1

    # -- K ---------------------------------------------------------------- #
    ks = [bk(i) for i in span_idx]
    k_move = max(abs(k - pre_k) for k in ks)
    rail_days = sum(deltas[i] for i, k in zip(span_idx, ks)
                    if k <= kmin + RAIL_EPS or k >= kmax - RAIL_EPS)

    # -- buckUsd ------------------------------------------------------------ #
    buck_usd_ext = None
    if pre_busd:
        buck_usd_ext = _signed_extreme(
            [x / pre_busd - 1.0 for x in (_busd(frames[i]) for i in span_idx)
             if x])

    # -- axis-2 weight deviation ------------------------------------------ #
    wdev_peak = wdev_peak_day = wdev_recovery_days = None
    if pre_wdev is not None:
        wds = [(x, i) for x, i in ((_wdev(frames[i]), i) for i in span_idx)
               if x is not None]
        if wds:
            wmax, iw = max(wds, key=lambda t: t[0])
            wdev_peak = wmax - pre_wdev
            wdev_peak_day = days[iw]
            wstart = max(day1, wdev_peak_day)
            for i in range(n):
                if days[i] < wstart:
                    continue
                x = _wdev(frames[i])
                if x is not None and x < pre_wdev + band:
                    wdev_recovery_days = days[i] - day1
                    break

    # -- population deltas -------------------------------------------------- #
    before = [i for i in range(n) if days[i] < day0]
    b = before[-1] if before else span_idx[0]
    e = span_idx[-1]
    fb, fe = frames[b], frames[e]

    def delta(key, scale=None):
        vb, ve = fb.get(key), fe.get(key)
        if vb is None or ve is None:
            return None
        return (ve - vb) if scale is None else (ve - vb) / scale

    d_exc_q = None
    qb, qe = fb.get("exc_q"), fe.get("exc_q")
    if qb and qe and len(qb) == len(qe):
        d_exc_q = [(y - x) / E6 / 1e6 for x, y in zip(qb, qe)]

    return {
        "pre_bv": pre_bv,
        "pre_k": pre_k,
        "pre_wdev": pre_wdev,
        "pre_buck_usd": pre_busd / E6 if pre_busd else None,
        "peak_dev": peak_dev,
        "peak_day": peak_day,
        "peak_offset": peak_day - day0,
        "auc_pct_days": auc,
        "recovery_days": recovery_days,
        "k_move": k_move,
        "rail_days": rail_days,
        "buck_usd_ext": buck_usd_ext,
        "wdev_peak": wdev_peak,
        "wdev_peak_day": wdev_peak_day,
        "wdev_recovery_days": wdev_recovery_days,
        "d_exc_pnl_m": delta("exc_pnl", E6 * 1e6),
        "d_exc_real_m": delta("exc_realized", E6 * 1e6),
        "d_raid_pnl_m": delta("raid_pnl", E6 * 1e6),
        "d_crb_pnl_m": delta("crb_pnl", E6 * 1e6),
        "crb_trades": delta("crb_trades"),
        "exc_entries": delta("exc_entries"),
        "exc_exits": delta("exc_exits"),
        "d_exc_q_m": d_exc_q,
        "span_end": days[e],
        "span_frames": len(span_idx),
    }


def summarize(path: str | Path, tail_frac: float = TAIL_FRAC,
              resp_days: int = RESP_DAYS, band: float = EXC_BAND,
              pre_days: int = PRE_DAYS) -> dict:
    d = json.loads(Path(path).read_text())
    frames = d["frames"]
    if not frames:
        return {"path": str(path), "frames": 0, "excursions": []}
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

    # Fallback channel source for worlds with NO FatCredit borrowers at all
    # (e.g. the portcast arm): judge liveness from the debtor + basket
    # channels instead.  Dollars are NET tail deltas of the two supply
    # tranches (credit residual = supply - basket direct-mint; basket =
    # dmOutstanding); events count debtor deploys and basket entries/exits
    # so an offsetting churn still reads as alive.  Only engaged when the
    # final frame shows the fat channel never existed (limit/issued/retired
    # all zero), so every historical vector's stats are unchanged.
    last = frames[-1]
    channel_src = "fat"
    channel_events_tail = None
    if (not last.get("fat_limit", 0) and not last.get("fat_issued", 0)
            and not last.get("fat_retired", 0)):
        channel_src = "bcd+dm"
        dm_t = _col(tail, "dmOutstanding")
        res_t = [s - o for s, o in zip(sup_t, dm_t)]
        d_res = res_t[-1] - res_t[0]
        d_dm = dm_t[-1] - dm_t[0]
        iss_net = max(0, d_res) + max(0, d_dm)
        ret_net = max(0, -d_res) + max(0, -d_dm)
        iss_t = [0, iss_net]              # reuse the delta-based reporting
        ret_t = [0, ret_net]
        channel_events_tail = sum(
            _col(tail, k)[-1] - _col(tail, k)[0]
            for k in ("bcd_deploys", "dmEntries", "dmExits"))

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
        "channel_src": channel_src,
        "channel_events_tail": channel_events_tail,
        "iv_applied": len(meta.get("interventions_applied", [])),
        "iv_failed": sum(1 for iv in meta.get("interventions_applied", [])
                         if not iv.get("ok", True)),
    }
    # Excursion response block: measurement only, never part of accept().
    stats["excursions"] = [
        {**w, **excursion_response(frames, w, resp_days=resp_days, band=band,
                                   pre_days=pre_days, kmin=kmin, kmax=kmax)}
        for w in excursion_windows(d, resp_days=resp_days)]
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
    if stats.get("channel_src", "fat") == "fat":
        if stats["issued_tail_m"] <= 0.0 and stats["retired_tail_m"] <= 0.0:
            reasons.append("issuance/redemption channels dead in the tail")
    else:
        # No-FatCredit world: net tranche deltas OR channel events suffice.
        if (stats["issued_tail_m"] <= 0.0 and stats["retired_tail_m"] <= 0.0
                and (stats.get("channel_events_tail") or 0) <= 0):
            reasons.append("issuance/redemption channels (bcd+dm) dead "
                           "in the tail")
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
            + ("PASS" if ok else "FAIL: " + "; ".join(reasons))
            + (" [ch:bcd+dm net]"
               if stats.get("channel_src", "fat") != "fat" else ""))


EXC_HEADERS = ("name", "kind", "day0-day1", "peak%", "@d", "recov", "auc",
               "Kmove", "rail", "$ext%", "wdev+", "dExc$M", "dRaid$M",
               "ent/ex")


def _f(v, spec, none="-"):
    """Format v with `spec`, or the `none` marker when it is None."""
    return none if v is None else format(v, spec)


def exc_header() -> str:
    return (f"{'name':<18} {'kind':<24} {'day0-day1':>10} {'peak%':>6} "
            f"{'@d':>4} {'recov':>6} {'auc':>7} {'Kmove':>6} {'rail':>5} "
            f"{'$ext%':>6} {'wdev+':>6} {'wrec':>5} {'dExc$M':>7} "
            f"{'dReal$M':>8} {'dRaid$M':>8} {'dCrb$M':>7} {'ent/ex':>7}")


def exc_row(stats: dict, x: dict) -> str:
    """One excursion-table line for window/response record `x`."""
    name = str(stats.get("name", ""))[:18]
    span = f"{x.get('day0')}-{x.get('day1')}"
    if x.get("span_frames", 0) == 0:
        return f"{name:<18} {x.get('kind', ''):<24.24} {span:>10}  (no frames in span)"
    pk = x.get("peak_dev")
    rec = x.get("recovery_days")
    bu = x.get("buck_usd_ext")
    ent, ex = x.get("exc_entries"), x.get("exc_exits")
    ent_ex = ("-" if ent is None and ex is None
              else f"{_f(ent, '.0f')}/{_f(ex, '.0f')}")
    return (f"{name:<18} {x.get('kind', ''):<24.24} {span:>10} "
            f"{_f(None if pk is None else 100 * pk, '+.1f'):>6} "
            f"{_f(x.get('peak_offset'), 'd'):>4} "
            f"{('never' if rec is None else str(rec)):>6} "
            f"{_f(x.get('auc_pct_days'), '.0f'):>7} "
            f"{_f(x.get('k_move'), '.3f'):>6} "
            f"{_f(x.get('rail_days'), 'd'):>5} "
            f"{_f(None if bu is None else 100 * bu, '+.1f'):>6} "
            f"{_f(x.get('wdev_peak'), '+.3f'):>6} "
            f"{('never' if x.get('wdev_recovery_days') is None else str(x.get('wdev_recovery_days'))):>5} "
            f"{_f(x.get('d_exc_pnl_m'), '+.2f'):>7} "
            f"{_f(x.get('d_exc_real_m'), '+.2f'):>8} "
            f"{_f(x.get('d_raid_pnl_m'), '+.2f'):>8} "
            f"{_f(x.get('d_crb_pnl_m'), '+.2f'):>7} "
            f"{ent_ex:>7}")


def _parse_args(args: list[str]) -> tuple[dict, list[str]]:
    """Pull --resp-days N / --band X / --json out of argv; the rest are
    vector paths.  Unknown --flags raise ValueError."""
    opts = {"resp_days": RESP_DAYS, "band": EXC_BAND, "json": False}
    paths = []
    i = 0
    while i < len(args):
        a = args[i]
        key, eq, val = a.partition("=")
        if key == "--json":
            opts["json"] = True
        elif key in ("--resp-days", "--band"):
            if not eq:
                i += 1
                if i >= len(args):
                    raise ValueError(f"{key} needs a value")
                val = args[i]
            if key == "--resp-days":
                opts["resp_days"] = int(val)
            else:
                opts["band"] = float(val)
        elif a.startswith("--"):
            raise ValueError(f"unknown flag {a}")
        else:
            paths.append(a)
        i += 1
    return opts, paths


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        opts, paths = _parse_args(args)
    except ValueError as e:
        print(f"eqmetrics: {e}", file=sys.stderr)
        return 2
    if not paths:
        print(__doc__)
        return 2
    allst = [summarize(p, resp_days=opts["resp_days"], band=opts["band"])
             for p in paths]
    worst = 0
    for st in allst:
        ok, _ = accept(st)
        worst = max(worst, 0 if ok else 1)
    if opts["json"]:
        print(json.dumps(allst, indent=1))
        return worst
    print(f"{'name':<18} {'seed':<7} {'days':>5} {'bv~':>8} {'bv sd':>7} "
          f"{'K~':>7} {'rail':>5} {'iss$M':>8} {'ret$M':>9} {'thr':>6}  verdict")
    for st in allst:
        print(row(st))
    if any(st.get("excursions") for st in allst):
        print(f"\nexcursion response (resp {opts['resp_days']}d, "
              f"band {100 * opts['band']:g}%)")
        print(exc_header())
        for st in allst:
            for x in st.get("excursions") or []:
                print(exc_row(st, x))
    return worst


if __name__ == "__main__":
    sys.exit(main())
