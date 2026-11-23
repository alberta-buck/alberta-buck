"""Cross the canonical pair policy's outward braking scale and amplitude on matched EVM paths."""

import importlib.util
import itertools
import math
from pathlib import Path
import sys


HERE = Path(__file__).resolve()
CAPTURE = HERE.with_name("capture-basket-signals.py")
OUTWARD_SCALES = {"20-bp": .002, "100-bp": .01}  # log return per day at half release
BRAKING_RATES = {"1-per-day": 1., "4-per-day": 4.}  # fraction-rate per day before the cap


def load_script(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def capture():
    # Scope the extra constructor setting to this capture process. The same
    # Solidity policy, observer, upkeep and wheel run in every arm.
    from alberta_buck.sim import basket_machine

    capture_script = load_script(CAPTURE)
    base_sizing = basket_machine.sizing
    basket_machine.DEFAULTS["release_speed"] = 0.

    def sizing(settings):
        scale = settings["release_speed"]
        if not math.isfinite(scale) or not 0 <= scale < 1:
            raise ValueError("machine.release_speed must be a daily log return in [0, 1)")
        return (round(scale * 10**18), *base_sizing(settings)[1:])

    basket_machine.sizing = sizing
    run_economy = capture_script.run

    def run(scenario, backend, *, on_day_start, on_frame):
        expected = sizing(basket_machine.settings(scenario.experiment.cfg.get("machine")))

        def verify(day, deployment, agents, counters):
            if day == 0:
                policy = deployment.pair_policy.functions
                actual = tuple(getattr(policy, field)().call() for field in (
                    "releaseSpeed", "releaseRate", "closureGain", "closureRate",
                    "maxRate", "impactShare", "slack", "patience"))
                if actual != expected:
                    raise ValueError(f"PairRebalancePolicy constructor expected {expected}, got {actual}")
                print(f"Verified PairRebalancePolicy.Sizing on chain {actual}", flush=True)
            on_day_start(day, deployment, agents, counters)

        return run_economy(scenario, backend, on_day_start=verify, on_frame=on_frame)

    capture_script.run = run
    capture_script.main()


def main():
    grid = load_script(HERE.with_name("run-basket-signal-grid.py"))
    grid.__doc__ = __doc__
    grid.CAPTURE = HERE
    grid.REGIMES = ("history", "three-phase")
    base_arms = grid.arms
    grid.arms = lambda cross: (
        arm for arm in base_arms(cross) if arm[0] != "candidate"
    )
    base_plan = grid.plan

    def plan(args, runtime):
        runtime = {**runtime, "capture_sha256": grid.digest(CAPTURE.read_bytes())}
        manifest = base_plan(args, runtime)
        cells = []
        for control in manifest["cells"]:
            cells.append(control)
            if control["arm"] != "production":
                continue
            for (onset, scale), (speed, rate) in itertools.product(
                OUTWARD_SCALES.items(), BRAKING_RATES.items()
            ):
                ident = f"{control['group']}--candidate-{onset}-{speed}-braking"
                settings = [*control["settings"], f"machine.release_speed={scale}",
                            f"machine.release_rate={rate}"]
                fingerprint = grid.digest(grid.json_bytes([
                    ident, args.days, settings, control["price_digest"],
                    manifest["experiment_sha256"], runtime, grid.CODE_SIZE_LIMIT,
                ]))
                path = f"cells/{ident}-{fingerprint[:16]}"
                cells.append(dict(control, id=ident, arm="candidate", response="braking",
                                  onset=onset, speed=speed, settings=settings, fingerprint=fingerprint,
                                  outward_scale_bp_per_day=round(scale * 10_000),
                                  braking_rate_per_day=rate, output=path + ".json", log=path + ".log"))
        manifest["cells"] = cells
        manifest["policy_axes"] = {
            "onset": "Outward relative log return in bp/day at half braking release",
            "speed": "Maximum braking rate per day before the common final rate cap",
            "urgency": "Common gap slack and patience across all arms",
        }
        return manifest

    grid.plan = plan
    return grid.main()


if __name__ == "__main__":
    # The existing runner invokes a capture with a positional output path.
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        capture()
    else:
        sys.exit(main())
