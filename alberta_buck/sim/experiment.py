"""Experiment harness -- declarative initial conditions + scripted mid-run
interventions for the equilibrium simulation.

An experiment is a TOML file with four sections (all optional -- defaults
reproduce the standard macro run):

    name = "baseline-5yr"
    notes = "free text, recorded into the vector"

    [scenario]            # window + cadence + population
    years = 5.0           # or start = "2020-07-01" / end = "2025-06-30"
    ticks_per_day = 1
    day_step = 10
    seed = 0xA1BC
    [scenario.agents]     # count overrides, merged over the defaults
    FatCreditBorrowerAgent = 5

    [deploy]              # controller + pool-depth initial conditions
    k0 = 0.75             # neutral feed-forward LTV
    kmax = 0.95           # rails; 0.95 keeps "railed" solvent (1/(1-K)=20x)
    tau_i_days = 90.0     # integral time (with dk_rail/e_max -> Ki)

    [agents.SaverAgent]   # per-class knob overrides for agent setup():
    base_rate_k = [300, 700]   # [lo, hi] -> seeded draw; scalar -> constant

    [[interventions]]     # day-indexed scripted actions (see Interventions)
    day = 600
    action = "retune"
    tau_i_days = 45.0

The RESOLVED experiment (defaults <- file <- --set overrides) plus the
applied-intervention log are embedded in the output vector, so every run is
self-describing and reproducible.

Intervention actions (the four surfaces):

  controller/governance:
    retune      {kp,ki,kd real-gain floats | dk_rail,e_max,tau_i_days,kp_frac}
                bumpless gain change (integrator re-derived, no output step)
    set_gains   same params, RAW setGains (output may step)  -- contrast case
    set_rails   {kmin,kmax}      floats; live K clamped into the new rails
    set_k0      {k0}             bumpless feed-forward move
    set_dt      {seconds}        PID pacing
    set_dtmax   {days}           integration-step clamp

  agent knobs:
    set_knob    {cls, idx="all"|int|list, knob, value|scale}
                per-class ordinal idx (0-based creation order)
    fund        {cls, idx, usdc_m}    mint USDC to proxies (+bump budget)

  exogenous shocks:
    price_shock  {token, mult[, from_day]}   multiplies the CSV reference
                 from that day on (whale re-pins pools to the shocked ref)
    uptake_shock {idx="all"|..., mag}        borrowers: +mag adoption step

  population:
    add_agents    {cls, n}                   construct + setup mid-run
    remove_agents {cls, idx, mode="wind_down"|"hard"}
                  wind_down: util_target -> 0 (retires via the normal loop)
                  hard: stops acting (positions freeze where they stand)
"""

from __future__ import annotations

import ast
import copy
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

E6 = 10 ** 6
E18 = 10 ** 18
M6 = 1_000_000 * 10 ** 6          # $1M in 6-dec token units

# ---------------------------------------------------------------------------
# Defaults -- reproduce the standard macro run (EQUILIBRIUM.md) except
# kmax = 0.95: the banked K=1.0 ceiling is the infinite-leverage boundary
# (1/(1-K)), so a controller pinned there is meaningless; 0.95 keeps a
# railed K solvent and makes "pinned vs settled" observable.
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "name": "unnamed",
    "notes": "",
    "scenario": {
        "years": 5.0,
        "start": "",
        "end": "",
        "days": 0,                 # 0 => full window
        "ticks_per_day": 1,
        "day_step": 10,
        "seed": 0xA1BC,
        "basket": "prorata",
        "agents": {},              # count overrides
    },
    "deploy": {
        "k0": 0.75,
        "kmin": 0.0,
        "kmax": 0.95,
        # Gains: raw real-gain overrides (kp/ki/kd) win when present;
        # otherwise derived: Ki = dk_rail/(e_max*tau_I), Kp = kp_frac*dk_rail/e_max.
        "kp": None,
        "ki": None,
        "kd": 0.0,
        "dk_rail": 0.5,
        "e_max": 0.10,
        "tau_i_days": 90.0,
        "kp_frac": 0.02,
        "dt": 1800,
        "dtmax_days": 0.0,         # 0 => unclamped (uint256.max)
        "target_buck_m": 10.0,     # $M common TOKEN/BUCK pool depth
        "target_buck_lp_m": 10.0,  # $M BUCK/USDC seed
    },
    "agents": {},                  # {cls: {knob: [lo,hi] | scalar}}
    "interventions": [],
}


def derive_gains(dk_rail=0.5, e_max=0.10, tau_i_days=90.0, kp_frac=0.02,
                 kd=0.0, kp=None, ki=None):
    """(KP, KI, KD) scaled real*1e12 for the ppm controller.  Raw real-gain
    kp/ki win over the derived (rail-authority / integral-time) form."""
    kp_real = kp if kp is not None else kp_frac * dk_rail / e_max
    ki_real = ki if ki is not None else dk_rail / (e_max * tau_i_days * 86400.0)
    return (int(round(kp_real * 1e12)), int(round(ki_real * 1e12)),
            int(round((kd or 0.0) * 1e12)))


def deploy_params(exp) -> SimpleNamespace:
    """Deploy-time knobs (controller init + pool depths), defaulted or from
    the experiment's [deploy] section.  Everything deploy.py needs."""
    dep = dict(DEFAULTS["deploy"])
    if exp is not None:
        dep.update(exp.deploy)
    kp, ki, kd = derive_gains(
        dk_rail=dep["dk_rail"], e_max=dep["e_max"],
        tau_i_days=dep["tau_i_days"], kp_frac=dep["kp_frac"],
        kd=dep["kd"], kp=dep.get("kp"), ki=dep.get("ki"))
    return SimpleNamespace(
        k0_wei=int(dep["k0"] * E18),
        kmin_wei=int(dep["kmin"] * E18),
        kmax_wei=int(dep["kmax"] * E18),
        kp_scaled=kp, ki_scaled=ki, kd_scaled=kd,
        dt=int(dep["dt"]),
        dtmax_secs=int(dep["dtmax_days"] * 86400),
        target_buck=int(dep["target_buck_m"] * M6),
        target_buck_lp=int(dep["target_buck_lp_m"] * M6),
    )


# ---------------------------------------------------------------------------
# Loading + merging
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _apply_set(cfg: dict, expr: str) -> None:
    """Apply one --set 'a.b.c=value' override; value parsed as a Python
    literal (list/number/bool/string) with bare-word fallback to str."""
    path, _, raw = expr.partition("=")
    if not _:
        raise ValueError(f"--set needs key=value, got {expr!r}")
    try:
        val = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        val = raw
    node = cfg
    keys = path.strip().split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = val


class Experiment:
    """Resolved experiment config: DEFAULTS <- TOML file <- --set overrides."""

    def __init__(self, cfg: dict, source: str = ""):
        self.cfg = cfg
        self.source = source
        self.name = cfg.get("name", "unnamed")
        self.notes = cfg.get("notes", "")
        self.scenario = cfg["scenario"]
        self.deploy = cfg["deploy"]
        self.agents = cfg.get("agents", {})
        self.interventions = cfg.get("interventions", [])

    def knob(self, cls_name: str, name: str, default):
        """The knob spec for an agent class: [lo, hi] range or scalar."""
        spec = self.agents.get(cls_name, {}).get(name)
        return default if spec is None else spec

    def resolved(self) -> dict:
        """JSON-safe resolved config for embedding in the vector."""
        return json.loads(json.dumps(
            {"source": self.source, **self.cfg}, default=str))


def load(path: str | Path | None = None, sets: list[str] | None = None
         ) -> Experiment:
    cfg = {}
    src = ""
    if path:
        p = Path(path)
        cfg = tomllib.loads(p.read_text())
        src = str(p)
    merged = _deep_merge(DEFAULTS, cfg)
    for expr in (sets or []):
        _apply_set(merged, expr)
    if not path and not merged.get("name"):
        merged["name"] = "cli"
    return Experiment(merged, source=src)


def build(exp: Experiment):
    """Scenario for this experiment (equilibrium family), with the
    experiment attached so deploy/loop/agents see the overrides."""
    from alberta_buck.sim.scenario import build_equilibrium
    s = exp.scenario
    sc = build_equilibrium(
        start=s["start"] or None, end=s["end"] or None,
        years=s["years"] or None,
        ticks_per_day=int(s["ticks_per_day"]),
        seed=int(s["seed"]))
    if s["agents"]:
        sc.agents = {**sc.agents, **{k: int(v) for k, v in s["agents"].items()}}
    if s["days"]:
        sc.days = min(int(s["days"]), sc.prices.days)
    sc.day_step = int(s["day_step"])
    sc.experiment = exp
    return sc


# ---------------------------------------------------------------------------
# Agent knob draws
# ---------------------------------------------------------------------------

def spec(scenario, cls_name: str, name: str, default):
    """Resolve a knob spec ([lo,hi] or scalar) from the scenario's attached
    experiment, falling back to `default`."""
    exp = getattr(scenario, "experiment", None)
    if exp is None:
        return default
    return exp.knob(cls_name, name, default)


def sample(sp, rng):
    """Sample a knob spec: [lo,hi] of ints -> randint, of floats -> uniform;
    scalar -> the constant."""
    if isinstance(sp, (list, tuple)):
        lo, hi = sp
        if isinstance(lo, int) and isinstance(hi, int):
            return rng.randint(lo, hi)
        return rng.uniform(float(lo), float(hi))
    return sp


def draw(scenario, cls_name: str, name: str, rng, default):
    """Resolve (experiment [agents.<cls>] overrides the coded default) and
    sample a knob in one step."""
    return sample(spec(scenario, cls_name, name, default), rng)


# ---------------------------------------------------------------------------
# Price overlay (exogenous price shocks)
# ---------------------------------------------------------------------------

class PriceOverlay:
    """Wraps Prices; multiplies token references from a given day on.  The
    whale re-pins TOKEN/USDC to the shocked reference, so a shock propagates
    through the same market plumbing as any real price move.  Keyed off the
    `day` argument, so day-0 baselines read through unshocked."""

    def __init__(self, base):
        self._base = base
        self._shocks: list[tuple[int, int, float]] = []   # (tok, from_day, mult)

    @property
    def days(self) -> int:
        return self._base.days

    def shock(self, token_idx: int, from_day: int, mult: float) -> None:
        self._shocks.append((int(token_idx), int(from_day), float(mult)))

    def ref(self, token_idx: int, day: int) -> int:
        p = self._base.ref(token_idx, day)
        for tok, fd, m in self._shocks:
            if tok == token_idx and day >= fd:
                p = int(p * m)
        return max(1, p)

    def day0(self, token_idx: int) -> int:
        return self.ref(token_idx, 0)


# ---------------------------------------------------------------------------
# Interventions engine
# ---------------------------------------------------------------------------

class Interventions:
    """Applies the experiment's day-indexed intervention schedule.  Owned by
    the run loop; `apply_due(day)` fires everything scheduled at or before
    `day` (once), in file order.  Every application (or failure) is recorded
    in `self.applied`, which the loop embeds in the vector."""

    def __init__(self, exp: Experiment, d, scenario, agents: list,
                 arbs: list, ctr: dict, rng):
        self.exp = exp
        self.d = d
        self.scenario = scenario
        self.agents = agents          # full population (loop's list object)
        self.arbs = arbs              # acting order pool (loop's list object)
        self.ctr = ctr
        self.rng = rng
        self.pending = sorted(
            (dict(iv) for iv in exp.interventions),
            key=lambda iv: int(iv.get("day", 0)))
        self.applied: list[dict] = []

    # -- helpers ---------------------------------------------------------- #

    def _gov(self, fn):
        return self.d.chain.send(fn, sender=self.d.gov)

    def _of_class(self, cls: str) -> list:
        return [a for a in self.agents if type(a).__name__ == cls]

    def _select(self, iv) -> list:
        """Agents matched by cls + per-class ordinal idx ('all'|int|list)."""
        pool = self._of_class(iv["cls"])
        idx = iv.get("idx", "all")
        if idx == "all":
            return pool
        if isinstance(idx, int):
            idx = [idx]
        return [pool[i] for i in idx if 0 <= i < len(pool)]

    def _token_index(self, sym: str) -> int:
        for i, t in enumerate(self.scenario.tokens):
            if t[0] == sym:
                return i
        raise ValueError(f"unknown token {sym!r}")

    # -- application ------------------------------------------------------ #

    def apply_due(self, day: int) -> None:
        while self.pending and int(self.pending[0].get("day", 0)) <= day:
            iv = self.pending.pop(0)
            try:
                note = self._apply(iv, day)
                ok = True
            except Exception as e:
                note = f"FAILED: {e!r}"[:300]
                ok = False
            self.applied.append({**iv, "applied_day": day, "ok": ok,
                                 "note": note})
            self.ctr["ivEvents"] = self.ctr.get("ivEvents", 0) + 1
            self.ctr["ivNote"] = note
            print(f"[iv] day {day}: {iv.get('action')} -> {note}", flush=True)

    def _apply(self, iv: dict, day: int) -> str:
        act = iv["action"]
        d = self.d

        # ---- controller / governance ---------------------------------- #
        if act in ("retune", "set_gains"):
            dep = self.exp.deploy
            kp, ki, kd = derive_gains(
                dk_rail=iv.get("dk_rail", dep["dk_rail"]),
                e_max=iv.get("e_max", dep["e_max"]),
                tau_i_days=iv.get("tau_i_days", dep["tau_i_days"]),
                kp_frac=iv.get("kp_frac", dep["kp_frac"]),
                kd=iv.get("kd", dep["kd"]),
                kp=iv.get("kp"), ki=iv.get("ki"))
            fn = (d.kctrl.functions.retune if act == "retune"
                  else d.kctrl.functions.setGains)
            self._gov(fn(kp, ki, kd))
            return f"gains KP={kp} KI={ki} KD={kd} ({act})"
        if act == "set_rails":
            kmin = int(iv["kmin"] * E18)
            kmax = int(iv["kmax"] * E18)
            self._gov(d.kctrl.functions.setRails(kmin, kmax))
            return f"rails [{iv['kmin']}, {iv['kmax']}]"
        if act == "set_k0":
            self._gov(d.kctrl.functions.setBuckK0(int(iv["k0"] * E18)))
            return f"buckK0 {iv['k0']}"
        if act == "set_dt":
            self._gov(d.kctrl.functions.setDT(int(iv["seconds"])))
            return f"dT {iv['seconds']}s"
        if act == "set_dtmax":
            self._gov(d.kctrl.functions.setDTMax(int(iv["days"] * 86400)))
            return f"dTMax {iv['days']}d"

        # ---- agent knobs ------------------------------------------------ #
        if act == "set_knob":
            sel = self._select(iv)
            knob = iv["knob"]
            hit = 0
            for a in sel:
                if not hasattr(a, knob):
                    continue
                old = getattr(a, knob)
                new = iv["value"] if "value" in iv else old * iv["scale"]
                if isinstance(old, int) and not isinstance(new, int):
                    new = int(new)
                setattr(a, knob, new)
                hit += 1
            return f"{iv['cls']}.{knob} set on {hit}/{len(sel)} agents"
        if act == "fund":
            sel = self._select(iv)
            amt = int(iv["usdc_m"] * M6)
            hit = 0
            for a in sel:
                proxy = getattr(a, "proxy", None)
                if proxy is None:
                    continue
                d.chain.send(d.usdc.functions.mint(proxy.address, amt))
                for battr in ("budget", "fund_budget"):
                    if hasattr(a, battr):
                        setattr(a, battr, getattr(a, battr) + amt)
                hit += 1
            return f"funded {hit} {iv['cls']} with ${iv['usdc_m']}M each"

        # ---- exogenous shocks ------------------------------------------- #
        if act == "price_shock":
            ti = self._token_index(iv["token"])
            from_day = int(iv.get("from_day", day))
            self.scenario.prices.shock(ti, from_day, float(iv["mult"]))
            return f"{iv['token']} x{iv['mult']} from day {from_day}"
        if act == "uptake_shock":
            sel = self._select({**iv, "cls":
                                iv.get("cls", "FatCreditBorrowerAgent")})
            mag = float(iv["mag"])
            for a in sel:
                a.extra_cap = getattr(a, "extra_cap", 0.0) + mag
            return f"uptake +{mag} on {len(sel)} borrowers"

        # ---- population -------------------------------------------------- #
        if act == "add_agents":
            from alberta_buck.sim.agents import REGISTRY
            cls = REGISTRY[iv["cls"]]
            n = int(iv.get("n", 1))
            nxt = max((a.idx for a in self.agents), default=-1) + 1
            added = []
            for k in range(n):
                a = cls(nxt + k)
                a.setup(self.d, self.scenario, self.rng)
                self.agents.append(a)
                self.arbs.append(a)
                added.append(a.idx)
            return f"added {n} {iv['cls']} (idx {added})"
        if act == "remove_agents":
            sel = self._select(iv)
            mode = iv.get("mode", "wind_down")
            for a in sel:
                if mode == "hard":
                    if a in self.arbs:
                        self.arbs.remove(a)
                    a.removed = True
                else:
                    if hasattr(a, "util_target"):
                        a.util_target = 0.0
                    if hasattr(a, "base_rate"):
                        a.base_rate = 0
            return f"{mode} {len(sel)} {iv['cls']}"

        raise ValueError(f"unknown intervention action {act!r}")
