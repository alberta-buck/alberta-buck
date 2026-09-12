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


# ---------------------------------------------------------------------------
# WP-1: the basket's own side, the markout ledger, K forecastability
# (CARRY-CONVEXITY.org 3.3 / 8.1; WAVE3.org R1, R2, R14)
# ---------------------------------------------------------------------------

def _nearest(days: list[int], day: int) -> int:
    """Index of the frame nearest `day` (the earlier one on ties)."""
    return min(range(len(days)), key=lambda i: (abs(days[i] - day), i))


def _bs_snap(f: dict) -> dict:
    """One frame's basket-side balance sheet in report units.  NAV in
    BUCK, in USD (x buck_usd) and in BASKETS (/ bvib) -- the last is the
    depositor's unit of account; $M and k."""
    bv = (f.get("basketVal") or 0) / E18
    bu = (_busd(f) or 0) / E6
    nav = (f.get("basketNav") or 0) / E6
    return {
        "day": f.get("day"),
        "bvib": bv,
        "buck_usd": bu,
        "nav_b_m": nav / 1e6,
        "nav_usd_m": nav * bu / 1e6,
        "nav_bsk_m": (nav / bv / 1e6) if bv else None,
        "treasury_k": (f.get("treasuryBuck") or 0) / E6 / 1e3,
        "dm_real_m": (f.get("dmProfitUsd") or 0) / E6 / 1e6,
        "dm_mark_m": (f.get("directMintPnl") or 0) / E6 / 1e6,
        "supply_m": (f.get("supply") or 0) / E6 / 1e6,
        "k": (f.get("buckK") or 0) / E18,
    }


def basket_side(frames: list, w: dict | None = None, plus_days: int = 60,
                pre_days: int = PRE_DAYS) -> dict:
    """The basket's own side of an excursion (the columns no catalogue
    summary carried until 2026-09-01): at the pre-injection baseline
    (pre_days before day0), at day1 + plus_days, and at the end.  Without
    a window: first frame / None / last frame."""
    if not frames:
        return {}
    days = _frame_days(frames)
    if w is None:
        return {"pre": _bs_snap(frames[0]), "plus": None,
                "end": _bs_snap(frames[-1])}
    day0, day1 = int(w["day0"]), int(w["day1"])
    return {"pre": _bs_snap(frames[_nearest(days, day0 - pre_days)]),
            "plus": _bs_snap(frames[_nearest(days, day1 + plus_days)]),
            "end": _bs_snap(frames[-1])}


def _mx_sub(a, b):
    """Elementwise a - b over the ledger's frame aggregates (lists of
    ints / dicts of lists); a missing b is zero."""
    if isinstance(a, dict):
        return {k: _mx_sub(v, (b or {}).get(k)) for k, v in a.items()}
    if isinstance(a, list):
        b = b or [0] * len(a)
        return [_mx_sub(x, y) for x, y in zip(a, b)]
    return a - (b or 0)


def markout_panel(frames: list, w: dict | None = None) -> dict | None:
    """The markout ledger (frame["mx"], cumulative) over the whole run or,
    with a window, over [last frame before day0, resp_end]: per basket
    pool fees / adverse (total, common-mode) at each horizon in $M and
    the carry ratio fees / adverse at the longest horizon; the BUCK/USDC
    venue; per-class toxicity = adverse per $M of that class's basket
    flow.  None when the vector carries no ledger (pre-WP-1 vectors)."""
    if not frames or not frames[-1].get("mx"):
        return None
    days = _frame_days(frames)
    if w is None:
        base, end = None, frames[-1]["mx"]
    else:
        day0 = int(w["day0"])
        before = [i for i in range(len(frames)) if days[i] < day0]
        base = frames[before[-1]].get("mx") if before else None
        end = frames[_nearest(days, int(w.get("resp_end", days[-1])))].get("mx")
        if not end:
            return None
    mx = _mx_sub(end, base) if base else end
    hs = [int(x) for x in mx.get("h", [])]
    if not hs:
        return None
    hl = hs[-1]
    M = 1e6 * E6

    def pool_row(i, r):
        adv = {D: [r[3 + 2 * k] / M, r[4 + 2 * k] / M] for k, D in enumerate(hs)}
        tot = adv[hl][0]
        return {"i": i, "n": r[0], "vol_m": r[1] / M, "fees_m": r[2] / M,
                "adv_m": adv,
                "carry_ratio": (r[2] / M) / (-tot) if tot < 0 else None}

    pools = [pool_row(i, r) for i, r in enumerate(mx.get("pool", []))]
    ubr = mx.get("ub") or []
    ub = None
    if ubr:
        ub = {"n": ubr[0], "vol_m": ubr[1] / M, "fees_m": ubr[2] / M,
              "adv_m": {D: ubr[3 + k] / M for k, D in enumerate(hs)}}
    cls = {}
    for c, r in (mx.get("cls") or {}).items():
        adv = {D: [r[3 + 2 * k] / M, r[4 + 2 * k] / M] for k, D in enumerate(hs)}
        nh = len(hs)
        vol_ub = r[3 + 2 * nh] / M
        adv_ub = {D: r[4 + 2 * nh + k] / M for k, D in enumerate(hs)}
        vol = r[1] / M
        cls[c] = {"n": r[0], "vol_m": vol, "fees_m": r[2] / M, "adv_m": adv,
                  "tox": (adv[hl][0] / vol) if vol > 0 else None,
                  "vol_ub_m": vol_ub, "adv_ub_m": adv_ub,
                  "tox_ub": (adv_ub[hl] / vol_ub) if vol_ub > 0 else None}
    fees = sum(p["fees_m"] for p in pools)
    adv_t = sum(p["adv_m"][hl][0] for p in pools)
    adv_c = sum(p["adv_m"][hl][1] for p in pools)
    ratios = [p["carry_ratio"] for p in pools if p["carry_ratio"] is not None]
    whale_adv = sum(v["adv_m"][hl][0] for c, v in cls.items()
                    if c.startswith("WhaleRaidAgent"))
    whale_vol = sum(v["vol_m"] for c, v in cls.items()
                    if c.startswith("WhaleRaidAgent"))
    whale_ub = sum(v["adv_ub_m"][hl] for c, v in cls.items()
                   if c.startswith("WhaleRaidAgent"))
    return {"h": hs, "window": w is not None, "pool": pools, "ub": ub,
            "cls": cls,
            "basket": {"fees_m": fees, "adv_m": adv_t, "adv_cm_m": adv_c,
                       "adv_diff_m": adv_t - adv_c, "netlp_m": fees + adv_t,
                       "worst_carry": min(ratios) if ratios else None},
            "whale": {"adv_basket_m": whale_adv, "vol_basket_m": whale_vol,
                      "adv_ub_m": whale_ub}}


def _ls(xs, ys):
    """Least-squares slope through the origin and R^2 of ys on xs."""
    den = sum(x * x for x in xs)
    if not den:
        return None, None
    b = sum(x * y for x, y in zip(xs, ys)) / den
    my = _mean(ys)
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - b * x) ** 2 for x, y in zip(xs, ys))
    return b, (1.0 - ss_res / ss_tot) if ss_tot else None


def k_forecast(frames: list, meta: dict | None,
               horizons=(7, 30)) -> dict | None:
    """K comprehensibility (WAVE3 R14 / G8) -- three questions a competent
    observer can check against public state.

    1. Is K CALM?  Persistence K(t+h) = K(t): MAE and the share within one
       point (0.01) at each horizon.  In organic regimes K should sit
       still; excursions move it by design.
    2. Does K OBEY ITS LAW?  The shipped controller is
         K = K_I - Kp (bvib - 1),   dK_I/dt = Ki (1 - bvib),
       so day to day  dK ~ Kp * d(1 - bvib)  and over a month
       dK ~ Ki * mean(1 - bvib) * 30.  Both are fitted by least squares
       (slope and R^2) and compared with the designed gains.  An observer
       who knows the deviation history knows K: the EX-POST direction hit
       rate is sign(dK_30) == sign(mean deviation) on months with
       |mean deviation| > 0.25%.
    3. Can K be FORECAST ex ante from today's state alone?  The naive law
       K + Ki h (1 - bvib) is scored beside the observer's law
       K + Kp (1 - bvib) + Ki h * (trailing-h mean of (1 - bvib)) -- the
       P kick unwinds as the deviation reverts, and the recent mean
       deviation persists -- with MAE and within-one-point.  Measured
       2026-09-02: persistence beats both at 7 and 30 days because
       excursions revert inside a week; that is a property of the P term,
       not a defect of the law, and it is reported so it can be decided
       (WAVE3 decisions pending 7)."""
    if len(frames) < 3:
        return None
    exp = (meta or {}).get("experiment", {}) or {}
    dep = exp.get("deploy", {}) or {}
    dk_rail = float(dep.get("dk_rail", 0.5))
    e_max = float(dep.get("e_max", 0.10))
    tau = float(dep.get("tau_i_days", 90.0))
    kp_frac = float(dep.get("kp_frac", 0.02))
    kp = dep.get("kp")
    ki = dep.get("ki")
    kp_real = float(kp) if kp else kp_frac * dk_rail / e_max
    ki_day = (float(ki) * 86400.0) if ki else dk_rail / (e_max * tau)
    kmin = float(dep.get("kmin", 0.0))
    kmax = float(dep.get("kmax", 0.95))
    days = _frame_days(frames)
    n = len(frames)
    bv = [f.get("basketVal", E18) / E18 for f in frames]
    bk = [f.get("buckK", 0) / E18 for f in frames]
    e = [1.0 - x for x in bv]
    out = {"kp_designed": kp_real, "ki_per_day_designed": ki_day, "h": {}}

    # -- the law, fitted ---------------------------------------------- #
    d1 = [(e[i] - e[i - 1], bk[i] - bk[i - 1]) for i in range(1, n)
          if days[i] - days[i - 1] <= 1]
    kp_fit, kp_r2 = _ls([x for x, _ in d1], [y for _, y in d1]) \
        if d1 else (None, None)
    m30 = []
    j = 0
    for i in range(n):
        target = days[i] + 30
        while j < n and days[j] < target:
            j += 1
        if j >= n:
            break
        span = days[j] - days[i]
        me = _mean(e[i:j + 1])
        m30.append((me * span, bk[j] - bk[i], me))
    ki_fit, ki_r2 = _ls([x for x, _, _ in m30], [y for _, y, _ in m30]) \
        if m30 else (None, None)
    post = [(dk, me) for _, dk, me in m30 if abs(me) > 0.0025]
    post_hit = (sum(1 for dk, me in post if dk * me > 0) / len(post)) \
        if post else None
    out["law"] = {"kp_fit": kp_fit, "kp_r2": kp_r2, "n1": len(d1),
                  "ki_fit_per_day": ki_fit, "ki_r2": ki_r2, "n30": len(m30),
                  "post_hit_30": post_hit, "post_n": len(post)}

    # -- calm, and the ex-ante forecasts ------------------------------ #
    for h in horizons:
        h = int(h)
        per, nai, obs = [], [], []
        j = 0
        for i in range(n):
            target = days[i] + h
            while j < n and days[j] < target:
                j += 1
            if j >= n:
                break
            lo = max(0, i - h)
            trail = _mean(e[lo:i + 1])
            fc_n = min(kmax, max(kmin, bk[i] + ki_day * h * e[i]))
            fc_o = min(kmax, max(kmin, bk[i] + kp_real * e[i]
                                 + ki_day * h * trail))
            per.append(abs(bk[j] - bk[i]))
            nai.append(abs(bk[j] - fc_n))
            obs.append(abs(bk[j] - fc_o))
        m = len(per)
        if not m:
            continue
        out["h"][h] = {
            "n": m,
            "mae_persist": _mean(per),
            "within_1pt_persist": sum(1 for x in per if x <= 0.01) / m,
            "mae_naive": _mean(nai),
            "mae_observer": _mean(obs),
            "within_1pt_observer": sum(1 for x in obs if x <= 0.01) / m,
        }
    return out


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
    # WP-1: the basket's own side, the markout ledger and K
    # forecastability, measured on the first injection window when there
    # is one (the catalogue's controlled window) and the whole run
    # otherwise.  Measurement only; never part of accept().
    inj = [w for w in stats["excursions"] if w.get("src") in ("raid", "iv")]
    w0 = inj[0] if inj else None
    stats["basket"] = basket_side(frames, w0, pre_days=pre_days)
    stats["markout"] = markout_panel(frames, w0)
    stats["markout_run"] = markout_panel(frames, None) if w0 else None
    stats["kfc"] = k_forecast(frames, meta)
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
    # WP-1 panels.
    print("\nbasket side at end (NAV $M USD / $M baskets, treasury k, "
          "depositor realized $M) and K comprehensibility: the law fitted "
          "(Kp, R2; Ki/day, R2; ex-post 30d direction hit) and calm "
          "(persistence MAE / within 1pt at 30d)")
    print(f"{'name':<18} {'navU':>7} {'navK':>7} {'treas':>7} {'dmReal':>7} "
          f"| {'Kp':>6} {'R2':>5} {'Ki/d':>6} {'R2':>5} {'hit30':>5} "
          f"| {'p30':>6} {'in30':>5} {'obs30':>6}")
    for st in allst:
        print(basket_row(st))
    if any(st.get("markout") for st in allst):
        print("\nmarkout ledger (window when injected, else whole run): "
              "basket fees / adverse total / adverse common-mode $M, worst "
              "carry ratio; whale adverse in basket pools / in BUCK-USDC $M")
        for st in allst:
            print(markout_row(st))
    return worst


def basket_row(st: dict) -> str:
    name = str(st.get("name", ""))[:18]
    e = (st.get("basket") or {}).get("end") or {}
    kfc = st.get("kfc") or {}
    law = kfc.get("law") or {}
    k = kfc.get("h") or {}

    def kf(h, key, spec):
        v = (k.get(h) or k.get(str(h)) or {}).get(key)
        return _f(v, spec)

    return (f"{name:<18} {_f(e.get('nav_usd_m'), '.1f'):>7} "
            f"{_f(e.get('nav_bsk_m'), '.1f'):>7} "
            f"{_f(e.get('treasury_k'), '.0f'):>7} "
            f"{_f(e.get('dm_real_m'), '+.2f'):>7} "
            f"| {_f(law.get('kp_fit'), '.3f'):>6} {_f(law.get('kp_r2'), '.2f'):>5} "
            f"{_f(law.get('ki_fit_per_day'), '.4f'):>6} {_f(law.get('ki_r2'), '.2f'):>5} "
            f"{_f(law.get('post_hit_30'), '.0%'):>5} "
            f"| {kf(30, 'mae_persist', '.4f'):>6} "
            f"{kf(30, 'within_1pt_persist', '.0%'):>5} "
            f"{kf(30, 'mae_observer', '.4f'):>6}")


def markout_row(st: dict) -> str:
    name = str(st.get("name", ""))[:18]
    m = st.get("markout")
    if not m:
        return f"{name:<18} (no ledger in vector)"
    b, w = m["basket"], m["whale"]
    return (f"{name:<18} fees {b['fees_m']:+.3f} adv {b['adv_m']:+.3f} "
            f"cm {b['adv_cm_m']:+.3f} netlp {b['netlp_m']:+.3f} "
            f"carry {_f(b['worst_carry'], '.2f', none='>1')} "
            f"| whale adv basket {w['adv_basket_m']:+.3f} "
            f"ub {w['adv_ub_m']:+.3f}")


# ---------------------------------------------------------------------------
# WP-15: the controller-alternatives grid's metrics (WAVE3.org "Metrics and
# gates"; CARRY-CONVEXITY.org predictions 9 and 10; R14c, R18).  Pure
# functions of a frame list, unit-tested on synthetic series
# (test_d7metrics.py); `d7_panel` gathers them and the wrapped `summarize`
# below carries the panel as stats["d7"] into the catalogue and star
# reports.  Frame fields (snapshot.py): sh_s (the observer's aggregate
# position, 1e18; None under --controller direct), sh_sat / sh_stale /
# sh_excluded / sh_desk_cap / sh_desk_stale / sh_desk_excluded, sh_net (the
# desk's book), ut_absorbed_open / ut_issued_open, fac_drawn, pid_up / pid_ui
# / pid_ud / pid_q / pid_qi / pid_qd (the two loops' terms, 1e18 K units),
# buckK, and the injectors' pu_* / bl_* / lx_* counters.
# ---------------------------------------------------------------------------

HAB_FRAC = 0.10            # |s| within this fraction of its peak = habituated
HAB_HOLD_DAYS = 5          # in-band hold required to call it (a zero crossing
                           # passes through the band without habituating)
P9_TAU_MULT = 1.5          # gate P9: t_hab <= 1.5 tau_s
P9_MAX_SIGN_CHANGES = 1


def _sval(f):
    v = f.get("sh_s")
    return None if v is None else v / E18


def _sign_changes(vals: list[float], band: float) -> int:
    """Transitions between 'clearly positive' (> +band) and 'clearly
    negative' (< -band); values inside the band do not change the state,
    so a decay through zero counts 0 and a limit cycle counts every swing."""
    state = 0
    n = 0
    for v in vals:
        s = 1 if v > band else (-1 if v < -band else 0)
        if s and state and s != state:
            n += 1
        if s:
            state = s
    return n


def habituation(frames: list, end_day: int | None = None,
                tau_s_days: float | None = None, start_day: int | None = None,
                frac: float = HAB_FRAC, hold_days: int = HAB_HOLD_DAYS) -> dict | None:
    """The habituation record on the observer's aggregate position s.

    peak            max |s| over [start_day, horizon] (start_day defaults to
                    end_day, the disturbance's end; None = the whole run)
    t_hab           days after end_day until |s| < frac * peak holds for
                    >= hold_days of data (rectangle coverage); None = never
    residual        |s| at the horizon (the last frame)
    residual_frac   residual / peak
    sign_changes    swings of s between +/- frac * peak after end_day (a
                    limit-cycle detector)
    p9              the gate: t_hab <= P9_TAU_MULT * tau_s and
                    sign_changes <= P9_MAX_SIGN_CHANGES (None without tau_s)
    None when the vector carries no sh_s (a direct-controller cell)."""
    days = _frame_days(frames)
    deltas = _frame_deltas(days)
    idx = [i for i in range(len(frames)) if _sval(frames[i]) is not None]
    if not idx:
        return None
    end = days[idx[0]] if end_day is None else int(end_day)
    start = end if start_day is None else int(start_day)
    win = [i for i in idx if days[i] >= start]
    if not win:
        return None
    peak = max(abs(_sval(frames[i])) for i in win)
    ipk = max(win, key=lambda i: abs(_sval(frames[i])))
    band = frac * peak
    post = [i for i in idx if days[i] >= end]
    t_hab = None
    j = 0
    while j < len(post):
        i = post[j]
        if abs(_sval(frames[i])) >= band:
            j += 1
            continue
        k = j
        while k + 1 < len(post) and abs(_sval(frames[post[k + 1]])) < band:
            k += 1
        if days[post[k]] + deltas[post[k]] - days[i] >= hold_days or peak == 0:
            t_hab = days[i] - end
            break
        j = k + 1
    residual = abs(_sval(frames[idx[-1]]))
    sc = _sign_changes([_sval(frames[i]) for i in post], band)
    p9 = None
    if tau_s_days:
        p9 = (t_hab is not None and t_hab <= P9_TAU_MULT * float(tau_s_days)
              and sc <= P9_MAX_SIGN_CHANGES)
    return {"peak": peak, "peak_day": days[ipk], "end_day": end,
            "t_hab": t_hab, "residual": residual,
            "residual_frac": (residual / peak) if peak else 0.0,
            "sign_changes": sc, "tau_s_days": tau_s_days, "p9": p9,
            "n": len(post)}


def carry(frames: list, day0: int | None = None, day1: int | None = None) -> dict:
    """Int |q_i| dt per facility in BUCK-days (whole BUCK x days) over
    [day0, day1] (defaults: the whole run): the undertakings' open book
    |ut_absorbed_open - ut_issued_open|, the facility's fac_drawn, the
    desk's |sh_net| (None -> 0), and `booked` = |sh_offset| (the
    pseudo-stabilizer's booked sum, which double-counts ut + fac and is
    reported for the observer's view).  The seeder is not booked as a
    position (WAVE3.org decision 17) and has no carry here."""
    days = _frame_days(frames)
    deltas = _frame_deltas(days)
    acc = {"ut": 0.0, "fac": 0.0, "desk": 0.0, "booked": 0.0}
    span = 0
    for i, f in enumerate(frames):
        if day0 is not None and days[i] < day0:
            continue
        if day1 is not None and days[i] > day1:
            continue
        dt = deltas[i]
        span += dt
        acc["ut"] += abs((f.get("ut_absorbed_open") or 0)
                         - (f.get("ut_issued_open") or 0)) / E6 * dt
        acc["fac"] += abs(f.get("fac_drawn") or 0) / E6 * dt
        acc["desk"] += abs(f.get("sh_net") or 0) / E6 * dt
        acc["booked"] += abs(f.get("sh_offset") or 0) / E6 * dt
    total = acc["ut"] + acc["fac"] + acc["desk"]
    return {**acc, "total": total, "days": span,
            "mean_buck": (total / span) if span else 0.0}


def saturation_dwell(frames: list) -> dict:
    """Fractions of the frames (that carry the field) with the level
    saturated (sh_sat >= 1, or the desk's held cap 0), with the stale mask
    set (sh_stale != 0 or the desk stale), with the excluded mask set, and
    with the ladder near its floor (ut_rho < 0.1)."""
    sat = stale = exc = ladder = 0
    n_sat = n_stale = n_exc = n_ladder = 0
    for f in frames:
        if f.get("sh_sat") is not None or f.get("sh_desk_cap") is not None:
            n_sat += 1
            if ((f.get("sh_sat") or 0) >= E18
                    or (f.get("sh_desk_cap") is not None and not f.get("sh_desk_cap"))):
                sat += 1
        if f.get("sh_stale") is not None or f.get("sh_desk_stale") is not None:
            n_stale += 1
            if f.get("sh_stale") or f.get("sh_desk_stale"):
                stale += 1
        if f.get("sh_excluded") is not None or f.get("sh_desk_excluded") is not None:
            n_exc += 1
            if f.get("sh_excluded") or f.get("sh_desk_excluded"):
                exc += 1
        if f.get("ut_rho") is not None:
            n_ladder += 1
            if f.get("ut_rho", 1.0) < 0.1:
                ladder += 1

    def frac(a, n):
        return (a / n) if n else None
    return {"sat_frac": frac(sat, n_sat), "stale_frac": frac(stale, n_stale),
            "excluded_frac": frac(exc, n_exc), "ladder_frac": frac(ladder, n_ladder),
            "sat_frames": sat, "stale_frames": stale, "excluded_frames": exc,
            "n": len(frames)}


def k_economy(frames: list, kmin: float = 0.0, kmax: float = 0.95,
              day0: int | None = None) -> dict:
    """K glides: the total variation of buckK, the max |dK| per day, and
    the rail days, over the frames from day0 on (default: all)."""
    days = _frame_days(frames)
    deltas = _frame_deltas(days)
    sel = [i for i in range(len(frames)) if day0 is None or days[i] >= day0]
    ks = [frames[i].get("buckK", 0) / E18 for i in sel]
    tv = 0.0
    dmax = 0.0
    for a, b in zip(sel, sel[1:]):
        dk = abs(frames[b].get("buckK", 0) - frames[a].get("buckK", 0)) / E18
        tv += dk
        dmax = max(dmax, dk / max(1, days[b] - days[a]))
    rail = sum(deltas[i] for i, k in zip(sel, ks)
               if k <= kmin + RAIL_EPS or k >= kmax - RAIL_EPS)
    return {"tv": tv, "dk_max_per_day": dmax, "rail_days": rail,
            "k_min": min(ks) if ks else None, "k_max": max(ks) if ks else None,
            "n": len(sel)}


def book_loading(frames: list, twin_frames: list | None = None) -> dict | None:
    """The class-9 record: the attacker's par-marked P&L ($M, the last
    frame's bl_pnl), the loaded fraction (bl_loaded_frac), K at the end of
    loading and at the unwind, and -- with the none-twin cell's frames --
    the max |K - K_twin| over the hold (frames with bl_phase 2; the whole
    run after loading when the hold is empty) per unit of loaded fraction.
    Gate P10's first half (P&L <= 0) is `p10_pnl`; its second half (V's
    excursion <= S's) compares two cells and is left to the table.  None
    when the vector carries no loader."""
    if not frames or frames[-1].get("bl_phase") is None:
        return None
    days = _frame_days(frames)
    last = frames[-1]
    pnl_m = (last.get("bl_pnl") or 0) / E6 / 1e6
    frac = float(last.get("bl_loaded_frac") or 0.0)
    hold = [i for i, f in enumerate(frames) if f.get("bl_phase") == 2]
    if not hold:
        hold = [i for i, f in enumerate(frames) if (f.get("bl_phase") or 0) >= 2]
    k_exc = None
    if twin_frames and hold:
        tk = {int(f.get("day", i)): f.get("buckK", 0) / E18
              for i, f in enumerate(twin_frames)}
        diffs = [abs(frames[i].get("buckK", 0) / E18 - tk[days[i]])
                 for i in hold if days[i] in tk]
        k_exc = max(diffs) if diffs else None
    return {"pnl_m": pnl_m, "loaded_frac": frac,
            "loaded_buck_m": (last.get("bl_loaded") or 0) / E6 / 1e6,
            "unwound_buck_m": (last.get("bl_unwound") or 0) / E6 / 1e6,
            "k_load": last.get("bl_k_load"), "k_unwind": last.get("bl_k_unwind"),
            "held_days": last.get("bl_held_days"),
            "phase": last.get("bl_phase"),
            "k_exc_max": k_exc,
            "k_exc_per_frac": (k_exc / frac) if (k_exc is not None and frac > 0) else None,
            "p10_pnl": pnl_m <= 0.0}


def attribution(frames: list) -> dict | None:
    """R14c: per frame, the price loop's (pid_up + pid_ui + pid_ud) and the
    position loop's (pid_q + pid_qi + pid_qd) contributions to dK, and the
    frames where stale / excluded were set; the compact summary is each
    loop's share of the total |dK| (the terms sum to K - K0, so their
    frame-to-frame changes ARE the decomposition of dK; at a rail the
    terms sum to the raw output, WP-13 open issue 8).  None when the
    vector carries no terms (a direct-controller cell)."""
    idx = [i for i, f in enumerate(frames) if f.get("pid_up") is not None]
    if len(idx) < 2:
        return None

    def price(f):
        return (f["pid_up"] + (f.get("pid_ui") or 0) + (f.get("pid_ud") or 0)) / E18

    def pos(f):
        return ((f.get("pid_q") or 0) + (f.get("pid_qi") or 0)
                + (f.get("pid_qd") or 0)) / E18
    tv_p = tv_q = 0.0
    for a, b in zip(idx, idx[1:]):
        tv_p += abs(price(frames[b]) - price(frames[a]))
        tv_q += abs(pos(frames[b]) - pos(frames[a]))
    tot = tv_p + tv_q
    stale = sum(1 for i in idx if frames[i].get("sh_stale") or frames[i].get("sh_desk_stale"))
    exc = sum(1 for i in idx if frames[i].get("sh_excluded") or frames[i].get("sh_desk_excluded"))
    return {"tv_price": tv_p, "tv_pos": tv_q,
            "share_price": (tv_p / tot) if tot else None,
            "share_pos": (tv_q / tot) if tot else None,
            "pos_last": pos(frames[idx[-1]]), "price_last": price(frames[idx[-1]]),
            "stale_frames": stale, "excluded_frames": exc, "n": len(idx)}


def attribution_series(frames: list) -> list[dict]:
    """The per-frame panel: day, dK, dK_price, dK_pos, stale, excluded."""
    out = []
    prev = None
    for f in frames:
        if f.get("pid_up") is None:
            continue
        p = (f["pid_up"] + (f.get("pid_ui") or 0) + (f.get("pid_ud") or 0)) / E18
        q = ((f.get("pid_q") or 0) + (f.get("pid_qi") or 0) + (f.get("pid_qd") or 0)) / E18
        k = f.get("buckK", 0) / E18
        rec = {"day": f.get("day"), "k": k,
               "dk": None if prev is None else k - prev[0],
               "dk_price": None if prev is None else p - prev[1],
               "dk_pos": None if prev is None else q - prev[2],
               "stale": bool(f.get("sh_stale") or f.get("sh_desk_stale")),
               "excluded": bool(f.get("sh_excluded") or f.get("sh_desk_excluded"))}
        out.append(rec)
        prev = (k, p, q)
    return out


def d7_windows(frames: list) -> list[dict]:
    """The WP-15 injectors' windows, in the shape of excursion_windows:
    each guard trip (a frame where pu_pushes rose) as "trip" (day0 = day1),
    the loader's load phase (bl_phase 1 span) as "load", its hold as
    "hold", and the LP exit (lx_exits rose) as "lpexit"; src "d7"."""
    out = []
    days = _frame_days(frames)
    prev_push = prev_exit = 0
    load = hold = None
    for i, f in enumerate(frames):
        pu = f.get("pu_pushes")
        if pu is not None and pu > prev_push:
            out.append({"kind": "trip", "day0": days[i], "day1": days[i], "src": "d7"})
            prev_push = pu
        lx = f.get("lx_exits")
        if lx is not None and lx > prev_exit:
            out.append({"kind": "lpexit", "day0": days[i], "day1": days[i], "src": "d7"})
            prev_exit = lx
        ph = f.get("bl_phase")
        if ph == 1:
            load = (days[i] if load is None else load[0], days[i])
        elif ph == 2:
            hold = (days[i] if hold is None else hold[0], days[i])
    if load:
        out.append({"kind": "load", "day0": load[0], "day1": load[1], "src": "d7"})
    if hold:
        out.append({"kind": "hold", "day0": hold[0], "day1": hold[1], "src": "d7"})
    out.sort(key=lambda w: (w["day0"], w["day1"]))
    return out


def d7_panel(frames: list, meta: dict | None, windows: list | None = None,
             twin_frames: list | None = None,
             tau_s_days: float | None = None) -> dict:
    """The "d7" panel of a cell: habituation after the LAST disturbance
    window (raid, intervention or injector; the whole run when there is
    none), carry over the run, saturation dwell, K economy from the first
    window on, book-loading (with the twin), and the loop attribution.
    tau_s defaults to the deploy's tau_i_days (the position loop's time
    constant is WP-16's; the star's tau_s axis passes its own)."""
    exp = (meta or {}).get("experiment", {}) or {}
    dep = exp.get("deploy", {}) or {}
    kmin = float(dep.get("kmin", 0.0))
    kmax = float(dep.get("kmax", 0.95))
    if tau_s_days is None:
        tau_s_days = float(dep.get("tau_i_days", 90.0))
    wins = [w for w in (windows or []) if w.get("src") in ("raid", "iv", "d7")]
    day0 = min((int(w["day0"]) for w in wins), default=None)
    day1 = max((int(w["day1"]) for w in wins), default=None)
    return {
        "tau_s_days": tau_s_days,
        "disturbance": {"day0": day0, "day1": day1, "n": len(wins)},
        "habituation": habituation(frames, end_day=day1, start_day=day0,
                                   tau_s_days=tau_s_days),
        "carry": carry(frames),
        "carry_post": carry(frames, day0=day0) if day0 is not None else None,
        "dwell": saturation_dwell(frames),
        "k_economy": k_economy(frames, kmin=kmin, kmax=kmax, day0=day0),
        "book_loading": book_loading(frames, twin_frames),
        "attribution": attribution(frames),
    }


_summarize_wp1 = summarize


def summarize(path: str | Path, tail_frac: float = TAIL_FRAC,
              resp_days: int = RESP_DAYS, band: float = EXC_BAND,
              pre_days: int = PRE_DAYS, twin: str | Path | None = None,
              tau_s_days: float | None = None) -> dict:
    """WP-15: the WP-1 summary plus the injectors' windows in the
    excursion block and the "d7" panel; `twin` names the none-twin cell's
    vector for the book-loading excursion, `tau_s_days` the position
    loop's time constant for gate P9."""
    st = _summarize_wp1(path, tail_frac=tail_frac, resp_days=resp_days,
                        band=band, pre_days=pre_days)
    d = json.loads(Path(path).read_text())
    frames = d.get("frames") or []
    if not frames:
        st["d7"] = None
        return st
    meta = d.get("meta", {})
    dep = (meta.get("experiment", {}) or {}).get("deploy", {}) or {}
    kmin = float(dep.get("kmin", 0.0))
    kmax = float(dep.get("kmax", 0.95))
    for w in d7_windows(frames):
        w["resp_end"] = w["day1"] + int(resp_days)
        st["excursions"].append({**w, **excursion_response(
            frames, w, resp_days=resp_days, band=band, pre_days=pre_days,
            kmin=kmin, kmax=kmax)})
    st["excursions"].sort(key=lambda w: (w["day0"], w["day1"]))
    twin_frames = None
    if twin and Path(twin).exists():
        twin_frames = json.loads(Path(twin).read_text()).get("frames") or []
    st["d7"] = d7_panel(frames, meta, st["excursions"], twin_frames=twin_frames,
                        tau_s_days=tau_s_days)
    return st


def d7_row(st: dict) -> str:
    """One line of the d7 panel for a cell (the catalogue / CLI tables)."""
    name = str(st.get("name", ""))[:18]
    p = st.get("d7") or {}
    h = p.get("habituation") or {}
    c = p.get("carry") or {}
    w = p.get("dwell") or {}
    k = p.get("k_economy") or {}
    b = p.get("book_loading") or {}
    a = p.get("attribution") or {}
    hab = ("--" if not h else
           f"t{('never' if h.get('t_hab') is None else h['t_hab'])} "
           f"res{_f(h.get('residual_frac'), '.2f')} sc{h.get('sign_changes')} "
           f"{'' if h.get('p9') is None else ('P9ok' if h['p9'] else 'P9x')}")
    car = f"ut{_f(c.get('ut'), '.3g')} fac{_f(c.get('fac'), '.3g')} desk{_f(c.get('desk'), '.3g')}"
    dw = f"sat{_f(w.get('sat_frac'), '.0%')} stale{_f(w.get('stale_frac'), '.0%')}"
    ke = f"tv{_f(k.get('tv'), '.3f')} dmax{_f(k.get('dk_max_per_day'), '.4f')} rail{_f(k.get('rail_days'), 'd')}"
    bl = ("--" if not b else
          f"pnl{_f(b.get('pnl_m'), '+.3f')} frac{_f(b.get('loaded_frac'), '.2f')} "
          f"kexc{_f(b.get('k_exc_max'), '.4f')} /frac{_f(b.get('k_exc_per_frac'), '.4f')}")
    at = ("--" if not a else
          f"pos{_f(a.get('share_pos'), '.0%')} stale{a.get('stale_frames')} "
          f"exc{a.get('excluded_frames')}")
    return f"{name:<18} hab[{hab}] carry[{car}] dwell[{dw}] K[{ke}] bl[{bl}] attr[{at}]"


_main_wp1 = main


def main(argv=None) -> int:
    """WP-15: the WP-1 CLI, then the d7 panel rows."""
    rc = _main_wp1(argv)
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        opts, paths = _parse_args(args)
    except ValueError:
        return rc
    if not paths or opts["json"]:
        return rc
    print("\nd7 panel (WP-15): habituation of s after the last disturbance "
          "(t_hab days, residual/peak, sign changes, P9), carry BUCK-days, "
          "saturation dwell, K economy, book-loading, loop attribution")
    for p in paths:
        print(d7_row(summarize(p, resp_days=opts["resp_days"], band=opts["band"])))
    return rc


if __name__ == "__main__":
    sys.exit(main())
