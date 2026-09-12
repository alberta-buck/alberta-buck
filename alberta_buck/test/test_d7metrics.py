"""WP-15: the controller-alternatives grid's metrics on synthetic frame
series (eqmetrics' WP-15 block) and the star driver's multi-level axes
(star.py's WP-15 block).  No chain."""

import json
import math
from pathlib import Path

import pytest

from alberta_buck.sim import eqmetrics as eq
from alberta_buck.sim import star

E6 = 10 ** 6
E18 = 10 ** 18


def _frames(n, **series):
    """n daily frames; each keyword is a per-frame list or a scalar."""
    out = []
    for i in range(n):
        f = {"day": i, "basketVal": E18, "buckK": int(0.75 * E18),
             "supply": 60 * 10 ** 12}
        for k, v in series.items():
            f[k] = v[i] if isinstance(v, (list, tuple)) else v
        out.append(f)
    return out


def _s18(vals):
    return [int(v * E18) for v in vals]


# -- habituation ------------------------------------------------------------ #

def test_habituation_exponential_decay_and_gate_p9():
    # s jumps to 0.02 at day 10 and decays with tau 20 d: 10% at ~46 d after.
    s = [0.0] * 10 + [0.02 * math.exp(-(i - 10) / 20.0) for i in range(10, 120)]
    fr = _frames(120, sh_s=_s18(s))
    h = eq.habituation(fr, end_day=10, start_day=10, tau_s_days=40.0)
    assert h["peak"] == pytest.approx(0.02)
    assert h["peak_day"] == 10
    assert 44 <= h["t_hab"] <= 48
    assert h["sign_changes"] == 0
    assert h["residual"] < 0.1 * h["peak"]
    assert h["p9"] is True                    # 46 <= 1.5 * 40
    assert eq.habituation(fr, end_day=10, tau_s_days=20.0)["p9"] is False


def test_habituation_limit_cycle_counts_sign_changes():
    s = [0.02 * math.sin(2 * math.pi * i / 40.0) for i in range(200)]
    fr = _frames(200, sh_s=_s18(s))
    h = eq.habituation(fr, end_day=0, tau_s_days=90.0)
    assert h["t_hab"] is None                 # never inside the band for 5 d
    assert h["sign_changes"] >= 8
    assert h["p9"] is False


def test_habituation_one_overshoot_is_one_sign_change():
    s = [0.02 * math.exp(-i / 15.0) * math.cos(2 * math.pi * i / 60.0)
         for i in range(150)]
    fr = _frames(150, sh_s=_s18(s))
    h = eq.habituation(fr, end_day=0, tau_s_days=90.0)
    assert h["sign_changes"] == 1
    assert h["t_hab"] is not None


def test_habituation_none_without_sh_s():
    assert eq.habituation(_frames(10), end_day=0) is None


def test_sign_changes_ignore_the_band():
    assert eq._sign_changes([1, 0.5, 0.05, -0.05, -0.5, -1], band=0.1) == 1
    assert eq._sign_changes([1, 0.05, 1, 0.05, 1], band=0.1) == 0
    assert eq._sign_changes([1, -1, 1, -1], band=0.1) == 3


# -- carry ------------------------------------------------------------------ #

def test_carry_buck_days_per_facility():
    fr = _frames(10, ut_absorbed_open=5 * E6, ut_issued_open=0,
                 fac_drawn=2 * E6, sh_net=-3 * E6, sh_offset=3 * E6)
    c = eq.carry(fr)
    assert c["ut"] == pytest.approx(50.0)
    assert c["fac"] == pytest.approx(20.0)
    assert c["desk"] == pytest.approx(30.0)
    assert c["total"] == pytest.approx(100.0)
    assert c["booked"] == pytest.approx(30.0)
    assert c["days"] == 10
    assert c["mean_buck"] == pytest.approx(10.0)
    # a window
    assert eq.carry(fr, day0=5)["ut"] == pytest.approx(25.0)
    # absent fields carry nothing
    assert eq.carry(_frames(5))["total"] == 0.0


# -- saturation dwell --------------------------------------------------------- #

def test_saturation_dwell_fractions():
    sat = [E18 if i in (2, 5) else 0 for i in range(10)]
    stale = [1 if i in (1, 2, 3) else 0 for i in range(10)]
    rho = [0.05 if i == 7 else 1.0 for i in range(10)]
    fr = _frames(10, sh_sat=sat, sh_stale=stale, sh_excluded=0,
                 sh_desk_cap=1000, ut_rho=rho)
    w = eq.saturation_dwell(fr)
    assert w["sat_frac"] == pytest.approx(0.2)
    assert w["stale_frac"] == pytest.approx(0.3)
    assert w["excluded_frac"] == 0.0
    assert w["ladder_frac"] == pytest.approx(0.1)
    # a desk with a zero held cap is saturated
    fr2 = _frames(4, sh_desk_cap=[0, 0, 1000, 1000])
    assert eq.saturation_dwell(fr2)["sat_frac"] == pytest.approx(0.5)
    # nothing to measure -> None fractions
    assert eq.saturation_dwell(_frames(3))["sat_frac"] is None


# -- K economy --------------------------------------------------------------- #

def test_k_economy_total_variation_and_rails():
    ks = [0.75, 0.76, 0.74, 0.74, 0.95, 0.95]
    fr = _frames(6, buckK=[int(k * E18) for k in ks])
    k = eq.k_economy(fr)
    assert k["tv"] == pytest.approx(0.01 + 0.02 + 0.0 + 0.21 + 0.0)
    assert k["dk_max_per_day"] == pytest.approx(0.21)
    assert k["rail_days"] == 2
    assert k["k_min"] == pytest.approx(0.74) and k["k_max"] == pytest.approx(0.95)
    assert eq.k_economy(fr, day0=4)["tv"] == 0.0


# -- book-loading -------------------------------------------------------------- #

def test_book_loading_pnl_and_k_excursion_vs_twin():
    phase = [0] * 5 + [1] * 10 + [2] * 20 + [3] * 5 + [4] * 10
    k = [0.75] * 15 + [0.70] * 20 + [0.72] * 15
    fr = _frames(50, bl_phase=phase, buckK=[int(x * E18) for x in k],
                 bl_pnl=-1_000_000 * E6, bl_loaded_frac=0.25, bl_loaded=4 * 10 ** 12,
                 bl_unwound=3 * 10 ** 12, bl_k_load=0.75, bl_k_unwind=0.70,
                 bl_held_days=20)
    twin = _frames(50)                        # K 0.75 throughout
    b = eq.book_loading(fr, twin)
    assert b["pnl_m"] == pytest.approx(-1.0)
    assert b["loaded_frac"] == pytest.approx(0.25)
    assert b["k_exc_max"] == pytest.approx(0.05)
    assert b["k_exc_per_frac"] == pytest.approx(0.2)
    assert b["held_days"] == 20 and b["phase"] == 4
    assert b["p10_pnl"] is True
    assert eq.book_loading(fr, None)["k_exc_max"] is None
    assert eq.book_loading(_frames(5), twin) is None


# -- attribution --------------------------------------------------------------- #

def test_attribution_shares_and_flags():
    n = 11
    up = [int(0.01 * i * E18) for i in range(n)]            # price loop +0.01 / frame
    q = [int(-0.005 * i * E18) for i in range(n)]           # position loop -0.005 / frame
    stale = [1 if i == 3 else 0 for i in range(n)]
    fr = _frames(n, pid_up=up, pid_ui=0, pid_ud=0, pid_q=q, pid_qi=0, pid_qd=0,
                 sh_stale=stale, sh_excluded=0)
    a = eq.attribution(fr)
    assert a["tv_price"] == pytest.approx(0.10)
    assert a["tv_pos"] == pytest.approx(0.05)
    assert a["share_price"] == pytest.approx(2 / 3)
    assert a["share_pos"] == pytest.approx(1 / 3)
    assert a["stale_frames"] == 1 and a["excluded_frames"] == 0
    ser = eq.attribution_series(fr)
    assert len(ser) == n and ser[0]["dk"] is None
    assert ser[1]["dk_price"] == pytest.approx(0.01)
    assert ser[1]["dk_pos"] == pytest.approx(-0.005)
    assert ser[3]["stale"] is True
    assert eq.attribution(_frames(5)) is None


# -- the injectors' windows and the panel ----------------------------------- #

def test_d7_windows_from_the_injectors_counters():
    pushes = [0, 0, 1, 1, 1, 2, 2]
    phase = [0, 1, 1, 2, 2, 3, 4]
    exits = [0, 0, 0, 0, 1, 1, 1]
    fr = _frames(7, pu_pushes=pushes, bl_phase=phase, lx_exits=exits)
    w = eq.d7_windows(fr)
    kinds = [(x["kind"], x["day0"], x["day1"]) for x in w]
    assert ("trip", 2, 2) in kinds and ("trip", 5, 5) in kinds
    assert ("load", 1, 2) in kinds and ("hold", 3, 4) in kinds
    assert ("lpexit", 4, 4) in kinds
    assert all(x["src"] == "d7" for x in w)


def test_d7_panel_end_to_end(tmp_path):
    n = 120
    s = [0.0] * 20 + [0.02 * math.exp(-(i - 20) / 15.0) for i in range(20, n)]
    fr = _frames(n, sh_s=_s18(s), sh_sat=0, sh_stale=0, sh_excluded=0,
                 pid_up=[int(0.001 * i * E18) for i in range(n)], pid_ui=0,
                 pid_ud=0, pid_q=[int(-0.0005 * i * E18) for i in range(n)],
                 pid_qi=0, pid_qd=0, ut_absorbed_open=E6, ut_issued_open=0,
                 pu_pushes=[0] * 20 + [1] * (n - 20))
    meta = {"experiment": {"deploy": {"tau_i_days": 30.0, "kmin": 0.0,
                                      "kmax": 0.95}}}
    wins = eq.d7_windows(fr)
    p = eq.d7_panel(fr, meta, wins)
    assert p["tau_s_days"] == 30.0
    assert p["disturbance"] == {"day0": 20, "day1": 20, "n": 1}
    assert p["habituation"]["end_day"] == 20 and p["habituation"]["p9"] is True
    assert p["carry"]["ut"] == pytest.approx(n)
    assert p["attribution"]["share_pos"] == pytest.approx(1 / 3)
    assert p["book_loading"] is None
    # the wrapped summarize carries the panel and the injector windows
    vec = tmp_path / "v.json"
    vec.write_text(json.dumps({"tokens": [], "decimals": [], "frames": fr,
                               "meta": meta}))
    st = eq.summarize(vec)
    assert st["d7"]["habituation"]["t_hab"] == p["habituation"]["t_hab"]
    assert any(x["kind"] == "trip" for x in st["excursions"])
    assert "hab[" in eq.d7_row(st)
    ok, _reasons = eq.accept(st)
    assert isinstance(ok, bool)


# -- star.py: values axes, presets, compound levels ------------------------- #

STAR = """
name = "t"
[baseline]
"scenario.agents.ExcursionArbAgent" = 8
[presets.design]
A = { SIM_CONTROLLER = "direct" }
S = { SIM_CONTROLLER = "shadow", SIM_SHADOW_MODE = "s", SIM_SHADOW_KQ = 0.1 }
V = { SIM_CONTROLLER = "shadow", SIM_SHADOW_MODE = "v", SIM_SHADOW_KQ = 0.1 }
[[arm]]
name = "none"
toml = "catalogue-none.toml"
[[arm]]
name = "dump"
toml = "catalogue-dump.toml"
[[axis]]
name = "kp"
key = "deploy.kp_frac"
base = 0.02
lo = 0.0
hi = 0.05
[[axis]]
name = "design"
preset = "design"
base = "S"
values = ["A", "S", "V"]
[[axis]]
name = "depth"
key = "deploy.target_buck_m"
values = [40.0]
[[axis]]
name = "cap"
base = { "agents.UndertakingAgent.leg_bp" = 40, SIM_OPS_LEG_BP = 40 }
lo = { "agents.UndertakingAgent.leg_bp" = 10, SIM_OPS_LEG_BP = 10 }
hi = { "agents.UndertakingAgent.leg_bp" = 160, SIM_OPS_LEG_BP = 160 }
arms = ["dump"]
"""


def test_star_values_presets_and_compound_axes(tmp_path):
    p = tmp_path / "t.toml"
    p.write_text(STAR)
    spec = star.load_star(p)
    cells = star.cells(spec, tmp_path / "out")
    labels = [c["label"] for c in cells]
    # none: base + kp lo/hi + design A, V + depth 40 = 6; dump: + cap lo/hi = 8
    assert labels == ["none-base", "none-kp-lo", "none-kp-hi", "none-design-A",
                      "none-design-V", "none-depth-40.0",
                      "dump-base", "dump-kp-lo", "dump-kp-hi", "dump-design-A",
                      "dump-design-V", "dump-depth-40.0", "dump-cap-lo",
                      "dump-cap-hi"]
    by = {c["label"]: c for c in cells}
    # the design base (S) is pinned in every cell but the design cells
    assert by["none-base"]["env"] == {"SIM_CONTROLLER": "shadow",
                                      "SIM_SHADOW_MODE": "s", "SIM_SHADOW_KQ": "0.1"}
    assert by["none-kp-lo"]["env"]["SIM_SHADOW_MODE"] == "s"
    assert by["none-design-A"]["env"] == {"SIM_CONTROLLER": "direct",
                                          "SIM_SHADOW_MODE": "s", "SIM_SHADOW_KQ": "0.1"}
    assert by["none-design-V"]["env"]["SIM_SHADOW_MODE"] == "v"
    assert by["none-design-A"]["value"] == "A" and by["none-design-A"]["level"] == "A"
    # a values axis on a --set key
    assert "deploy.target_buck_m=40.0" in by["none-depth-40.0"]["sets"]
    assert not any(s.startswith("deploy.target_buck_m=") for s in by["none-base"]["sets"])
    # the compound axis: base pinned on dump only, lo / hi move both channels
    assert "agents.UndertakingAgent.leg_bp=40" in by["dump-base"]["sets"]
    assert by["dump-base"]["env"]["SIM_OPS_LEG_BP"] == "40"
    assert "agents.UndertakingAgent.leg_bp=160" in by["dump-cap-hi"]["sets"]
    assert by["dump-cap-hi"]["env"]["SIM_OPS_LEG_BP"] == "160"
    assert not any(s.startswith("agents.UndertakingAgent.leg_bp=") for s in by["none-base"]["sets"])
    # the WP-12 axis is untouched and the baseline set survives
    assert "deploy.kp_frac=0.0" in by["dump-kp-lo"]["sets"]
    assert "scenario.agents.ExcursionArbAgent=8" in by["dump-cap-hi"]["sets"]
    # --axes restricts the WP-15 axes too
    only = star.cells(spec, tmp_path / "out", axes=["design"])
    assert [c["label"] for c in only if c["arm"] == "none"] == \
        ["none-base", "none-design-A", "none-design-V"]


def test_star_wp12_spec_counts_are_unchanged():
    spec = star.load_star(star.STARS / "k-integral-c0.toml")
    cells = star.cells(spec, Path("/tmp/x"))
    assert len(cells) == 23                    # 1 + 2A per arm, A = 4 / 4 / 3
    assert all("value" not in c for c in cells)


def test_star_validation_errors(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('name = "b"\n[[arm]]\nname = "none"\ntoml = "catalogue-none.toml"\n'
                   '[[axis]]\nname = "d"\npreset = "design"\nvalues = ["A"]\n')
    with pytest.raises(SystemExit):
        star.load_star(bad)
    bad.write_text('name = "b"\n[[arm]]\nname = "none"\ntoml = "catalogue-none.toml"\n'
                   '[[axis]]\nname = "d"\nkey = "deploy.k0"\nvalues = []\n')
    with pytest.raises(SystemExit):
        star.load_star(bad)
