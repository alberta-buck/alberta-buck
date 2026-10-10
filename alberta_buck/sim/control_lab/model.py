"""Pure configuration, deterministic feeds and experiment dependencies."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import tomllib
from pathlib import Path


STAGES = {
    "feeds": dict(title="01 · External prices only"),
    "monitor": dict(title="02 · Monitor a replayed price trace", monitor=True, replay=True),
    "monitor-k": dict(title="03 · Check K against the replay", monitor=True, replay=True, k=True),
    "linked": dict(title="04 · Link the markets", monitor=True, arb=True),
    "basket": dict(title="05 · Enable basket rebalancing", monitor=True, arb=True, basket=True),
    "k": dict(title="06 · Let K move", monitor=True, arb=True, basket=True, k=True),
    "desk": dict(title="07 · Add the desk", monitor=True, arb=True, basket=True, k=True, desk=True),
    "demand": dict(title="08 · Demand, without a supplier", monitor=True, arb=True, basket=True, k=True, demand=True),
    "supply": dict(title="09 · Supply, without a buyer", monitor=True, arb=True, basket=True, k=True, supply=True),
    "both": dict(title="10 · Both private responses", monitor=True, arb=True, basket=True, k=True, demand=True, supply=True),
    "all": dict(title="11 · Private responses and desk", monitor=True, arb=True, basket=True, k=True, demand=True, supply=True, desk=True),
}

DEFAULTS = {
    "days": 120, "ticks_per_day": 4, "seed": 41404,
    "feed": {"base": [100., 100., 100.], "amplitude": .12, "period_days": 30.,
             "walk_amplification": 0., "walk_daily_sigma": .01},
    "bootstrap": {"history_days": 1280, "basket_warmup_days": 0},
    "market": {"depth_usd": 1_000_000, "basket_equity_usd": 1_000_000,
               "arb_rounds": 2, "arb_cap_bp": 200, "arb_beneficiary": "external"},
    "monitor": {"interval": 1800, "max_age": 3600, "deviation_bp": 500},
    "controller": {"k0": .75, "kmin": .20, "kmax": .95,
                   "tau_i_days": 90., "kp_frac": .02},
    "desk": {"capital_per_token": 400_000, "turnover_buck": 1_200_000,
             "capital_floor_buck": 300_000, "enter_bp": 300, "release_bp": 200,
             "gain_bp": 2500, "cap_bp": 40, "persist_days": 30},
    "demand": {"budget_usdc": 1_000_000, "max_per_day": 25_000,
               "rate_at_par_per_day": 5_000, "backoff_bp": 100},
    "supply": {"credit_face_buck": 1_000_000, "max_per_day": 25_000,
               "rate_at_par_per_day": 5_000, "backoff_bp": 100},
    "basket": {"leash_bp": 3000, "tilt": 1., "weight_band_bp": 200},
    # PairRebalancePolicy.Sizing: speeds are log returns/day, rates gap fractions/day.
    # These are the first experiment's values; closure_rate=0 leaves closure to speed alone,
    # and release_speed=0 would scale the braking release by the rate's own allowance.
    # slack is a share of the book and patience is share-days (0.02: two point-days);
    # patience=0 leaves eligibility to the basket's fixed weight band.
    "pair": {"release_speed": .002, "release_rate": .1, "closure_gain": 20, "closure_rate": 0.,
             "max_rate": 1., "impact_share": 1., "slack": 0., "patience": 0.},
    "profiles": [{"name": "three-phase", "perturbations": []}],
}


def load_config(path: str | Path) -> dict:
    raw = tomllib.loads(Path(path).read_text())
    cfg = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if key not in cfg:
            raise ValueError(f"unknown configuration section: {key}")
        if isinstance(value, dict):
            unknown = value.keys() - cfg[key].keys()
            if unknown:
                raise ValueError(f"unknown {key} settings: {sorted(unknown)}")
            cfg[key].update(value)
        else:
            cfg[key] = value
    validate(cfg)
    return cfg


def validate(cfg: dict) -> None:
    if not isinstance(cfg["days"], int) or not 1 <= cfg["days"] <= 3660:
        raise ValueError("days must be an integer in 1..3660")
    tpd = cfg["ticks_per_day"]
    if not isinstance(tpd, int) or not 1 <= tpd <= 48 or 86400 % tpd:
        raise ValueError("ticks_per_day must divide 86400, in 1..48")
    feed = cfg["feed"]
    if len(feed["base"]) != 3 or any(p <= 0 for p in feed["base"]):
        raise ValueError("exactly three positive base prices are required")
    if not 0 <= feed["amplitude"] < 1 or feed["period_days"] <= 0:
        raise ValueError("invalid sine amplitude or period")
    if any(not math.isfinite(feed[k]) or feed[k] < 0
           for k in ("walk_amplification", "walk_daily_sigma")):
        raise ValueError("random walk amplification and daily sigma must be finite and nonnegative")
    history_days = cfg["bootstrap"]["history_days"]
    if not isinstance(history_days, int) or not 1280 <= history_days <= 20000:
        raise ValueError("bootstrap.history_days must be in 1280..20000")
    warm_days = cfg["bootstrap"]["basket_warmup_days"]
    if not isinstance(warm_days, int) or not 0 <= warm_days <= 365:
        raise ValueError("bootstrap.basket_warmup_days must be in 0..365")
    mon = cfg["monitor"]
    if not 0 < mon["interval"] <= mon["max_age"] <= 86400 or not 0 < mon["deviation_bp"] <= 1000:
        raise ValueError("invalid monitor pacing or tolerance")
    if cfg["market"]["depth_usd"] <= 0 or cfg["market"]["basket_equity_usd"] <= 0:
        raise ValueError("pool depth and basket capital must be positive")
    if not 1 <= cfg["market"]["arb_rounds"] <= 20 or not 0 < cfg["market"]["arb_cap_bp"] <= 1000:
        raise ValueError("invalid arbitrage work bound")
    if cfg["market"].get("arb_beneficiary", "external") not in ("external", "basket"):
        raise ValueError("arb_beneficiary must be external or basket")
    band = cfg["basket"]["weight_band_bp"]
    if not isinstance(band, int) or not 0 <= band <= 1000:
        raise ValueError("basket.weight_band_bp must be an integer in 0..1000")
    pair = cfg["pair"]
    if (not isinstance(pair["closure_gain"], int) or not 0 <= pair["closure_gain"] <= 10_000
            or any(not math.isfinite(pair[k]) or not 0 <= pair[k] <= 18 for k in
                   ("release_speed", "release_rate", "closure_rate", "max_rate", "impact_share", "slack", "patience"))
            or pair["max_rate"] <= 0 or pair["impact_share"] > 1 or pair["slack"] >= 1):
        raise ValueError("invalid pair sizing")
    for name in ("demand", "supply"):
        a = cfg[name]
        if not 0 <= a["rate_at_par_per_day"] <= a["max_per_day"] or not a["backoff_bp"] > 0:
            raise ValueError(f"invalid {name} desired flow, daily cap or backoff")
    names = [p["name"] for p in cfg["profiles"]]
    if not names or len(names) != len(set(names)):
        raise ValueError("profile names must be nonempty and unique")
    if any(not n or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in n) for n in names):
        raise ValueError("profile names use lowercase letters, digits, hyphens and underscores")


def pair_sizing(pair: dict) -> tuple:
    """The PairRebalancePolicy.Sizing constructor argument."""
    return (round(pair["release_speed"] * 1e18), round(pair["release_rate"] * 1e18), pair["closure_gain"],
            round(pair["closure_rate"] * 1e18), round(pair["max_rate"] * 1e18), round(pair["impact_share"] * 1e18),
            round(pair["slack"] * 1e18), round(pair["patience"] * 1e18))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def baseline_prices(cfg: dict, day: float) -> list[float]:
    return [round(base * (1 + cfg["feed"]["amplitude"] * math.sin(
        2 * math.pi * day / cfg["feed"]["period_days"] + i * 2 * math.pi / 3)), 6)
        for i, base in enumerate(cfg["feed"]["base"])]


def own_path(profile: dict | None) -> bool:
    """A profile that supplies its whole price path takes no sine, walk or perturbation."""
    return bool(profile) and ("history" in profile or "commodities" in profile)


def history_rows(cfg: dict, profile: dict | None = None) -> list[dict]:
    """Strictly past history; synthetic disturbances and live endpoint pins stay out."""
    if profile and "history" in profile:
        from .historical import historical_inputs
        return historical_inputs(cfg, profile)["history"]
    if profile and "commodities" in profile:
        from .commodities import commodity_inputs
        return commodity_inputs(cfg, profile)["history"]
    return [{"day": day, "prices": baseline_prices(cfg, day)}
            for day in range(-cfg["bootstrap"]["history_days"], 0)]


def feed_rows(cfg: dict, profile: dict) -> list[dict]:
    """Fractional perturbations add to a positive DC-biased three-phase sine.

    Noise streams depend on seed + perturbation + token + tick, never on
    actor order or the number of workers. A common perturbation shares a
    noise draw across its selected tokens; independent=true splits it.
    """
    if "history" in profile:
        from .historical import historical_inputs
        if profile.get("perturbations") or cfg["feed"]["walk_amplification"]:
            raise ValueError("pinned historical comparison currently requires no extra disturbances")
        return historical_inputs(cfg, profile)["rows"]
    if "commodities" in profile:
        from .commodities import commodity_inputs
        if profile.get("perturbations") or cfg["feed"]["walk_amplification"]:
            raise ValueError("the commodity profile is a complete path and takes no extra disturbances")
        return commodity_inputs(cfg, profile)["rows"]
    # Independent Brownian log-price walks, with no drift in log space. The
    # seed excludes stage/profile/amplification, pairing all comparisons.
    # No day-zero shock; no walk enters the separate bootstrap history.
    walks = [0.] * 3
    streams = [random.Random(digest([cfg["seed"], "log-walk-v1", i])) for i in range(3)]
    sigma = cfg["feed"]["walk_daily_sigma"] / math.sqrt(cfg["ticks_per_day"])
    amplification = cfg["feed"]["walk_amplification"]
    rows = []
    for step in range(cfg["days"] * cfg["ticks_per_day"] + 1):
        if step:
            for i in range(3):
                walks[i] += sigma * streams[i].gauss(0., 1.)
        day = step / cfg["ticks_per_day"]
        values = [1 + cfg["feed"]["amplitude"] * math.sin(
            2 * math.pi * day / cfg["feed"]["period_days"] + i * 2 * math.pi / 3)
                  for i in range(3)]
        for j, p in enumerate(profile.get("perturbations", [])):
            kind = p["kind"]
            if kind not in {"sine", "triangle", "ramp", "step", "noise"}:
                raise ValueError(f"unknown perturbation: {kind}")
            tokens = p.get("tokens", [0, 1, 2])
            if not tokens or any(i not in (0, 1, 2) for i in tokens):
                raise ValueError("perturbation tokens must select 0, 1 and/or 2")
            start, duration = p.get("start", 0.), p.get("duration", cfg["days"] + 1.)
            if duration <= 0:
                raise ValueError("perturbation duration must be positive")
            age = day - start
            if age < 0 or (age >= duration and kind != "ramp"):
                continue
            a = float(p.get("amplitude", .1))
            if kind == "triangle":
                shape = max(0., 1 - abs(2 * age / duration - 1))
            elif kind == "ramp":
                shape = min(1., age / duration)  # hold the final level
            elif kind == "sine":
                period = p.get("period_days", duration)
                if period <= 0:
                    raise ValueError("sine period must be positive")
                shape = math.sin(2 * math.pi * age / period + p.get("phase", 0.))
            else:
                shape = 1.
            for i in tokens:
                if kind == "noise":
                    stream = i if p.get("independent", False) else "common"
                    seed = digest([cfg["seed"], j, stream, step])
                    shape = random.Random(seed).gauss(0., 1.)
                values[i] += a * shape
        prices = [round(cfg["feed"]["base"][i] * values[i] * math.exp(amplification * walks[i]), 6)
                  for i in range(3)]
        if any(not math.isfinite(p) or p <= 0 for p in prices):
            raise ValueError(f"nonpositive/nonfinite price in {profile['name']} at day {day}")
        rows.append({"day": day, "target_usd": prices})
    return rows


def dependencies(cfg: dict, profile: dict, stage: str) -> dict:
    """Only knobs used by this stage invalidate its cached simulation."""
    flags = STAGES[stage]
    keys = ["days", "ticks_per_day", "seed", "feed", "market"]
    # Fixed-K arms use k0/rails too; changing integral gains only affects K arms.
    resolved = {k: cfg[k] for k in keys}
    resolved["initial_k"] = {k: cfg["controller"][k] for k in ("k0", "kmin", "kmax")}
    for key in ("monitor", "desk", "demand", "supply", "basket"):
        if flags.get(key):
            resolved[key] = cfg[key]
    if flags.get("basket"):
        resolved["pair"] = cfg["pair"]
    if flags.get("monitor"):
        resolved["bootstrap"] = cfg["bootstrap"]
    if cfg["bootstrap"]["basket_warmup_days"]:
        # All arms start with the same settled LP book, including feeds-only.
        resolved["basket_prelude"] = dict(days=cfg["bootstrap"]["basket_warmup_days"],
            leash_bp=cfg["basket"]["leash_bp"], interval=cfg["monitor"]["interval"],
            weight_band_bp=cfg["basket"]["weight_band_bp"])
    if flags.get("k"):
        resolved["controller"] = cfg["controller"]
    inputs = {"config": resolved, "profile": profile, "stage": stage}
    if "history" in profile:
        from .historical import historical_inputs
        inputs["historical_source"] = historical_inputs(cfg, profile)["provenance"]
    return inputs
