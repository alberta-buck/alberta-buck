"""kdesign -- the offline derivation of the position loop's gains (WP-16).

    python -m alberta_buck.sim.kdesign tomls   [--smoke]        # (a) the step-test experiments
    python -m alberta_buck.sim.kdesign launch  [--smoke] [--run] # the detached cells + done markers
    python -m alberta_buck.sim.kdesign fit     [--outdir ...]   # (b) identify the plant from the vectors
    python -m alberta_buck.sim.kdesign solve   [--outdir ...]   # (c) LQR + SIMC + margins -> gains.json / gains.org
    python -m alberta_buck.sim.kdesign all                      # fit then solve

CARRY-CONVEXITY.org D7 and "The position loop (D7)"; WAVE3.org WP-16, decisions
11 / 12 / 17 / 18 / 19; reports/wp-13.org for the controller as built.  numpy
only at runtime; scipy is an optional cross-check in the unit test.

THE REDUCED PLANT (the model of record)
=======================================

Two loops on one lever (BuckKControllerShadow):

    K = K0 + [Kp e + Ki Int(e) + Kd de/dt] + [Kq r + Kqi Int(r) + Kqd dr/dt],
    e = 1 - bvib (ppm),  r = 0 - s (ppm),  s = the observer's aggregate position

States, continuous time, per-day units (h = the design cadence, days):

    e     price error, 1 - bvib (a fraction; the contract carries ppm)
    Ie    its time integral, in fraction * days (the contract: ppm * seconds)
    s     the aggregate position in the mode's units (S: sum lambda_i q_i / D,
          price units; V: sum w_i q_i / cap_i over included stabilizers, a
          fill in [-1, 1]); absorbed POSITIVE, issued NEGATIVE
    Is    its time integral
    d     (optional) the net demand flow the level absorbs, in s-units / day,
          a first-order persistent process with the persistence horizon T_p
    u     K - K_eq: the deviation of K from the level at which the net flow
          is zero.  The controller's output is K - K0 with K0 the resting
          point; the integrators absorb K_eq - K0, so u is what the
          feedback law acts on.

Dynamics:

    de/dt  = -a e   - k_e u  (+ w_e)      the price is SELF-REGULATING: the
                                          cast (arbs, DM agents, savers)
                                          reverts an excursion at rate a;
                                          K moves supply through creditLimit
                                          and a higher K makes BUCK cheaper
                                          (e falls), so the K -> e gain is
                                          -k_e, k_e > 0
    ds/dt  =         k_s u  + d           the position is INTEGRATING: a K
                                          offset moves the net flow the
                                          facilities absorb while they hold
                                          price, ds/dt ~ F / cap; a higher K
                                          issues BUCK that unwinds an issued
                                          book (s rises), so k_s > 0
    dIe/dt = e,  dIs/dt = s
    dd/dt  = -d / T_p                     (when the flow state is carried)

Discrete time at the cadence h, ZOH on u (phi = exp(-a h), b = k_e (1 - phi) / a):

    e[k+1]  = phi e[k] - b u[k]
    Ie[k+1] = Ie[k] + h e[k+1]            the contract's newI = I + err * dt
                                          uses the CURRENT error, so the
                                          integral state at a cycle includes
                                          that cycle's sample (feedthrough)
    s[k+1]  = s[k] + k_s h u[k] + h d[k]
    Is[k+1] = Is[k] + h s[k+1]
    d[k+1]  = rho d[k],  rho = exp(-h / T_p)

    x[k+1] = A x[k] + B u[k] + G d[k],  x = [e, Ie, s, Is] (or [e, Ie, s, Is, d])

The three plant parameters are IDENTIFIED from step tests in the real sim
(`fit`): a and k_e from the price response, k_s from the books' response to a
K step.  k_s is identified on the raw pseudo-stabilizer book q (BUCK / day per
unit K, F_K) and converted to each mode's units through the full-book value
of s, sigma_mode (S: cap_off / D; V: w_off / (w_off + w_desk), the desk's
empty book included at weight 1 -- decision 18): k_s = F_K / (cap_off) *
sigma_mode.  In FILL units (s / sigma) the S and V problems are identical and
the per-mode gains are the fill-unit gains divided by sigma_mode.

Assumptions (each one is a finding to check against the cells):

  1. Linear, no deadband: the facilities absorb in proportion to the flow
     whatever the band; the identified k_s is the average over the test.
  2. First-order price reversion with one rate a (the cast's fast reversion),
     no dead time from K to supply (a lag would show as a poor fit of the
     first-order step response; reported).
  3. The K -> s channel is integrating on the horizon of interest (a finite
     re-levering of the existing debtors would make it self-regulating; both
     fits are reported and the ramp's initial slope is k_s either way).
  4. No coupling from s back into e (the facilities hold price; the price
     the controller reads is the raw basket, decision 10) and none from e
     into s beyond the flow K induces; the demand disturbance enters s.
  5. The desk's own book is empty on the none arm (it is in the S sum at
     lambda 1 and in the V denominator at weight 1).

THE JOINT PLANT IS NOT STABILIZABLE (finding)
=============================================

One lever drives both integrals through one flow, so the combination

    z = Ie + (h phi / (1 - phi)) e + (k_e / (a k_s)) s     (-> a Ie + e + (k_e / k_s) s)

is CONSERVED by the plant for any input (z' A = z', z' B = 0): the price
integral and the position carry the same information -- the impact-scaled
cumulative net flow -- and cannot both be zeroed.  A joint LQR that costs
both integrals has no finite solution: the value iteration's gains drift
with the horizon (the price gains collapse as the position weight wins), the
Riccati iterate grows without bound.  This is the linear shadow of D7's
lever count ("two integrators on one lever wind against each other"); what
makes the two loops compatible in the real system is the BAND: while a
facility holds price the price loop sees a constant error and s carries the
flow, and once the book is drained the price relaxes inside the band and s
is frozen.  So the reduced plant is regime-switched, and the derivation is
REGIME-WISE, the standard mid-ranging design (Shinskey): each loop's LQR on
its own regime's plant, in one currency, then the joint closed loop
analysed for its eigenvalues and margins.  In the joint closed loop the
conserved mode survives as an eigenvalue at exactly 1 with eigenvector
(0, Kqi, 0, -Ki): the two integrators' contents can shift between them
without moving K (benign in K, but their difference is unbounded in the
linear model; the sim's band and rails bound it -- a K-economy metric for
WP-15's attribution panel).

THE COST AND THE FEEDBACK
=========================

    price loop    J_e = sum_k h [ q_e e^2 + q_Ie Ie^2 + r u^2 ]          x = [e, Ie]
    position loop J_s = sum_k h [ w c (q_s s^2 + q_Is Is^2) + r u^2 ]    x = [s, Is (, d)]

Bryson's rule on the design's OWN constants (experiment.DEFAULTS["deploy"]):

    r    = 1 / dk_rail^2                 a full-authority K move costs 1
    q_Ie = 1 / (e_max tau_I)^2           a sustained e_max for tau_I costs 1;
                                         in continuous time the LQR then gives
                                         Ki = sqrt(q_Ie / r) = dk_rail /
                                         (e_max tau_I), the sim's own
                                         derivation (deploy.py) EXACTLY --
                                         the Ki anchor is structural
    q_e  = calibrated: the weight at which the price loop's LQR Kp equals the
                                         sim's kp_frac dk_rail / e_max
                                         (inverse LQR); 0 when no non-negative
                                         weight reaches it (then the Kp
                                         anchor reports the miss, which is a
                                         statement about the identified a)
    q_s  = 1 / sigma^2                   a full book costs 1 per (day) ...
    q_Is = 1 / (sigma T_p)^2             ... and a full book held for the
                                         persistence horizon costs 1
    w    the overall carry weight (the carry cost of a full book relative to
         a full-authority K move), EXPOSED; c the carry class of decision 11
         (desk 1, undertakings 1 per side, facility 0.5, seeder 0.25; the
         lumped pseudo-stabilizer is the undertakings, c = 1)

u = -L x in each loop.  With the controller's sign convention (both loops are
PIDs on an ERROR with setpoints par and zero, decision 19):

    Kp = -L_e,  Ki = -L_Ie,  Kq = +L_s,  Kqi = +L_Is,  Kqd from L_d (below)

in per-day units; the contract takes Ki and Kqi per SECOND (/ 86400), Kqd per
(s / second) (x 86400), and every gain real * 1e12.  In continuous time the
position loop's LQR is explicit: Kqi = sqrt(w c q_Is / r) = sqrt(w c) dk_rail
/ (sigma T_p), Kq = sqrt(2 Kqi / k_s + w c q_s / r).  The inflow feedforward:
with the flow state carried, the LQR returns L_d, the feedback on the
disturbance; the controller sees only ds/dt = k_s u + d, so realising L_d
through the D term gives Kqd = L_d / (1 - L_d k_s) and inflates the other
position gains by the same 1 / (1 - L_d k_s).  The table of record sets
Kqd = 0 (SIMC tunes an integrating process with PI; a D term on a lumpy book
is a source of K jerk) and reports the implied value.

SIMC (Skogestad) for the integrating position loop with the closed-loop time
constant tau_s: Kq = 1 / (k_s tau_s), Kqi = Kq / (4 tau_s).  The LQR's implied
tau_s is 1 / (k_s Kq_LQR); its damping is zeta = k_s Kq / (2 sqrt(k_s Kqi))
(SIMC: 1; the LQR at q_s = 0: 1 / sqrt 2).  The carry weight of record is the
one whose LQR tau_s is the geometric mean of the bounds tau_I and T_p (the
range the design states: slower than the price loop's slow mode, faster than
the persistence horizon), and the two-point tau_s axis {0.5, 2} x tau_s
scales the LQR gains by SIMC's laws (Kq ~ 1 / tau, Kqi ~ 1 / tau^2), which
move the loop's time scale and keep its damping.

The JOINT closed loop (the 4- or 5-state plant under both loops' gains) is
then checked: its eigenvalues (the structural unit eigenvalue set aside),
the slowest mode, and the gain and phase margins of the loop broken at K.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

from alberta_buck.sim.experiment import DEFAULTS, derive_gains

DAY = 86400.0
E6 = 10 ** 6
E18 = 10 ** 18
REPO = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO / "alberta_buck" / "sim" / "experiments"
OUTDIR = REPO / "build" / "sim" / "kdesign"

# The persistence horizon T_p (days): the duration a full book may stand
# before the level-2 test converts it to an outright operation.  The ops doc's
# rule is 30 consecutive days past the leash for the PRICE (silenced under D7
# by the facilities' own bids, ops doc "runaway one"); a book-based dwell is
# undecided (report, open issue).  Taken as 2 tau_I: a flow K has not owned
# after two integral times is structural.
T_PERSIST_DAYS = 180.0
TAU_I_DAYS = float(DEFAULTS["deploy"]["tau_i_days"])
H_DESIGN_DAYS = 0.25          # the sim's controller cadence: one cycle per tick
K_HOLD = 0.75                 # the open-loop hold level (the deploy's k0)
STEP_DAY_HOLD = 120           # gains off, K held at K_HOLD (the cast has settled)
STEP_DAY = 150                # the K step: 215 days of open-loop response
# The K channel is the debtors' arrival hazard (k / k_ref) and their K-scaled
# limits: slow and lumpy (a 40-day smoke showed ~100 BUCK of supply per 0.1 K
# in ten days), so the steps reach the rails and the response window is long.
STEPS_D10 = {"m20": -0.20, "m10": -0.10, "m05": -0.05, "p05": 0.05, "p10": 0.10, "p20": 0.20}
STEPS_D40 = {"m20": -0.20, "p20": 0.20}
CARRY_CLASSES = {"desk": 1.0, "undertakings": 1.0, "facility": 0.5, "seeder": 0.25}


# ---------------------------------------------------------------------------
# The plant
# ---------------------------------------------------------------------------

@dataclass
class Plant:
    """The reduced plant in one mode's s units, per-day rates."""
    a: float            # price reversion rate, 1/day
    k_e: float          # K -> e gain, fraction/day per unit K (enters as -k_e)
    k_s: float          # K -> s gain, s-units/day per unit K
    t_flow: float = T_PERSIST_DAYS   # persistence of the flow disturbance, days

    def discretize(self, h: float, flow_state: bool = False):
        """(A, B, G) at cadence h days; x = [e, Ie, s, Is] (+ [d])."""
        if self.a > 0:
            phi = math.exp(-self.a * h)
            b = self.k_e * (1.0 - phi) / self.a
        else:
            phi, b = 1.0, self.k_e * h
        n = 5 if flow_state else 4
        A = np.zeros((n, n))
        A[0, 0] = phi
        A[1, 0] = h * phi
        A[1, 1] = 1.0
        A[2, 2] = 1.0
        A[3, 2] = h
        A[3, 3] = 1.0
        B = np.zeros((n, 1))
        B[0, 0] = -b
        B[1, 0] = -h * b
        B[2, 0] = self.k_s * h
        B[3, 0] = self.k_s * h * h
        G = np.zeros((n, 1))
        G[2, 0] = h
        G[3, 0] = h * h
        if flow_state:
            rho = math.exp(-h / self.t_flow) if self.t_flow > 0 else 0.0
            A[2, 4] = h
            A[3, 4] = h * h
            A[4, 4] = rho
            G = np.zeros((n, 1))
            G[4, 0] = 1.0
        return A, B, G


# ---------------------------------------------------------------------------
# LQR by Riccati iteration (numpy only)
# ---------------------------------------------------------------------------

def dare_value_iteration(A, B, Q, R, tol: float = 1e-13, max_iter: int = 2_000_000):
    """The discrete algebraic Riccati equation by value iteration from P = Q:
    P <- Q + A'PA - A'PB (R + B'PB)^-1 B'PA until the relative change is
    below tol -- the finite-horizon cost-to-go, one step per iteration.
    Exact but slow for the slow modes (a 180-day mode at h = 0.25 needs
    ~1e6 steps); the cross-check for dare() in the unit test."""
    A = np.asarray(A, float)
    B = np.asarray(B, float)
    Q = np.asarray(Q, float)
    R = np.atleast_2d(np.asarray(R, float))
    P = Q.copy()
    for it in range(1, max_iter + 1):
        BtP = B.T @ P
        S = R + BtP @ B
        K = np.linalg.solve(S, BtP @ A)
        Pn = Q + A.T @ P @ (A - B @ K)
        Pn = 0.5 * (Pn + Pn.T)
        scale = max(1.0, float(np.max(np.abs(Pn))))
        if float(np.max(np.abs(Pn - P))) <= tol * scale:
            return Pn, it
        P = Pn
    raise RuntimeError(f"dare: no convergence in {max_iter} iterations")


def dare(A, B, Q, R, tol: float = 1e-11, max_iter: int = 120):
    """The discrete algebraic Riccati equation by the structure-preserving
    DOUBLING algorithm: with A_0 = A, G_0 = B R^-1 B', H_0 = Q,

        A_{k+1} = A_k (I + G_k H_k)^-1 A_k
        G_{k+1} = G_k + A_k (I + G_k H_k)^-1 G_k A_k'
        H_{k+1} = H_k + A_k' H_k (I + G_k H_k)^-1 A_k

    H_k is the cost-to-go after 2^k steps of the value iteration, so sixty
    iterations cover 1e18 steps and the slow position modes converge in
    milliseconds.  Converges for a stabilizable pair with the costed modes
    detectable; an uncosted decoupled block (the position loop at weight 0)
    keeps H = 0 there, the decoupled solution, while its A_k / G_k blocks
    grow harmlessly (they never touch the costed block's update)."""
    A = np.asarray(A, float)
    B = np.asarray(B, float)
    Q = np.asarray(Q, float)
    R = np.atleast_2d(np.asarray(R, float))
    n = A.shape[0]
    I = np.eye(n)
    Ak = A.copy()
    Gk = B @ np.linalg.solve(R, B.T)
    Hk = Q.copy()
    for it in range(1, max_iter + 1):
        M = np.linalg.solve(I + Gk @ Hk, Ak)          # (I + G H)^-1 A
        Hn = Hk + Ak.T @ Hk @ M
        Hn = 0.5 * (Hn + Hn.T)
        Gn = Gk + Ak @ np.linalg.solve(I + Gk @ Hk, Gk) @ Ak.T
        An = Ak @ M
        if not np.all(np.isfinite(Hn)):
            break
        scale = max(1.0, float(np.max(np.abs(Hn))))
        if float(np.max(np.abs(Hn - Hk))) <= tol * scale:
            return Hn, it
        Ak, Gk, Hk = An, Gn, Hn
    return dare_value_iteration(A, B, Q, R)


def lqr(A, B, Q, R):
    """u = -L x minimising sum x'Qx + u'Ru; returns (L (1 x n), P, iterations)."""
    P, it = dare(A, B, Q, R)
    B = np.asarray(B, float)
    S = np.atleast_2d(np.asarray(R, float)) + B.T @ P @ B
    L = np.linalg.solve(S, B.T @ P @ np.asarray(A, float))
    return L, P, it


# ---------------------------------------------------------------------------
# The cost weights
# ---------------------------------------------------------------------------

@dataclass
class Design:
    """The design constants the weights are scaled to (the sim's deploy
    defaults) and the WP-16 choices."""
    dk_rail: float = float(DEFAULTS["deploy"]["dk_rail"])
    e_max: float = float(DEFAULTS["deploy"]["e_max"])
    tau_i_days: float = TAU_I_DAYS
    kp_frac: float = float(DEFAULTS["deploy"]["kp_frac"])
    t_persist_days: float = T_PERSIST_DAYS
    carry_class: float = CARRY_CLASSES["undertakings"]
    h_days: float = H_DESIGN_DAYS

    def sim_gains(self) -> tuple[float, float]:
        """(Kp real, Ki real per second): the sim's own derivation, exact."""
        kp, ki, _kd = derive_gains(dk_rail=self.dk_rail, e_max=self.e_max,
                                   tau_i_days=self.tau_i_days,
                                   kp_frac=self.kp_frac)
        return kp / 1e12, ki / 1e12


@dataclass
class Weights:
    q_e: float
    q_Ie: float
    q_s: float
    q_Is: float
    r: float
    w_carry: float = 0.0
    sigma: float = 1.0          # the full-book value of s in the mode's units

    def Q(self, flow_state: bool = False):
        d = [self.q_e, self.q_Ie, self.w_carry * self.q_s, self.w_carry * self.q_Is]
        if flow_state:
            d.append(0.0)
        return np.diag(d)


def bryson_weights(des: Design, sigma: float = 1.0, w_carry: float = 0.0,
                   q_e: float | None = None) -> Weights:
    """The weights on the design's own scales (module docstring); q_e is
    filled by calibrate_qe unless given."""
    r = 1.0 / des.dk_rail ** 2
    q_Ie = 1.0 / (des.e_max * des.tau_i_days) ** 2
    q_s = des.carry_class / sigma ** 2
    q_Is = des.carry_class / (sigma * des.t_persist_days) ** 2
    q_e_bryson = 1.0 / des.e_max ** 2
    return Weights(q_e=q_e_bryson if q_e is None else q_e, q_Ie=q_Ie,
                   q_s=q_s, q_Is=q_Is, r=r, w_carry=w_carry, sigma=sigma)


def price_loop_gains(plant: Plant, w: Weights, h: float) -> tuple[float, float]:
    """(Kp, Ki per day) of the price loop alone (position weight 0)."""
    w0 = Weights(w.q_e, w.q_Ie, w.q_s, w.q_Is, w.r, 0.0, w.sigma)
    A, B, _G = plant.discretize(h)
    L, _P, _it = lqr(A, B, w0.Q(), w0.r)
    return -float(L[0, 0]), -float(L[0, 1])


def calibrate_qe(plant: Plant, w: Weights, des: Design, h: float) -> tuple[float, bool]:
    """The price-error weight at which the LQR's Kp equals the sim's (the
    inverse LQR); Kp is monotone in q_e.  Returns (q_e, feasible)."""
    kp_target, _ki = des.sim_gains()

    def kp_at(qe):
        return price_loop_gains(plant, Weights(qe, w.q_Ie, w.q_s, w.q_Is, w.r,
                                               0.0, w.sigma), h)[0]
    if kp_at(0.0) >= kp_target:
        return 0.0, False
    lo, hi = 0.0, 1.0 / des.e_max ** 2
    while kp_at(hi) < kp_target:
        hi *= 4.0
        if hi > 1e12:
            return hi, False
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if kp_at(mid) < kp_target:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-12 * max(1.0, hi):
            break
    return 0.5 * (lo + hi), True


# ---------------------------------------------------------------------------
# Gains, units, SIMC, margins
# ---------------------------------------------------------------------------

def gains_from_L(L, k_s: float, flow_state: bool = False) -> dict:
    """Map u = -L x onto the controller's (Kp, Ki, Kq, Kqi, Kqd), per-day
    units, in the sign convention of decision 19 (r = 0 - s)."""
    L = np.asarray(L, float).ravel()
    g = {"Kp": -float(L[0]), "Ki_day": -float(L[1]),
         "Kq": float(L[2]), "Kqi_day": float(L[3]), "Kqd_day": 0.0,
         "L_d": 0.0, "d_inflation": 1.0}
    if flow_state and len(L) > 4:
        ld = float(L[4])
        infl = 1.0 / (1.0 - ld * k_s)
        g["L_d"] = ld
        g["d_inflation"] = infl
        g["Kqd_day"] = ld * infl
    return g


def to_contract_units(g: dict) -> dict:
    """Per-day gains -> the contract's real units (per second) and the
    real * 1e12 integers it stores."""
    ki = g["Ki_day"] / DAY
    kqi = g["Kqi_day"] / DAY
    kqd = g["Kqd_day"] * DAY
    return {"Kp": g["Kp"], "Ki": ki, "Kq": g["Kq"], "Kqi": kqi, "Kqd": kqd,
            "Kp_x1e12": int(round(g["Kp"] * 1e12)),
            "Ki_x1e12": int(round(ki * 1e12)),
            "Kq_x1e12": int(round(g["Kq"] * 1e12)),
            "Kqi_x1e12": int(round(kqi * 1e12)),
            "Kqd_x1e12": int(round(kqd * 1e12))}


def real_from_x1e12(x: int) -> float:
    return x / 1e12


def scale_gains_between_modes(g: dict, sigma_from: float, sigma_to: float) -> dict:
    """Position gains in one mode's units -> another's (the same books read
    sigma_to / sigma_from as large): Kq scales by sigma_from / sigma_to."""
    f = sigma_from / sigma_to
    out = dict(g)
    for k in ("Kq", "Kqi_day", "Kqd_day"):
        if k in out:
            out[k] = out[k] * f
    return out


def simc_integrating(k_s: float, tau_s: float) -> tuple[float, float]:
    """SIMC for ds/dt = k_s u (no dead time): Kq = 1/(k_s tau_s), Kqi =
    Kq / (4 tau_s) (tau_I = 4 tau_c); per-day units."""
    kq = 1.0 / (k_s * tau_s)
    return kq, kq / (4.0 * tau_s)


def implied_tau_s(k_s: float, kq: float) -> float:
    """The closed-loop time constant SIMC's Kq formula implies for a gain."""
    return 1.0 / (k_s * kq)


def position_loop_damping(k_s: float, kq: float, kqi: float) -> float:
    """zeta of s'' + k_s Kq s' + k_s Kqi s = 0 (SIMC: 1; LQR at q_s = 0: 1/sqrt 2)."""
    return k_s * kq / (2.0 * math.sqrt(k_s * kqi)) if kqi > 0 else float("inf")


def tau_s_axis(g: dict, tau_s: float, factors=(0.5, 2.0)) -> dict:
    """The two-point tau_s axis: the LQR gains rescaled by SIMC's laws
    (Kq ~ 1/tau, Kqi ~ 1/tau^2, Kqd ~ 1) to f x tau_s."""
    out = {}
    for f in factors:
        out[f"{f:g}"] = {"tau_s_days": tau_s * f, "Kq": g["Kq"] / f,
                         "Kqi_day": g["Kqi_day"] / f ** 2, "Kqd_day": g["Kqd_day"]}
    return out


def closed_loop(A, B, L, h: float) -> dict:
    """Eigenvalues of A - B L: moduli, the slowest mode's time constant
    (days) and the margin to the unit circle."""
    Acl = np.asarray(A, float) - np.asarray(B, float) @ np.asarray(L, float)
    ev = np.linalg.eigvals(Acl)
    mod = np.abs(ev)
    taus = [(-h / math.log(m)) if 0 < m < 1 else float("inf") for m in mod]
    return {"eig": [complex(z) for z in ev], "max_modulus": float(mod.max()),
            "unit_circle_margin": float(1.0 - mod.max()),
            "slowest_tau_days": float(max(t for t in taus)),
            "stable": bool(mod.max() < 1.0)}


def margins(A, B, L, h: float, n: int = 20000) -> dict:
    """Gain and phase margins of the loop broken at the plant input,
    G(z) = L (zI - A)^-1 B with negative feedback, on a log grid of
    frequencies up to the Nyquist rate."""
    A = np.asarray(A, float)
    B = np.asarray(B, float)
    L = np.asarray(L, float)
    nI = np.eye(A.shape[0])
    w = np.logspace(-6, math.log10(math.pi / h), n)
    gain = np.empty(n)
    phase = np.empty(n)
    for i, wi in enumerate(w):
        z = complex(math.cos(wi * h), math.sin(wi * h))
        Gz = (L @ np.linalg.solve(z * nI - A, B))[0, 0]
        gain[i] = abs(Gz)
        phase[i] = math.degrees(math.atan2(Gz.imag, Gz.real))
    # unwrap the phase (continuous in frequency)
    ph = np.degrees(np.unwrap(np.radians(phase)))
    # gain crossover: the last frequency where |G| crosses 1 from above
    pm = None
    wc = None
    for i in range(n - 1):
        if (gain[i] - 1.0) * (gain[i + 1] - 1.0) <= 0 and gain[i] != gain[i + 1]:
            t = (1.0 - gain[i]) / (gain[i + 1] - gain[i])
            pc = ph[i] + t * (ph[i + 1] - ph[i])
            wc = w[i] + t * (w[i + 1] - w[i])
            pm = 180.0 + pc
    # phase crossover: |G| where the phase crosses -180
    gm = None
    wg = None
    for i in range(n - 1):
        if (ph[i] + 180.0) * (ph[i + 1] + 180.0) <= 0 and ph[i] != ph[i + 1]:
            t = (-180.0 - ph[i]) / (ph[i + 1] - ph[i])
            g = gain[i] + t * (gain[i + 1] - gain[i])
            wg = w[i] + t * (w[i + 1] - w[i])
            gm = (1.0 / g) if g > 0 else float("inf")
            break
    return {"phase_margin_deg": pm, "gain_crossover_rad_per_day": wc,
            "gain_margin": gm, "phase_crossover_rad_per_day": wg}


# ---------------------------------------------------------------------------
# One design: a plant in FILL units -> the table row for a mode
# ---------------------------------------------------------------------------

def conserved_direction(plant: Plant, h: float) -> np.ndarray:
    """The row vector z with z A = z and z B = 0 on the 4-state plant: the
    combination of the price integral, the price and the position that no
    input can move (the finding in the module docstring)."""
    phi = math.exp(-plant.a * h) if plant.a > 0 else 1.0
    beta = h * phi / (1.0 - phi) if phi < 1 else h
    gamma = plant.k_e / (plant.a * plant.k_s) if plant.a > 0 else plant.k_e * h / (plant.k_s * h)
    return np.array([beta, 1.0, gamma, 0.0])


def position_plant(k_s: float, h: float, t_flow: float = T_PERSIST_DAYS,
                   flow_state: bool = False):
    """The position loop's own plant: x = [s, Is] (+ [d]); the integrating
    regime with the facility holding price."""
    n = 3 if flow_state else 2
    A = np.zeros((n, n))
    A[0, 0] = 1.0
    A[1, 0] = h
    A[1, 1] = 1.0
    B = np.zeros((n, 1))
    B[0, 0] = k_s * h
    B[1, 0] = k_s * h * h
    if flow_state:
        A[0, 2] = h
        A[1, 2] = h * h
        A[2, 2] = math.exp(-h / t_flow) if t_flow > 0 else 0.0
    return A, B


def position_loop_gains(k_s: float, w: Weights, h: float, t_flow: float = T_PERSIST_DAYS,
                        flow_state: bool = False) -> dict:
    """(Kq, Kqi per day, Kqd per day, L_d) of the position loop's LQR on
    its own plant, with the carry weight applied."""
    A, B = position_plant(k_s, h, t_flow, flow_state)
    Q = np.diag([w.w_carry * w.q_s, w.w_carry * w.q_Is] + ([0.0] if flow_state else []))
    L, _P, it = lqr(A, B, Q, w.r)
    L = L.ravel()
    g = {"Kq": float(L[0]), "Kqi_day": float(L[1]), "Kqd_day": 0.0, "L_d": 0.0,
         "d_inflation": 1.0, "dare_iterations": it}
    if flow_state:
        ld = float(L[2])
        infl = 1.0 / (1.0 - ld * k_s)
        g.update({"L_d": ld, "d_inflation": infl, "Kqd_day": ld * infl})
    return g


def carry_weight_for_tau(k_s: float, w: Weights, h: float, tau_target: float,
                         t_flow: float = T_PERSIST_DAYS, flow_state: bool = False) -> float:
    """The carry weight at which the position loop's implied tau_s equals
    the target (tau_s falls monotonically with the weight); bisection in
    log w."""
    def tau_at(wc):
        wx = Weights(w.q_e, w.q_Ie, w.q_s, w.q_Is, w.r, wc, w.sigma)
        g = position_loop_gains(k_s, wx, h, t_flow, flow_state)
        return implied_tau_s(k_s, g["Kq"]) if g["Kq"] > 0 else float("inf")
    lo, hi = -14.0, 8.0          # log10 w
    if tau_at(10 ** hi) > tau_target:
        return 10 ** hi
    if tau_at(10 ** lo) < tau_target:
        return 10 ** lo
    for _ in range(120):
        mid = 0.5 * (lo + hi)
        if tau_at(10 ** mid) > tau_target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-9:
            break
    return 10 ** (0.5 * (lo + hi))


def joint_closed_loop(plant: Plant, g: dict, h: float, flow_state: bool = False) -> dict:
    """Both loops on the joint plant: L = [-Kp, -Ki, Kq, Kqi (, L_d)] in the
    plant's units; the eigenvalues with the structural unit eigenvalue (the
    integrators' redundancy, eigenvector (0, Kqi, 0, -Ki)) set aside, the
    slowest remaining mode, and the margins of the loop broken at K."""
    A, B, _G = plant.discretize(h, flow_state)
    L = np.array([[-g["Kp"], -g["Ki_day"], g["Kq"], g["Kqi_day"]] + ([g.get("L_d", 0.0)] if flow_state else [])])
    Acl = A - B @ L
    ev = np.linalg.eigvals(Acl)
    # set aside the eigenvalue nearest 1 (the conserved / redundancy mode)
    idx = int(np.argmin(np.abs(ev - 1.0)))
    unit = ev[idx]
    rest = np.delete(ev, idx)
    mod = np.abs(rest)
    taus = [(-h / math.log(m)) if 0 < m < 1 else float("inf") for m in mod]
    mg = margins(A, B, L, h)
    return {"eig": [[z.real, z.imag] for z in ev],
            "structural_unit_eig": [unit.real, unit.imag],
            "structural_unit_eig_dev": float(abs(unit - 1.0)),
            "max_modulus": float(mod.max()), "unit_circle_margin": float(1.0 - mod.max()),
            "slowest_tau_days": float(max(taus)), "stable": bool(mod.max() < 1.0),
            "margins": mg}


def design_mode(plant_fill: Plant, sigma: float, des: Design,
                w_carry: float | None = None, tau_target: float | None = None,
                flow_state: bool = True) -> dict:
    """The gain table for one mode: the plant in fill units and the mode's
    full-book value sigma.  Regime-wise: the price loop's LQR on [e, Ie]
    (the anchor), the position loop's on [s, Is (, d)] in fill units (S and
    V are one problem there), mapped to the mode's units by 1 / sigma; then
    the joint closed loop's eigenvalues and margins."""
    h = des.h_days
    w = bryson_weights(des, sigma=1.0, w_carry=0.0)
    q_e, feasible = calibrate_qe(plant_fill, w, des, h)
    w = Weights(q_e, w.q_Ie, w.q_s, w.q_Is, w.r, 0.0, 1.0)
    # the price loop (== the joint LQR at position weight 0): the anchor
    kp0, ki0 = price_loop_gains(plant_fill, w, h)
    kp_sim, ki_sim = des.sim_gains()
    anchor = {"Kp": kp0, "Ki": ki0 / DAY, "Kp_sim": kp_sim, "Ki_sim": ki_sim,
              "Kp_ratio": kp0 / kp_sim, "Ki_ratio": (ki0 / DAY) / ki_sim,
              "q_e": q_e, "q_e_bryson": 1.0 / des.e_max ** 2,
              "q_e_feasible": feasible,
              "pass": abs(kp0 / kp_sim - 1) <= 0.25 and abs((ki0 / DAY) / ki_sim - 1) <= 0.25}
    # the position loop on its own plant, in fill units
    if w_carry is None:
        if tau_target is None:
            tau_target = math.sqrt(des.tau_i_days * des.t_persist_days)
        w_carry = carry_weight_for_tau(plant_fill.k_s, w, h, tau_target, plant_fill.t_flow, flow_state)
    wc = Weights(q_e, w.q_Ie, w.q_s, w.q_Is, w.r, w_carry, 1.0)
    gp = position_loop_gains(plant_fill.k_s, wc, h, plant_fill.t_flow, flow_state)
    g_fill = {"Kp": kp0, "Ki_day": ki0, **gp}
    # the mode's units: s_mode = sigma * s_fill, k_s_mode = sigma * k_s_fill
    g = scale_gains_between_modes(g_fill, 1.0, sigma)
    g["L_d"] = g_fill["L_d"] / sigma
    k_s_mode = plant_fill.k_s * sigma
    tau_s = implied_tau_s(k_s_mode, g["Kq"])
    zeta = position_loop_damping(k_s_mode, g["Kq"], g["Kqi_day"])
    kq_simc, kqi_simc = simc_integrating(k_s_mode, tau_s)
    plant_mode = Plant(plant_fill.a, plant_fill.k_e, k_s_mode, plant_fill.t_flow)
    cl = joint_closed_loop(plant_mode, g, h, flow_state)
    zdir = conserved_direction(plant_mode, h)
    # the position loop alone: its own closed loop (the SIMC comparison's frame)
    Ap, Bp = position_plant(k_s_mode, h, plant_fill.t_flow, flow_state)
    Lp = np.array([[g["Kq"], g["Kqi_day"]] + ([g["L_d"]] if flow_state else [])])
    clp = closed_loop(Ap, Bp, Lp, h)
    # sensitivity of tau_s to the carry weight
    sens = {}
    for f in (0.1, 0.3, 1.0, 3.0, 10.0):
        wx = Weights(q_e, w.q_Ie, w.q_s, w.q_Is, w.r, w_carry * f, 1.0)
        gx = scale_gains_between_modes(position_loop_gains(plant_fill.k_s, wx, h, plant_fill.t_flow, flow_state), 1.0, sigma)
        sens[f"{f:g}"] = {"w_carry": w_carry * f,
                          "tau_s_days": implied_tau_s(k_s_mode, gx["Kq"]),
                          "zeta": position_loop_damping(k_s_mode, gx["Kq"], gx["Kqi_day"]),
                          "Kq": gx["Kq"], "Kqi_day": gx["Kqi_day"]}
    row = {
        "sigma": sigma, "h_days": h, "flow_state": flow_state,
        "plant": {"a": plant_fill.a, "k_e": plant_fill.k_e,
                  "k_s_fill": plant_fill.k_s, "k_s": k_s_mode,
                  "t_flow_days": plant_fill.t_flow,
                  "conserved_direction": [float(x) for x in zdir]},
        "weights": {"q_e": q_e, "q_Ie": wc.q_Ie, "q_s": wc.q_s, "q_Is": wc.q_Is,
                    "r": wc.r, "w_carry": w_carry, "carry_class": des.carry_class,
                    "t_persist_days": des.t_persist_days, "tau_target_days": tau_target},
        "anchor": anchor,
        "lqr": {**g, **to_contract_units(g), "tau_s_days": tau_s, "zeta": zeta,
                "s0_equivalent_lambda": g["Kq"] / kp_sim,
                "dare_iterations": gp["dare_iterations"],
                "position_loop_alone": {k: v for k, v in clp.items() if k != "eig"}},
        "simc": {"tau_s_days": tau_s, "Kq": kq_simc, "Kqi_day": kqi_simc,
                 "Kqi": kqi_simc / DAY, "Kq_x1e12": int(round(kq_simc * 1e12)),
                 "Kqi_x1e12": int(round(kqi_simc / DAY * 1e12)),
                 "Kqi_lqr_over_simc": g["Kqi_day"] / kqi_simc if kqi_simc else None},
        "tau_s_axis": {k: {**v, "Kqi": v["Kqi_day"] / DAY,
                           "Kq_x1e12": int(round(v["Kq"] * 1e12)),
                           "Kqi_x1e12": int(round(v["Kqi_day"] / DAY * 1e12))}
                       for k, v in tau_s_axis(g, tau_s).items()},
        "tau_s_in_range": des.tau_i_days <= tau_s <= des.t_persist_days,
        "closed_loop": {k: v for k, v in cl.items() if k != "margins"},
        "margins": cl["margins"],
        "carry_sensitivity": sens,
    }
    return row


# ---------------------------------------------------------------------------
# The step-test experiments
# ---------------------------------------------------------------------------

def _none_physics() -> str:
    """The physics and cast of catalogue-none.toml, verbatim from the file
    (so the step cells ARE the none arm plus the undertakings and the step)."""
    return (EXPERIMENTS / "catalogue-none.toml").read_text()


def step_toml(cell: str, depth_m: float, step: float | None, days: int = 365,
              hold_day: int = STEP_DAY_HOLD, step_day: int = STEP_DAY,
              k_hold: float = K_HOLD) -> str:
    """One step-test experiment: catalogue-none's physics, the ops basket,
    one undertakings desk at a 0.5% band so a book exists, days truncated,
    then at hold_day the price loop's gains off (raw setGains, so the K0
    that follows is not re-derived away) and K held at k_hold; at step_day
    the K0 step (absent on the control cell)."""
    base = _none_physics()
    head, _, rest = base.partition('name = "catalogue-none"')
    assert rest, "catalogue-none.toml changed shape"
    rest = rest.replace(
        'notes = "control: no injection -- endogenous excursions only"',
        f'notes = "WP-16 step test: none arm + undertakings, gains off day {hold_day}, '
        f'K held {k_hold:g}, K0 step {"none" if step is None else f"{step:+.2f}"} day {step_day}"',
        1)
    rest = rest.replace("seed = 0xA1BC\n", f"seed = 0xA1BC\ndays = {days}\nbasket = \"ops\"\n", 1)
    rest = rest.replace("target_buck_lp_m = 50.0     # deep national-scale BUCK/USDC exit route\n",
                        "target_buck_lp_m = 50.0     # deep national-scale BUCK/USDC exit route\n"
                        f"target_buck_m = {depth_m:g}       # WP-16: the depth axis (10 | 40)\n", 1)
    rest = rest.replace("ExcursionCreditArbAgent = 3\n",
                        "ExcursionCreditArbAgent = 3\nUndertakingAgent = 1        # WP-16: the book the observer aggregates\n", 1)
    rest = rest.replace("# (no injection)\n", "")
    tail = f"""# -- WP-16: a book in front of K, and the K step ------------------------- #
[agents.UndertakingAgent]
eps = 0.005                 # the default 3% band never trades on the calm arm
p = 1.0

[[interventions]]
day = {hold_day}
action = "set_gains"        # raw: Kp = Ki = Kd = 0, the price loop open
kp = 0.0
ki = 0.0
kd = 0.0

[[interventions]]
day = {hold_day}
action = "set_k0"           # with Ki = 0 the K0 move is the live K (no re-derivation)
k0 = {k_hold:g}
"""
    if step is not None:
        tail += f"""
[[interventions]]
day = {step_day}
action = "set_k0"           # the step: K {k_hold:g} -> {k_hold + step:g}, held open-loop to the end
k0 = {k_hold + step:g}
"""
    header = ("# WP-16 step-test cell -- generated by `python -m alberta_buck.sim.kdesign tomls`\n"
              "# from catalogue-none.toml (the physics and the cast are that file's, verbatim).\n"
              f"# Cell {cell}: depth target_buck_m {depth_m:g}, K0 step "
              f"{'none (control)' if step is None else f'{step:+.2f}'} at day {step_day}.\n")
    return header + head + f'name = "kdesign-step-{cell}"' + rest + tail


def cells(smoke: bool = False) -> dict[str, dict]:
    """The cell set: name -> {depth_m, step}."""
    out = {}
    for tag, st in [("ctrl", None)] + list(STEPS_D10.items()):
        out[f"d10-{tag}"] = {"depth_m": 10.0, "step": st}
    for tag, st in [("ctrl", None)] + list(STEPS_D40.items()):
        out[f"d40-{tag}"] = {"depth_m": 40.0, "step": st}
    if smoke:
        out = {k: v for k, v in out.items() if k in ("d10-ctrl", "d10-p10")}
    return out


def write_tomls(outdir: Path | None = None, smoke: bool = False) -> list[Path]:
    """(a) The experiments: the eight step cells into experiments/ (committed),
    or the two 40-day smoke variants (steps at days 20 / 30) under
    build/sim/kdesign/smoke/."""
    paths = []
    if smoke:
        outdir = (outdir or OUTDIR) / "smoke"
        kw = dict(days=40, hold_day=20, step_day=30)
    else:
        outdir = outdir or EXPERIMENTS
        kw = {}
    outdir.mkdir(parents=True, exist_ok=True)
    for name, c in cells(smoke).items():
        p = outdir / f"kdesign-step-{name}.toml"
        p.write_text(step_toml(name, c["depth_m"], c["step"], **kw))
        paths.append(p)
    return paths


def launch_script(outdir: Path | None = None, smoke: bool = False,
                  venv: str | None = None) -> Path:
    """The detached runner: one nohup cell per line, a .log and a .done
    marker each (ONBOARDING-D7.org's ssh habit)."""
    outdir = outdir or OUTDIR
    vec = outdir / ("smoke" if smoke else "")
    vec.mkdir(parents=True, exist_ok=True)
    tomls = write_tomls(outdir, smoke) if smoke else [
        EXPERIMENTS / f"kdesign-step-{n}.toml" for n in cells()]
    venv = venv or os.environ.get(
        "ALBERTA_BUCK_VENV",
        str(REPO.parent / "alberta-buck.venv-0.1.0-nix-linux-cpython-313"))
    lines = ["#!/bin/bash", "# WP-16 step-test cells (generated); each is independent.",
             f"cd {REPO}", f"export PYTHONPATH={REPO} ALBERTA_BUCK_REPO={REPO}"]
    for t in tomls:
        name = t.stem.replace("kdesign-step-", "")
        out = vec / f"{name}.json"
        log = vec / f"{name}.log"
        done = vec / f"{name}.done"
        cmd = (f"source {venv}/bin/activate && SIM_CONTROLLER=shadow SIM_SHADOW_LAMBDA=1 "
               f"python -m alberta_buck.sim --experiment {t} --backend pyrevm "
               f"--set scenario.basket=ops --out {out}; echo exit $? > {done}")
        lines.append(f"rm -f {done}; nohup nix develop --command bash -c '{cmd}' "
                     f"> {log} 2>&1 < /dev/null & disown")
    sh = vec / "run.sh"
    sh.write_text("\n".join(lines) + "\n")
    sh.chmod(0o755)
    return sh


# ---------------------------------------------------------------------------
# Identification from the vectors
# ---------------------------------------------------------------------------

def load_cell(path: Path) -> dict:
    """The daily series the fit needs, in real units (BUCK, fractions, days)."""
    d = json.loads(Path(path).read_text())
    fr = d["frames"]
    meta = d.get("meta", {}) or {}

    def col(key, scale=1.0, default=0):
        return np.array([(f.get(key) if f.get(key) is not None else default)
                         for f in fr], float) / scale
    day = col("day")
    D = np.array([sum(pb[1] for pb in f.get("poolBal", [])) for f in fr], float) / E6
    out = {
        "path": str(path), "frames": fr, "meta": meta, "n": len(fr), "day": day,
        "e": 1.0 - col("basketVal", E18, E18), "K": col("buckK", E18),
        "q": col("sh_offset", E6), "s_S": col("sh_s", E18),
        "q_desk": col("sh_net", E6), "cap_desk": col("sh_cap", E6),
        "cap_off": col("sh_offset_cap", E6), "D": D,
        "supply": col("supply", E6), "nav": col("basketNav", E6),
        "rho": col("ut_rho", 1.0, 1.0), "tranche": col("ut_tranche"),
        "trades": col("ut_trades"), "issued_open": col("ut_issued_open", E6),
        "absorbed_open": col("ut_absorbed_open", E6),
    }
    # V's fill from the books (mode S was run; decision 18's dilution by the
    # empty desk at weight 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        fo = np.where(out["cap_off"] > 0, out["q"] / out["cap_off"], 0.0)
        fd = np.where(out["cap_desk"] > 0, out["q_desk"] / out["cap_desk"], 0.0)
    out["s_V"] = 0.5 * (fo + fd)
    steps = [(int(iv.get("applied_day", iv.get("day", 0))), iv.get("action"), iv)
             for iv in meta.get("interventions_applied", []) if iv.get("ok", True)]
    out["steps"] = steps
    k0s = [(dday, iv.get("k0")) for dday, act, iv in steps if act == "set_k0"]
    out["step_day"] = k0s[-1][0] if len(k0s) >= 2 else None
    out["hold_day"] = k0s[0][0] if k0s else None
    return out


def _ls(X, y):
    """Least squares: coefficients, residual RMS, R^2 (about zero: the
    differenced series have no offset)."""
    X = np.asarray(X, float)
    y = np.asarray(y, float)
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ coef
    ss = float(np.sum(y * y))
    return coef, float(np.sqrt(np.mean(res * res))), (1.0 - float(np.sum(res * res)) / ss if ss > 0 else float("nan"))


def identical_prefix(ctrl: dict, step: dict, keys=("basketVal", "buckK", "supply",
                                                    "sh_offset", "ut_trades")) -> int:
    """The number of leading frames on which the two cells agree (all
    frame keys); the L3 check that the step is the ONLY difference."""
    n = min(ctrl["n"], step["n"])
    for i in range(n):
        if ctrl["frames"][i] != step["frames"][i]:
            return i
    return n


def fit_pair(ctrl: dict, step: dict, h: float = 1.0, end_day: int | None = None,
             min_rho: float = 0.10) -> dict:
    """The differential step response: Delta x = x_step - x_ctrl over the
    common days from the step; the disturbance history is shared (keyed rng),
    so the difference isolates the K effect.

      price:    De[k+1] = phi De[k] - b DK[k+1]                (a, k_e)
      book:     Dq[k+1] - Dq[k] = F_K h DK[k+1]                (integrating)
                Dq[k+1] = psi Dq[k] + g DK[k+1]                (self-regulating alternative)
      supply:   the same two fits on Delta supply
    """
    t0 = step["step_day"]
    n = min(ctrl["n"], step["n"])
    i0 = int(np.searchsorted(ctrl["day"], t0)) - 1
    i0 = max(i0, 0)
    i1 = n
    if end_day is not None:
        i1 = min(i1, int(np.searchsorted(ctrl["day"], end_day)))
    # stop at the weak-side reserve's exhaustion in either cell (the ladder's
    # deeper tranches are a different regime)
    sat = None
    for i in range(i0, i1):
        if min(ctrl["rho"][i], step["rho"][i]) < min_rho:
            sat = int(ctrl["day"][i])
            i1 = i
            break
    sl = slice(i0, i1)
    dK = step["K"][sl] - ctrl["K"][sl]
    de = step["e"][sl] - ctrl["e"][sl]
    dq = step["q"][sl] - ctrl["q"][sl]
    ds = step["supply"][sl] - ctrl["supply"][sl]
    dK_step = float(np.median(dK[1:])) if len(dK) > 1 else 0.0
    out = {"step_day": t0, "n_pairs": int(i1 - i0 - 1), "end_day": int(ctrl["day"][i1 - 1]),
           "saturated_day": sat, "dK": dK_step,
           "identical_frames": identical_prefix(ctrl, step)}
    if i1 - i0 < 6:
        out["error"] = "too few frames"
        return out
    # price
    X = np.column_stack([de[:-1], dK[1:]])
    coef, rms, r2 = _ls(X, de[1:])
    phi, b = float(coef[0]), -float(coef[1])
    a = -math.log(phi) / h if 0 < phi < 1 else float("nan")
    k_e = a * b / (1.0 - phi) if 0 < phi < 1 else b / h
    out["price"] = {"phi": phi, "b": b, "a": a, "k_e": k_e, "rms": rms, "r2": r2,
                    "e_ss_per_K": (-b / (1.0 - phi)) if 0 < phi < 1 else None,
                    "de_last": float(de[-1]), "de_max_abs": float(np.max(np.abs(de)))}
    # book: integrating (ramp) and self-regulating alternatives
    coef, rms, r2 = _ls(dK[1:, None], (dq[1:] - dq[:-1]))
    fk = float(coef[0]) / h
    out["book"] = {"F_K": fk, "rms": rms, "r2": r2, "dq_last": float(dq[-1]),
                   "dq_max_abs": float(np.max(np.abs(dq)))}
    X = np.column_stack([dq[:-1], dK[1:]])
    coef, rms2, r22 = _ls(X, dq[1:])
    psi, g = float(coef[0]), float(coef[1])
    out["book"]["self_reg"] = {"psi": psi, "g": g, "rms": rms2, "r2": r22,
                               "gain": (g / (1.0 - psi)) if psi < 1 else None,
                               "tau_days": (-h / math.log(psi)) if 0 < psi < 1 else None}
    # supply
    coef, rms, r2 = _ls(dK[1:, None], (ds[1:] - ds[:-1]))
    out["supply"] = {"F_K": float(coef[0]) / h, "rms": rms, "r2": r2,
                     "dsupply_last": float(ds[-1])}
    X = np.column_stack([ds[:-1], dK[1:]])
    coef, rms2, r22 = _ls(X, ds[1:])
    psi, g = float(coef[0]), float(coef[1])
    out["supply"]["self_reg"] = {"psi": psi, "g": g, "rms": rms2, "r2": r22,
                                 "gain": (g / (1.0 - psi)) if psi < 1 else None,
                                 "tau_days": (-h / math.log(psi)) if 0 < psi < 1 else None}
    # the scales the units need, over the fit window of the control cell
    out["scales"] = {"D": float(np.mean(ctrl["D"][sl])), "cap_off": float(np.mean(ctrl["cap_off"][sl])),
                     "cap_desk": float(np.mean(ctrl["cap_desk"][sl])),
                     "nav": float(np.mean(ctrl["nav"][sl]))}
    return out


def fit_pooled(ctrl: dict, steps: list[dict], h: float = 1.0, min_rho: float = 0.10) -> dict:
    """The pooled least squares over every step cell of one depth (the same
    regressions, stacked), plus the per-cell fits and the linearity check."""
    per = {}
    Xe, ye, Xq, yq, Xs, ys = [], [], [], [], [], []
    for st in steps:
        f = fit_pair(ctrl, st, h, min_rho=min_rho)
        per[st["name"]] = f
        if "error" in f:
            continue
        t0 = st["step_day"]
        i0 = max(int(np.searchsorted(ctrl["day"], t0)) - 1, 0)
        i1 = int(np.searchsorted(ctrl["day"], f["end_day"], side="right"))
        sl = slice(i0, i1)
        dK = st["K"][sl] - ctrl["K"][sl]
        de = st["e"][sl] - ctrl["e"][sl]
        dq = st["q"][sl] - ctrl["q"][sl]
        ds = st["supply"][sl] - ctrl["supply"][sl]
        Xe.append(np.column_stack([de[:-1], dK[1:]])); ye.append(de[1:])
        Xq.append(dK[1:, None]); yq.append(dq[1:] - dq[:-1])
        Xs.append(dK[1:, None]); ys.append(ds[1:] - ds[:-1])
    out = {"cells": per}
    if not Xe:
        out["error"] = "no usable step cell"
        return out
    coef, rms, r2 = _ls(np.vstack(Xe), np.concatenate(ye))
    phi, b = float(coef[0]), -float(coef[1])
    a = -math.log(phi) / h if 0 < phi < 1 else float("nan")
    k_e = a * b / (1.0 - phi) if 0 < phi < 1 else b / h
    out["price"] = {"phi": phi, "b": b, "a": a, "k_e": k_e, "rms": rms, "r2": r2,
                    "n": int(sum(len(y) for y in ye))}
    coef, rms, r2 = _ls(np.vstack(Xq), np.concatenate(yq))
    out["book"] = {"F_K": float(coef[0]) / h, "rms": rms, "r2": r2}
    coef, rms, r2 = _ls(np.vstack(Xs), np.concatenate(ys))
    out["supply"] = {"F_K": float(coef[0]) / h, "rms": rms, "r2": r2}
    sc = [f["scales"] for f in per.values() if "scales" in f]
    out["scales"] = {k: float(np.mean([s[k] for s in sc])) for k in sc[0]} if sc else {}
    # the control cell's own reversion (closed loop, the whole run): AR(1) of e
    e = ctrl["e"]
    coef, _rms, r2c = _ls(np.column_stack([e[:-1], np.ones(len(e) - 1)]), e[1:])
    out["control_ar1"] = {"phi": float(coef[0]), "a": (-math.log(coef[0]) / h) if 0 < coef[0] < 1 else None,
                          "r2": r2c}
    # linearity: F_K and k_e per cell vs the pooled value
    out["linearity"] = {n: {"F_K_ratio": (f["book"]["F_K"] / out["book"]["F_K"]) if out["book"]["F_K"] else None,
                            "k_e_ratio": (f["price"]["k_e"] / out["price"]["k_e"]) if out["price"]["k_e"] else None}
                        for n, f in per.items() if "book" in f}
    return out


def identify(outdir: Path | None = None, smoke: bool = False) -> dict:
    """(b) Load every vector present and fit per depth."""
    outdir = outdir or OUTDIR
    vec = outdir / ("smoke" if smoke else "")
    loaded = {}
    for name, c in cells(smoke).items():
        p = vec / f"{name}.json"
        if p.exists():
            cell = load_cell(p)
            cell["name"] = name
            cell.update(c)
            loaded[name] = cell
    res = {"vectors": {n: {"path": c["path"], "frames": c["n"], "step_day": c["step_day"],
                           "hold_day": c["hold_day"], "K_last": float(c["K"][-1]),
                           "trades": int(c["trades"][-1]), "q_max_abs": float(np.max(np.abs(c["q"]))),
                           "s_S_max_abs": float(np.max(np.abs(c["s_S"]))),
                           "rho_min": float(np.min(c["rho"])),
                           "e_rms": float(np.sqrt(np.mean(c["e"] ** 2)))}
                       for n, c in loaded.items()},
           "depths": {}}
    for depth in ("d10", "d40"):
        ctrl = loaded.get(f"{depth}-ctrl")
        steps = [c for n, c in loaded.items() if n.startswith(depth) and c["step"] is not None]
        if ctrl is None or not steps:
            continue
        res["depths"][depth] = fit_pooled(ctrl, steps)
        res["depths"][depth]["depth_m"] = ctrl["depth_m"]
    return res


# ---------------------------------------------------------------------------
# (c) The table
# ---------------------------------------------------------------------------

def plants_from_fit(fit: dict, des: Design) -> dict:
    """Per depth: the plant in fill units and the two modes' sigma."""
    out = {}
    for depth, f in fit.get("depths", {}).items():
        if "price" not in f or "book" not in f:
            continue
        sc = f["scales"]
        cap = sc["cap_off"]
        D = sc["D"]
        a, k_e, F_K = f["price"].get("a"), f["price"].get("k_e"), f["book"].get("F_K")
        bad = [n for n, v in (("a", a), ("k_e", k_e), ("F_K", F_K))
               if v is None or not math.isfinite(v) or v <= 0]
        if bad:
            print(f"[solve] {depth}: no usable plant ({', '.join(bad)} not finite and "
                  "positive); skipped", file=sys.stderr)
            continue
        plant = Plant(a=a, k_e=k_e, k_s=F_K / cap, t_flow=des.t_persist_days)
        out[depth] = {"plant_fill": plant, "sigma": {"S": cap / D, "V": 0.5},
                      "D": D, "cap_off": cap, "F_K": f["book"]["F_K"],
                      "F_K_supply": f["supply"]["F_K"], "depth_m": f.get("depth_m")}
    return out


def solve(fit: dict, des: Design | None = None, w_carry: float | None = None,
          tau_target: float | None = None, flow_state: bool = True) -> dict:
    des = des or Design()
    table = {"design": asdict(des), "sim_gains": dict(zip(("Kp", "Ki"), des.sim_gains())),
             "t_persist_days": des.t_persist_days, "rows": {}}
    for depth, p in plants_from_fit(fit, des).items():
        for mode in ("S", "V"):
            row = design_mode(p["plant_fill"], p["sigma"][mode], des, w_carry, tau_target, flow_state)
            row["depth"] = depth
            row["depth_m"] = p["depth_m"]
            row["mode"] = mode
            row["D"] = p["D"]
            row["cap_off"] = p["cap_off"]
            row["F_K"] = p["F_K"]
            row["F_K_supply"] = p["F_K_supply"]
            table["rows"][f"{mode}-{depth}"] = row
    return table


def _fmt(v, spec: str) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "-"
    return format(v, spec)


def org_fit_table(fit: dict) -> str:
    """The step responses per cell and the pooled fit per depth, as org."""
    lines = ["| depth | cell | dK | pairs | identical frames | sat day | De last | Dq last (BUCK) | Dsupply last (BUCK) | F_K book (BUCK/d/K) | r2 | F_K supply | r2 | a (1/d) | k_e | r2 |",
             "|-------+------+----+-------+------------------+---------+---------+----------------+---------------------+---------------------+----+------------+----+---------+-----+----|"]
    for depth, f in fit.get("depths", {}).items():
        for name, c in f.get("cells", {}).items():
            if "book" not in c:
                lines.append(f"| {depth} | {name} | {_fmt(c.get('dK'), '+.2f')} | {c.get('n_pairs', '-')} | "
                             f"{c.get('identical_frames', '-')} | {c.get('saturated_day') or '-'} | "
                             f"{c.get('error', 'no fit')} | | | | | | | | | |")
                continue
            lines.append(
                f"| {depth} | {name} | {_fmt(c['dK'], '+.2f')} | {c['n_pairs']} | {c['identical_frames']} "
                f"| {c['saturated_day'] or '-'} | {_fmt(c['price']['de_last'], '+.4f')} "
                f"| {_fmt(c['book']['dq_last'], '+,.0f')} | {_fmt(c['supply']['dsupply_last'], '+,.0f')} "
                f"| {_fmt(c['book']['F_K'], '+.4g')} | {_fmt(c['book']['r2'], '.2f')} "
                f"| {_fmt(c['supply']['F_K'], '+.4g')} | {_fmt(c['supply']['r2'], '.2f')} "
                f"| {_fmt(c['price']['a'], '.3g')} | {_fmt(c['price']['k_e'], '+.3g')} | {_fmt(c['price']['r2'], '.2f')} |")
        if "price" in f:
            ar = f.get("control_ar1", {})
            lines.append(
                f"| {depth} | POOLED | | {f['price']['n']} | | | control AR1 a {_fmt(ar.get('a'), '.3g')} | | "
                f"| {_fmt(f['book']['F_K'], '+.4g')} | {_fmt(f['book']['r2'], '.2f')} "
                f"| {_fmt(f['supply']['F_K'], '+.4g')} | {_fmt(f['supply']['r2'], '.2f')} "
                f"| {_fmt(f['price']['a'], '.3g')} | {_fmt(f['price']['k_e'], '+.3g')} | {_fmt(f['price']['r2'], '.2f')} |")
    return "\n".join(lines)


def org_table(table: dict) -> str:
    """The gain table as org, one row per mode x depth."""
    lines = ["| mode | depth | k_s (s/day/K) | w | tau_s d | zeta | Kq | Kqi (1/s) | Kqd | Kq x1e12 | Kqi x1e12 | SIMC Kq | SIMC Kqi (1/s) | tau_s x0.5: Kq / Kqi | tau_s x2: Kq / Kqi | anchor Kp / Ki ratio | in range |",
             "|------+-------+---------------+---+---------+------+----+-----------+-----+----------+-----------+---------+----------------+----------------------+--------------------+----------------------+----------|"]
    for key, r in table["rows"].items():
        g, s, ax, an = r["lqr"], r["simc"], r["tau_s_axis"], r["anchor"]
        lines.append(
            f"| {r['mode']} | {r['depth_m']:g} | {r['plant']['k_s']:.3e} | {r['weights']['w_carry']:.3e} "
            f"| {g['tau_s_days']:.1f} | {g['zeta']:.2f} | {g['Kq']:.4g} | {g['Kqi']:.3e} | {g['Kqd']:.3g} "
            f"| {g['Kq_x1e12']} | {g['Kqi_x1e12']} | {s['Kq']:.4g} | {s['Kqi']:.3e} "
            f"| {ax['0.5']['Kq']:.4g} / {ax['0.5']['Kqi']:.3e} | {ax['2']['Kq']:.4g} / {ax['2']['Kqi']:.3e} "
            f"| {an['Kp_ratio']:.3f} / {an['Ki_ratio']:.3f} | {'yes' if r['tau_s_in_range'] else 'NO'} |")
    return "\n".join(lines)


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, complex):
        return [o.real, o.imag]
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    raise TypeError(f"not serialisable: {type(o)}")


def _clean(o):
    """JSON-safe: numpy scalars, NaN / inf -> None, nested."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        o = o.item()
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, complex):
        return [o.real, o.imag]
    return o


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="alberta_buck.sim.kdesign", description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["tomls", "launch", "fit", "solve", "all"])
    ap.add_argument("--outdir", default=str(OUTDIR))
    ap.add_argument("--smoke", action="store_true", help="the 40-day two-cell smoke set")
    ap.add_argument("--run", action="store_true", help="launch: execute the runner")
    ap.add_argument("--w-carry", type=float, default=None, help="solve: the carry weight (default: for tau_s at the geometric mean of the bounds)")
    ap.add_argument("--tau-target", type=float, default=None, help="solve: the tau_s (days) the carry weight is set for")
    ap.add_argument("--t-persist", type=float, default=T_PERSIST_DAYS)
    ap.add_argument("--no-flow-state", action="store_true", help="solve: 4-state plant (no Kqd)")
    a = ap.parse_args(argv)
    outdir = Path(a.outdir)
    if a.cmd == "tomls":
        for p in write_tomls(outdir if a.smoke else None, a.smoke):
            print(p)
        return 0
    if a.cmd == "launch":
        sh = launch_script(outdir, a.smoke)
        print(sh)
        if a.run:
            subprocess.run(["bash", str(sh)], check=True)
        return 0
    des = Design(t_persist_days=a.t_persist)
    if a.cmd in ("fit", "all"):
        fit = identify(outdir, a.smoke)
        p = outdir / ("smoke" if a.smoke else "") / "fit.json"
        p.write_text(json.dumps(_clean(fit), indent=1))
        print(p)
        po = p.with_suffix(".org")
        po.write_text(org_fit_table(fit) + "\n")
        print(org_fit_table(fit))
        for depth, f in fit.get("depths", {}).items():
            if "price" in f:
                print(f"[fit] {depth}: a={f['price']['a']:.4g}/d k_e={f['price']['k_e']:.4g} (r2 {f['price']['r2']:.3f}) "
                      f"F_K={f['book']['F_K']:.4g} BUCK/d/K (r2 {f['book']['r2']:.3f}) "
                      f"supply F_K={f['supply']['F_K']:.4g} (r2 {f['supply']['r2']:.3f}); "
                      f"control AR1 a={f['control_ar1']['a']}")
    if a.cmd in ("solve", "all"):
        p = outdir / ("smoke" if a.smoke else "") / "fit.json"
        fit = json.loads(p.read_text())
        table = solve(fit, des, a.w_carry, a.tau_target, not a.no_flow_state)
        pj = outdir / ("smoke" if a.smoke else "") / "gains.json"
        pj.write_text(json.dumps(_clean(table), indent=1))
        po = pj.with_suffix(".org")
        po.write_text(org_table(table) + "\n")
        print(pj)
        print(org_table(table))
    return 0


if __name__ == "__main__":
    sys.exit(main())
