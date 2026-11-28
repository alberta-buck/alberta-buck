"""Compare a small canonical economy on pyrevm and the Node WebEVM Savings world."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from alberta_buck.sim import experiment

ROOT = Path(__file__).resolve().parents[2]
ECONOMY = Path(__file__).with_name("experiments") / "economy.toml"
POP = "agents.HouseholdPopulationAgent"
OPENING_FIELDS = {
    "buck": ("totalSupply", "totalIssued"),
    "basket": ("totalShares", "treasuryShares", "lien", "sharePrice", "basketValueInBuck"),
    "kctrl": ("buckK", "dTMax", "leakTau"),
}


def replay_inputs(days, seed):
    """Resolve one price tape and opening cohort for both EVMs."""
    # This parity fixture compares the bootstrap shared with WebEVM.
    settings = [f"scenario.days={days}", f"scenario.seed={seed}", "scenario.startup_days=0",
                *(f"{POP}.initial_{role}s=1" for role in ("debtor", "saver", "investor"))]
    exp = experiment.load(ECONOMY, settings)
    scenario = experiment.build(exp)
    prices = scenario.prices.series
    dp = experiment.deploy_params(exp)
    spec = {
        "days": scenario.days,
        "opening_fields": OPENING_FIELDS,
        "sets": {"scenario.seed": scenario.seed, "scenario.ticks_per_day": scenario.ticks_per_day,
                 **{f"{POP}.{key}": value for key, value in exp.agents["HouseholdPopulationAgent"].items()}},
        "deployment": {
            "targetBuck": str(dp.target_buck), "targetBuckLp": str(dp.target_buck_lp),
            "controllerHorizon": str(dp.dtmax_secs) if dp.dtmax_secs else None,
            "params": {"kp": str(dp.kp_scaled), "ki": str(dp.ki_scaled), "kd": str(dp.kd_scaled),
                       "dt": str(dp.dt), "kmin": str(dp.kmin_wei), "kmax": str(dp.kmax_wei),
                       "k0": str(dp.k0_wei), "leakTau": str(dp.leak_secs)},
        },
        "prices": {
            "tokens": [{"sym": sym, "name": name, "dec": decimals, "weightBp": weight, "p0": series[0]}
                       for (sym, name, decimals, weight), series in zip(scenario.tokens, prices, strict=True)],
            # The tab's shocks and inter-tick interpolation need the next external close.
            "series": {"history": [series[:scenario.days + 1] for series in prices]},
        },
    }
    spec["price_digest"] = hashlib.sha256(json.dumps(spec["prices"], sort_keys=True).encode()).hexdigest()
    return scenario, spec


def python_opening(deployment):
    return {
        contract: {name: str(getattr(getattr(deployment, contract).functions, name)().call())
                   for name in fields}
        for contract, fields in OPENING_FIELDS.items()
    }


def capture_first_deposit(deployment, evidence):
    """Read the first deposit's pre-transaction price marks without advancing the EVM."""
    from alberta_buck.sim.chain import load_artifact

    d = deployment
    send = d.chain.send
    token_index = {token.address.lower(): i for i, token in enumerate(d.tokens)}

    def observe(fn, *args, **kwargs):
        if getattr(fn, "fn_name", None) == "depositToken" and fn.address == d.basket.address:
            d.chain.send = send
            token, amount, _ = fn.args
            i = token_index[token.lower()]
            pool = d.w3.eth.contract(address=d.pool_buck[i], abi=load_artifact("UniswapV3Pool")[0])
            sqrt, tick, *_ = pool.functions.slot0().call()
            window = d.basket.functions.twapWindow().call()
            old, new = pool.functions.observe([window, 0]).call()[0]
            gross, net = d.buck.functions.accountBalancesScaled(pool.address).call()
            low, twap, high = d.basket.functions.marks(i, 0).call()[3:6]
            evidence.update(
                token=i, amount=str(amount),
                buck_is_token0=pool.functions.token0().call() == d.buck.address,
                sqrt_price_x96=str(sqrt), tick=tick, twap_tick=(new - old) // window,
                carry_gross=str(gross), carry_net=str(net),
                price_low=str(low), price_twap=str(twap), price_high=str(high),
                basis_at_sample=str(amount * low // 10**d.dec[i]),
            )
        return send(fn, *args, **kwargs)

    d.chain.send = observe


def python_boot_actions(deployment, journal):
    from web3.logs import DISCARD

    deposits, wheel, samples = [], [], []
    tokens = {contract.address: i for i, contract in enumerate(deployment.tokens)}
    signals = deployment.signals
    decoders = [signals.events.Observed(), deployment.kctrl.events.BuckKUpdated(),
                deployment.basket.events.WheelWork(), deployment.basket.events.BasketArbitrage(),
                deployment.wheel.events.JubileeAttempted()]
    for entry in journal:
        if entry["fn"] not in ("depositToken", "tick", "observe"):
            continue
        receipt = deployment.w3.eth.get_transaction_receipt(entry["tx"])
        time = deployment.w3.eth.get_block(entry["block"])["timestamp"]
        if entry["fn"] == "depositToken":
            for event in deployment.basket.events.Deposited().process_receipt(receipt, errors=DISCARD):
                args = event["args"]
                holding = deployment.basket.functions.holdings(args["receiptId"]).call()
                deposits.append({"token": tokens[args["token"]], "amount": str(args["tokenAmount"]),
                                 "basis": str(holding[1]), "credit_mark": str(args["buckMinted"]),
                                 "timestamp": time})
        elif entry["fn"] == "tick":
            events = sorted((event for decoder in decoders for event in decoder.process_receipt(receipt, errors=DISCARD)),
                            key=lambda event: event["logIndex"])
            for event in deployment.wheel.events.Ticked().process_receipt(receipt, errors=DISCARD):
                wheel.append({"work": str(event["args"]["work"]), "timestamp": time,
                              "events": [boot_event(e) for e in events]})
        else:
            samples.extend(boot_event(e) for e in signals.events.Observed().process_receipt(receipt, errors=DISCARD))
    source = deployment.wheel.functions.sampleSource().call()
    origins = {signals.address: "price book"}
    if deployment.monetary_policy is not None:
        origins[deployment.monetary_policy.address] = "desk policy"
    return {"deposits": deposits, "wheel": wheel, "samples": samples,
            "sample_source": origins.get(source, "unknown")}


def boot_event(event):
    return {"name": event["event"], "args": {k: str(v) for k, v in event["args"].items()
                                              if k in ("at", "raw", "newBuckK", "kind", "i", "job", "work",
                                                       "route", "direction", "amount")}}


def python_frame(frame):
    return {
        "day": frame["day"],
        "reference": frame["refUsd"],
        "money": {key: str(frame[source]) for key, source in (
            ("k", "buckK"), ("basket_value", "basketVal"), ("supply", "supply"),
            ("share_price", "eq_share_price"), ("basket_lien", "eq_lien"))},
        "households": {
            agent["member"]: {"role": agent["role"], "arrival_day": agent["arrive_day"],
                              "principal": str(agent["drawn"]), "basis": str(agent["basket"])}
            for agent in frame["octl"] if "member" in agent
        },
    }


def first_difference(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        for key in dict.fromkeys([*left, *right]):
            child = f"{path}.{key}" if path else key
            if key not in left or key not in right:
                return child, left.get(key, "<missing>"), right.get(key, "<missing>")
            difference = first_difference(left[key], right[key], child)
            if difference is not None:
                return difference
        return None
    if left != right:
        return path, left, right
    return None


def compare(python, javascript):
    if len(python) != len(javascript):
        print(f"Frame count differs: pyrevm={len(python)}, WebEVM={len(javascript)}")
        return 1
    status = 0
    for section in ("day", "reference", "households", "money"):
        for expected, observed in zip(python, javascript, strict=True):
            difference = first_difference(expected[section], observed[section], section)
            if difference is not None:
                field, left, right = difference
                print(f"Day {expected['day']}, {field}: pyrevm={left!r}, WebEVM={right!r}")
                status = 1
                break
    return status


def compare_boot_actions(python, javascript, phases=("deposits", "wheel")):
    status = 0
    for phase in phases:
        left, right = python[phase], javascript[phase]
        for i, (a, b) in enumerate(zip(left, right), start=1):
            if phase == "wheel":
                a, b = wheel_shape(a), wheel_shape(b)
            difference = first_difference({k: v for k, v in a.items() if k != "timestamp"},
                                          {k: v for k, v in b.items() if k != "timestamp"})
            if difference is not None:
                field, expected, observed = difference
                print(f"Opening {phase} #{i}, {field}: pyrevm={expected!r}, WebEVM={observed!r}")
                status = 1
                break
        else:
            if len(left) != len(right):
                print(f"Opening {phase} count: pyrevm={len(left)}, WebEVM={len(right)}")
                status = 1
    return status


def wheel_shape(turn):
    fields = ("kind", "i", "job", "work", "route", "direction")
    return {"work": turn["work"], "jobs": [
        (row["name"], {key: row["args"][key] for key in fields if key in row["args"]})
        for row in turn["events"]]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--seed", type=int, default=41404)
    parser.add_argument("--output", type=Path, help="optional JSON evidence with opening actions and close frames")
    parser.add_argument("--check-opening-wheel", action="store_true",
                        help="require matching bootstrap wheel work, separate from exact ledger parity")
    parser.add_argument("--records", type=Path, help="write dashboard records for both runtimes")
    parser.add_argument("--record-prefix", default="economy-parity", help="prefix for the two run IDs")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 30:
        parser.error("--days must be between 1 and 30")
    if not 1 <= args.seed <= 2**53 - 1:
        parser.error("--seed must be a positive JavaScript-safe integer")
    if args.records is not None and not args.record_prefix.replace("-", "").isalnum():
        parser.error("--record-prefix must contain letters, digits or hyphens")

    from alberta_buck.sim.loop import run
    from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
    from alberta_buck.sim.chain import Journal

    scenario, spec = replay_inputs(args.days, args.seed)
    recorder = None
    js_id = f"{args.record_prefix}-webevm"
    if args.records is not None:
        from alberta_buck.sim.runrecord import RunRecorder, verified_complete
        py_id = f"{args.record_prefix}-pyrevm"
        if not verified_complete(args.records, py_id, scenario, spec["price_digest"], "pyrevm"):
            recorder = RunRecorder(args.records, "Economy · pyrevm", run_id=py_id)
        spec["record"] = not verified_complete(args.records, js_id, scenario, spec["price_digest"], "webevm")
    python = {"opening": None, "frames": [], "first_deposit_mark": {}}

    with tempfile.TemporaryDirectory(prefix="buck-savings-parity-") as directory:
        journal = Journal(Path(directory) / "pyrevm.jsonl")

        def capture_opening(deployment):
            if recorder is not None:
                recorder.manifest["source"] = {"driver": "loop.run", "price_digest": spec["price_digest"]}
            python["opening"] = python_opening(deployment)
            spec["clock"]["first_tick_timestamp"] = deployment.household_epoch
            actions = python_boot_actions(deployment, Journal.load(journal.path))
            python["boot_actions"] = actions
            spec["bootstrap_deposits"] = [{"token": row["token"], "amount": row["amount"]}
                                          for row in actions["deposits"]]

        try:
            with PyrevmAnvil() as backend:
                spec["clock"] = {"genesis_timestamp": backend.ts, "block_seconds": backend.block_seconds}
                run(scenario, backend, verbose=False, on_boot=capture_opening, journal=journal,
                    recorder=recorder,
                    on_bootstrap=lambda d: capture_first_deposit(d, python["first_deposit_mark"]),
                    on_frame=lambda frame: python["frames"].append(python_frame(frame)))
        except BaseException as exc:
            if recorder is not None and recorder.manifest:
                recorder.close("failed", error=exc)
            raise
        finally:
            journal.close()
        output = Path(directory) / "webevm.json"
        try:
            subprocess.run(["node", str(ROOT / "core/js/bin/savings-parity.mjs"), str(output)],
                           input=json.dumps(spec), text=True, check=True, cwd=ROOT)
            javascript = json.loads(output.read_text())
            if spec.get("record"):
                from alberta_buck.sim.savings_records import write_record
                write_record(args.records, js_id, scenario, spec, javascript)
                del javascript["record_frames"]
                del javascript["record_meta"]
        except BaseException as exc:
            if recorder is not None:
                recorder.close("failed", error=exc)
            raise
    if recorder is not None:
        recorder.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"inputs": spec, "pyrevm": python, "webevm": javascript}, indent=2) + "\n")
    if args.check_opening_wheel:
        left, right = python["boot_actions"], javascript["boot_actions"]
        if any(actions["sample_source"] != "desk policy" or not actions["samples"]
               or not any(int(row["work"]) for row in actions["wheel"]) for actions in (left, right)):
            print("Opening wheel must do work after the canonical desk publishes an initial sample.")
            return 1
        status = compare_boot_actions(left, right, phases=("wheel",))
        if not status:
            print(f"Opening wheel work agrees across {len(left['wheel'])} transactions.")
        return status
    status = compare_boot_actions(python["boot_actions"], javascript["boot_actions"])
    for runtime, result in (("pyrevm", python), ("WebEVM", javascript)):
        mark = result["first_deposit_mark"]
        direction = -1 if mark["buck_is_token0"] else 1
        print(f"{runtime} first deposit: TOKEN-to-BUCK tick {direction * mark['tick']}, "
              f"low mark {mark['price_low']}, sampled basis {mark['basis_at_sample']}")
    opening_diff = first_difference(python["opening"], javascript["opening"], "opening")
    if opening_diff is not None:
        field, left, right = opening_diff
        print(f"Before day 0, {field}: pyrevm={left!r}, WebEVM={right!r}")
    status |= compare(python["frames"], javascript["frames"]) | int(opening_diff is not None)
    if not status:
        print(f"Opening balances and {len(python['frames'])} days agree on the compared fields.")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
