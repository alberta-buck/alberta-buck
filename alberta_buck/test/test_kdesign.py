"""WP-16: the gain derivation on a SYNTHETIC plant (alberta_buck/sim/kdesign.py).

Riccati iteration against a known solution (and scipy when importable), the
joint plant's conserved combination (the finding), the anchor on a plant
built from the sim's own derivation, the SIMC formulas, the units mapping
(real <-> x1e12, S <-> V), the closed-loop margins, the identification's
recovery of known gains, and the step-test TOMLs' physics against
catalogue-none.toml.
"""

from __future__ import annotations

import math
import tomllib

import numpy as np
import pytest

from alberta_buck.sim import kdesign as kd
from alberta_buck.sim.experiment import derive_gains


SYN = kd.Plant(a=0.6, k_e=0.10, k_s=0.02, t_flow=180.0)


# --- Riccati ----------------------------------------------------------------- #

def test_dare_scalar_closed_form():
    # x' = a x + b u, cost q x^2 + r u^2: P solves p = q + a^2 p - a^2 b^2 p^2 / (r + b^2 p)
    a, b, q, r = 0.95, 0.3, 2.0, 0.5
    for solver in (kd.dare, kd.dare_value_iteration):
        P, _it = solver([[a]], [[b]], [[q]], [[r]])
        p = float(P[0, 0])
        lhs = q + a * a * p - (a * b * p) ** 2 / (r + b * b * p)
        assert abs(lhs - p) < 1e-9
        A2 = b * b
        B2 = r - a * a * r - b * b * q
        C2 = -q * r
        root = (-B2 + math.sqrt(B2 * B2 - 4 * A2 * C2)) / (2 * A2)
        assert abs(p - root) < 1e-7


def test_dare_doubling_agrees_with_value_iteration():
    # the position loop's plant: two integrators, slow modes
    A, B = kd.position_plant(0.02, 0.25, 180.0, flow_state=True)
    Q = np.diag([0.1, 0.1 / 180.0 ** 2, 0.0])
    R = np.array([[4.0]])
    P, it = kd.dare(A, B, Q, R)
    assert it < 120
    Pv, _itv = kd.dare_value_iteration(A, B, Q, R, tol=1e-10, max_iter=400_000)
    assert np.max(np.abs(P - Pv)) <= 1e-4 * max(1.0, np.max(np.abs(Pv)))


def test_dare_matches_scipy_when_available():
    pytest.importorskip("scipy")
    from scipy.linalg import solve_discrete_are
    A, B = kd.position_plant(0.02, 0.25, 180.0, flow_state=True)
    Q = np.diag([1e-3, 1e-3 / 180.0 ** 2, 0.0])
    R = np.array([[4.0]])
    P, _it = kd.dare(A, B, Q, R)
    Ps = solve_discrete_are(A, B, Q, R)
    assert np.max(np.abs(P - Ps)) <= 1e-7 * max(1.0, np.max(np.abs(Ps)))


# --- The joint plant's conserved combination (the finding) ------------------- #

def test_joint_plant_has_a_conserved_direction():
    h = 0.25
    A, B, _G = SYN.discretize(h)
    z = kd.conserved_direction(SYN, h)
    assert np.max(np.abs(z @ A - z)) < 1e-12
    assert abs(float((z @ B)[0])) < 1e-12
    # it involves the price integral, the price and the position: one lever
    # cannot zero both integrals
    assert z[1] == 1.0 and z[0] > 0 and z[2] > 0 and z[3] == 0.0


def test_joint_closed_loop_keeps_the_structural_unit_eigenvalue():
    des = kd.Design()
    row = kd.design_mode(SYN, sigma=1.0, des=des, w_carry=1e-3)
    cl = row["closed_loop"]
    assert cl["structural_unit_eig_dev"] < 1e-9
    assert cl["stable"] and cl["max_modulus"] < 1.0


# --- The anchor -------------------------------------------------------------- #

def test_ki_anchor_is_brysons_rule_independent_of_the_plant():
    # q_Ie = 1/(e_max tau_I)^2, r = 1/dk_rail^2 -> Ki = dk_rail / (e_max tau_I)
    des = kd.Design()
    _kp_sim, ki_sim = des.sim_gains()
    for a, k_e in ((0.3, 0.05), (0.6, 0.10), (1.5, 0.30)):
        plant = kd.Plant(a=a, k_e=k_e, k_s=0.02)
        w = kd.bryson_weights(des, q_e=0.0)
        _kp, ki_day = kd.price_loop_gains(plant, w, des.h_days)
        assert abs(ki_day / kd.DAY / ki_sim - 1.0) < 0.05


def test_anchor_on_a_plant_built_from_the_sims_derivation():
    des = kd.Design()
    kp_sim, ki_sim = des.sim_gains()
    assert (kp_sim, ki_sim) == (derive_gains()[0] / 1e12, derive_gains()[1] / 1e12)
    row = kd.design_mode(SYN, sigma=1.0, des=des, w_carry=1e-3)
    an = row["anchor"]
    assert an["q_e_feasible"]
    assert abs(an["Kp_ratio"] - 1.0) <= 0.25
    assert abs(an["Ki_ratio"] - 1.0) <= 0.25
    assert an["pass"]
    # the price loop's gains in the table ARE the anchor's (regime-wise)
    assert abs(row["lqr"]["Kp"] - an["Kp"]) < 1e-12
    assert abs(row["lqr"]["Ki"] - an["Ki"]) < 1e-18


def test_position_weight_zero_decouples_the_loops():
    des = kd.Design()
    w = kd.bryson_weights(des, q_e=0.3, w_carry=0.0)
    A, B, _G = SYN.discretize(des.h_days)
    L, _P, _it = kd.lqr(A, B, w.Q(), w.r)
    g = kd.gains_from_L(L, SYN.k_s)
    assert abs(g["Kq"]) < 1e-12 and abs(g["Kqi_day"]) < 1e-12
    kp2, ki2 = kd.price_loop_gains(SYN, w, des.h_days)
    assert abs(g["Kp"] - kp2) < 1e-12 and abs(g["Ki_day"] - ki2) < 1e-12


def test_gains_are_insensitive_to_the_design_cadence():
    des = kd.Design()
    out = {}
    for h in (0.25, 1.0):
        w = kd.bryson_weights(des, q_e=0.3, w_carry=1e-3)
        kp, ki = kd.price_loop_gains(SYN, w, h)
        gp = kd.position_loop_gains(SYN.k_s, w, h, SYN.t_flow, True)
        out[h] = {"Kp": kp, "Ki_day": ki, **gp}
    # Kp carries the current-sample integral feedthrough (Ki h), the rest
    # is cadence-free
    assert abs(out[0.25]["Kp"] / out[1.0]["Kp"] - 1.0) < 0.35
    for k in ("Ki_day", "Kq", "Kqi_day", "Kqd_day"):
        assert abs(out[0.25][k] / out[1.0][k] - 1.0) < 0.06, k


# --- SIMC and the position loop ------------------------------------------------ #

def test_simc_formulas_and_their_inverse():
    kq, kqi = kd.simc_integrating(k_s=0.02, tau_s=120.0)
    assert abs(kq - 1.0 / (0.02 * 120.0)) < 1e-12
    assert abs(kqi - kq / (4 * 120.0)) < 1e-12
    assert abs(kd.implied_tau_s(0.02, kq) - 120.0) < 1e-9
    assert abs(kd.position_loop_damping(0.02, kq, kqi) - 1.0) < 1e-9


def test_position_loop_lqr_is_the_continuous_time_closed_form():
    # Kqi = sqrt(w q_Is / r), Kq = sqrt(2 Kqi / k_s + w q_s / r) (fine cadence)
    des = kd.Design()
    w = kd.bryson_weights(des, q_e=0.0, w_carry=2e-3)
    g = kd.position_loop_gains(SYN.k_s, w, 0.05, SYN.t_flow, False)
    kqi = math.sqrt(w.w_carry * w.q_Is / w.r)
    kq = math.sqrt(2 * kqi / SYN.k_s + w.w_carry * w.q_s / w.r)
    assert abs(g["Kqi_day"] / kqi - 1.0) < 0.02
    assert abs(g["Kq"] / kq - 1.0) < 0.02


def test_lqr_position_loop_lands_where_the_carry_weight_puts_it():
    des = kd.Design()
    row = kd.design_mode(SYN, sigma=1.0, des=des, tau_target=127.0)
    assert abs(row["lqr"]["tau_s_days"] / 127.0 - 1.0) < 0.02
    assert row["lqr"]["Kq"] > 0 and row["lqr"]["Kqi_day"] > 0
    # LQR damping between the q_s = 0 limit (1/sqrt 2) and well-damped
    assert 0.6 < row["lqr"]["zeta"] < 3.0
    # SIMC at the same tau_s has the same Kq by construction
    assert abs(row["simc"]["Kq"] / row["lqr"]["Kq"] - 1.0) < 1e-9
    ax = row["tau_s_axis"]
    assert abs(ax["0.5"]["Kq"] / row["lqr"]["Kq"] - 2.0) < 1e-9
    assert abs(ax["2"]["Kqi_day"] / row["lqr"]["Kqi_day"] - 0.25) < 1e-9
    # the implied feedforward: positive (a flow into s is drained) and small
    assert row["lqr"]["Kqd_day"] > 0


def test_margins_of_the_joint_loop():
    des = kd.Design()
    row = kd.design_mode(SYN, sigma=1.0, des=des, w_carry=1e-3)
    m = row["margins"]
    assert m["phase_margin_deg"] is not None and m["phase_margin_deg"] > 45.0
    assert m["gain_margin"] is None or m["gain_margin"] > 1.8
    assert row["closed_loop"]["stable"]
    assert row["lqr"]["position_loop_alone"]["stable"]


# --- Units ----------------------------------------------------------------------- #

def test_units_real_to_contract_and_back():
    g = {"Kp": 0.1, "Ki_day": 6.43004e-7 * kd.DAY, "Kq": 0.25, "Kqi_day": 2.0e-7 * kd.DAY,
         "Kqd_day": 3.0 / kd.DAY}
    c = kd.to_contract_units(g)
    assert c["Kp_x1e12"] == 100_000_000_000
    assert c["Ki_x1e12"] == 643_004
    assert abs(kd.real_from_x1e12(c["Kqi_x1e12"]) - 2.0e-7) < 1e-18
    assert abs(c["Kqd"] - 3.0) < 1e-12


def test_s_and_v_are_one_problem_in_fill_units():
    # designing directly in each mode's units reproduces the fill-unit
    # design scaled by 1 / sigma_mode
    des = kd.Design()
    sig_S, sig_V = 0.96, 0.5
    fill = kd.design_mode(SYN, sigma=1.0, des=des, w_carry=2e-3)
    for sig in (sig_S, sig_V):
        w = kd.bryson_weights(des, sigma=sig, w_carry=2e-3, q_e=fill["weights"]["q_e"])
        g = kd.position_loop_gains(SYN.k_s * sig, w, des.h_days, SYN.t_flow, True)
        assert abs(g["Kq"] * sig / fill["lqr"]["Kq"] - 1.0) < 1e-6
        assert abs(g["Kqi_day"] * sig / fill["lqr"]["Kqi_day"] - 1.0) < 1e-6
        assert abs(g["Kqd_day"] * sig / fill["lqr"]["Kqd_day"] - 1.0) < 1e-6
    rowS = kd.design_mode(SYN, sigma=sig_S, des=des, w_carry=2e-3)
    rowV = kd.design_mode(SYN, sigma=sig_V, des=des, w_carry=2e-3)
    assert abs(rowV["lqr"]["Kq"] / rowS["lqr"]["Kq"] - sig_S / sig_V) < 1e-6
    assert abs(rowV["lqr"]["tau_s_days"] / rowS["lqr"]["tau_s_days"] - 1.0) < 1e-6
    assert abs(rowV["lqr"]["Kp"] / rowS["lqr"]["Kp"] - 1.0) < 1e-12


# --- Identification ------------------------------------------------------------- #

def _synthetic_cells(F_K=250.0, a=0.6, k_e=0.10, step=0.10, n=365, t_hold=180, t_step=210, seed=3):
    """Two cells with a shared disturbance history; the plant of record in
    discrete time (h = 1 day), the step cell's K stepped at t_step."""
    rng = np.random.default_rng(seed)
    w_e = rng.normal(0, 0.002, n)
    w_q = rng.normal(0, 400.0, n)
    phi = math.exp(-a)
    b = k_e * (1 - phi) / a
    cells = {}
    for name, dk in (("ctrl", 0.0), ("step", step)):
        e = np.zeros(n); q = np.zeros(n); K = np.full(n, 0.75); sup = np.full(n, 5.0e7)
        K[t_step:] += dk
        for k in range(1, n):
            e[k] = phi * e[k - 1] - b * (K[k] - 0.75) + w_e[k]
            q[k] = q[k - 1] + F_K * (K[k] - 0.75) + w_q[k]
            sup[k] = sup[k - 1] + 2 * F_K * (K[k] - 0.75) + 0.5 * w_q[k]
        frames = [{"day": k, "basketVal": int((1 - e[k]) * 1e18), "buckK": int(K[k] * 1e18),
                   "sh_offset": int(q[k] * 1e6), "sh_s": int(q[k] / 1.04e7 * 1e18),
                   "sh_net": 0, "sh_cap": int(1.17e6 * 1e6), "sh_offset_cap": int(1e7 * 1e6),
                   "poolBal": [[0, int(2.6e6 * 1e6)]] * 4, "supply": int(sup[k] * 1e6),
                   "basketNav": int(2.0e7 * 1e6), "ut_rho": 1.0, "ut_tranche": 0, "ut_trades": k,
                   "ut_issued_open": 0, "ut_absorbed_open": int(q[k] * 1e6)} for k in range(n)]
        meta = {"interventions_applied": [
            {"day": t_hold, "action": "set_gains", "applied_day": t_hold, "ok": True},
            {"day": t_hold, "action": "set_k0", "k0": 0.75, "applied_day": t_hold, "ok": True}]}
        if dk:
            meta["interventions_applied"].append(
                {"day": t_step, "action": "set_k0", "k0": 0.75 + dk, "applied_day": t_step, "ok": True})
        cells[name] = {"frames": frames, "meta": meta}
    return cells


def test_identification_recovers_known_gains(tmp_path):
    import json
    cells = _synthetic_cells()
    paths = {}
    for name, c in cells.items():
        p = tmp_path / f"{name}.json"
        p.write_text(json.dumps(c))
        paths[name] = p
    ctrl = kd.load_cell(paths["ctrl"]); ctrl["name"] = "ctrl"
    st = kd.load_cell(paths["step"]); st["name"] = "step"
    assert st["step_day"] == 210 and st["hold_day"] == 180
    f = kd.fit_pair(ctrl, st)
    assert f["identical_frames"] == 210
    assert abs(f["dK"] - 0.10) < 1e-9
    assert abs(f["price"]["a"] / 0.6 - 1.0) < 0.15
    assert abs(f["price"]["k_e"] / 0.10 - 1.0) < 0.15
    assert abs(f["book"]["F_K"] / 250.0 - 1.0) < 0.10
    assert abs(f["supply"]["F_K"] / 500.0 - 1.0) < 0.10
    pooled = kd.fit_pooled(ctrl, [st])
    assert abs(pooled["book"]["F_K"] / 250.0 - 1.0) < 0.10
    assert abs(pooled["scales"]["D"] - 1.04e7) < 1.0
    table = kd.org_fit_table({"depths": {"d10": pooled}})
    assert "| d10 | step |" in table and "| d10 | POOLED |" in table


# --- The experiments --------------------------------------------------------------- #

def test_step_tomls_carry_the_none_arms_physics():
    none = tomllib.loads((kd.EXPERIMENTS / "catalogue-none.toml").read_text())
    for name, c in kd.cells().items():
        t = tomllib.loads(kd.step_toml(name, c["depth_m"], c["step"]))
        assert t["scenario"]["basket"] == "ops" and t["scenario"]["days"] == 365
        agents = dict(t["scenario"]["agents"])
        assert agents.pop("UndertakingAgent") == 1
        assert agents == none["scenario"]["agents"]
        for cls in none["agents"]:
            assert t["agents"][cls] == none["agents"][cls]
        assert t["agents"]["UndertakingAgent"] == {"eps": 0.005, "p": 1.0}
        assert t["deploy"]["target_buck_m"] == c["depth_m"]
        iv = t["interventions"]
        assert iv[0] == {"day": 120, "action": "set_gains", "kp": 0.0, "ki": 0.0, "kd": 0.0}
        assert iv[1] == {"day": 120, "action": "set_k0", "k0": 0.75}
        if c["step"] is None:
            assert len(iv) == 2
        else:
            assert iv[2]["day"] == 150 and abs(iv[2]["k0"] - (0.75 + c["step"])) < 1e-12
    # the committed files are what the generator writes
    for name, c in kd.cells().items():
        p = kd.EXPERIMENTS / f"kdesign-step-{name}.toml"
        assert p.read_text() == kd.step_toml(name, c["depth_m"], c["step"])


def test_ksteps_carry_their_base_verbatim():
    """T15's K steps: the base experiment unchanged but for the name and the
    days, then the open-loop hold and (off the control) the step -- and the
    committed files are what the generator writes."""
    base = "organic-retiree"
    src = tomllib.loads((kd.EXPERIMENTS / f"{base}.toml").read_text())
    for cell, st in [("ctrl", None)] + list(kd.KSTEPS.items()):
        text = kd.kstep_toml(base, cell, st)
        t = tomllib.loads(text)
        assert t["name"] == f"kstep-{base}-{cell}"
        assert t["scenario"]["days"] == 365
        assert {k: v for k, v in t["scenario"].items() if k != "days"} == src["scenario"]
        assert t["agents"] == src["agents"] and t["deploy"] == src["deploy"]
        iv = t["interventions"]
        assert iv[0] == {"day": 120, "action": "set_gains", "kp": 0.0, "ki": 0.0, "kd": 0.0}
        assert iv[1] == {"day": 120, "action": "set_k0", "k0": 0.75}
        if st is None:
            assert len(iv) == 2
        else:
            assert iv[2]["day"] == 150 and abs(iv[2]["k0"] - (0.75 + st)) < 1e-12
        assert (kd.EXPERIMENTS / f"kstep-{base}-{cell}.toml").read_text() == text
