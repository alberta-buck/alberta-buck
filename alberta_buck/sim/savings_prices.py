"""The savings sandbox's prices, for its world in the browser (core/js/src/scenarios/savings-world.js).

An experiment's TOKENs -- symbols, decimals, declared weights, day-0 prices -- and its daily reference
prices, the history window and its reverting twin, as the Python world resolves them
(experiment.build: gen_historical's window, scenario.keep_tokens, gen_prices.revert_like), written as
JSON. Standard library only (no web3, no kernels), so the sandbox's build runs it anywhere, the Pages
runner included:

    PYTHONPATH=. python3 -m alberta_buck.sim.savings_prices --out core/js/artifacts/savings-prices.json

Prices are USDC micro (6-dec) per whole TOKEN, as Prices.ref gives them.
"""
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from alberta_buck.sim.gen_historical import gen
from alberta_buck.sim.gen_prices import revert_like
from alberta_buck.sim.prices import Prices
from alberta_buck.sim.scenario import EQUILIBRIUM_TOKENS, Scenario, keep_tokens

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = ROOT / "alberta_buck/sim/experiments/economy.toml"


def resolve(path: str | Path = EXPERIMENT) -> dict:
    """The experiment's TOKENs and price paths, as experiment.build resolves them."""
    s = tomllib.loads(Path(path).read_text()).get("scenario", {})
    if s.get("family", "equilibrium") not in ("equilibrium", "rebalancing-eq"):
        raise ValueError("the savings world's prices follow the equilibrium worlds' basket")
    years = s.get("years")
    files, n_days, first, last = gen(start=s.get("start") or None, end=s.get("end") or None,
                                     years=1.5 if not years else float(years))
    sc = Scenario(name="savings", tokens=list(EQUILIBRIUM_TOKENS), csv_files=files, agents={}, days=n_days)
    if s.get("tokens"):
        keep_tokens(sc, list(s["tokens"]))
    history = sc.prices
    revert = Prices(revert_like(sc.csv_files))
    return {
        "experiment": str(Path(path).relative_to(ROOT)) if Path(path).is_relative_to(ROOT) else str(path),
        "start": str(first), "end": str(last), "days": history.days,
        "tokens": [{"sym": t[0], "name": t[1], "dec": t[2], "weightBp": t[3], "p0": history.day0(i)}
                   for i, t in enumerate(sc.tokens)],
        "series": {"history": [row[:history.days] for row in history.series],
                   "revert": [row[:history.days] for row in revert.series]},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--experiment", default=str(EXPERIMENT))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = resolve(a.experiment)
    out.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(f"{out}: {len(data['tokens'])} TOKENs, {data['days']} days from {data['start']}")


if __name__ == "__main__":
    main()
