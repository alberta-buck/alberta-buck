"""Replay sampled control-lab price tapes on the current household economy."""

import argparse
from collections import defaultdict
import fcntl
import importlib.util
import json
import math
from pathlib import Path
import sys

from alberta_buck.sim import experiment


GRID_PATH = Path(__file__).with_name("run-basket-signal-grid.py")
SPEC = importlib.util.spec_from_file_location("basket_signal_grid", GRID_PATH)
grid = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(grid)

ARCHIVE_MAPPING = {"PAXG": 0, "NRGC": 1, "LABR": 2}
INTERACTIVE = "interactive"
SIZING = "basket-sizing/current"
HORIZON = 121


def archive_entries(root):
    """Read the current manifests and select one source capture per named tape."""
    for family in (INTERACTIVE, SIZING):
        raw = (root / family / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if manifest["status"] != "complete":
            raise ValueError(f"{family} archive manifest is incomplete")
        manifest_sha = grid.digest(raw)
        entries = manifest["entries"]
        if family == INTERACTIVE:
            cases = manifest["cases"]["basket"]
            for profile in manifest["grid"]["profiles"]:
                for walk in manifest["grid"]["walk"]:
                    # Commodity walk selectors may all point to the same case.
                    if profile == "commodities" and walk != 0:
                        base = cases[f"{profile}:0:medium:medium"]
                        if cases.get(f"{profile}:{walk:g}:medium:medium", base) != base:
                            raise ValueError("commodity walk case changed")
                        continue
                    key = cases[f"{profile}:{walk:g}:medium:medium"]
                    label = f"interactive-{profile}-walk-{walk:g}"
                    yield label, family, manifest_sha, entries[key]
        else:
            for profile in manifest["profiles"]:
                entry = next(e for e in entries.values() if e["profile"] == profile and e["sizing"] == "lab")
                yield f"sizing-{profile}", family, manifest_sha, entry


def prepare_pack(root):
    tapes = []
    for label, family, manifest_sha, entry in archive_entries(root):
        source = root / family / entry["file"]
        raw = source.read_bytes()
        capture = json.loads(raw)
        if capture["cache_key"] != entry["cache_key"] or capture["profile"]["name"] != entry["profile"]:
            raise ValueError(f"archive capture disagrees with manifest: {source}")
        ticks = capture["config"]["ticks_per_day"]
        frames = capture["frames"]
        if len(frames) != (HORIZON - 1) * ticks + 1:
            raise ValueError(f"archive capture has wrong length: {source}")
        daily = []
        for day, frame in enumerate(frames[::ticks]):
            closes = frame["target_usd"]
            if frame["day"] != day or len(closes) != len(ARCHIVE_MAPPING) or any(
                    not math.isfinite(price) or price <= 0 for price in closes):
                raise ValueError(f"archive capture has invalid daily close {day}: {source}")
            daily.append(closes)
        tapes.append(dict(label=label, file=str(source.relative_to(root)), sha256=grid.digest(raw),
                          manifest_sha256=manifest_sha, steps_per_day=ticks,
                          implementation=capture["implementation"], closes=daily))
    return dict(schema_version=1, daily_closes=HORIZON, archive_tapes=tapes)


def plan(args, price_pack, runtime):
    if price_pack["schema_version"] != 1 or price_pack["daily_closes"] != HORIZON:
        raise ValueError("unsupported archive price pack")
    token_setting = f"scenario.tokens={list(ARCHIVE_MAPPING)!r}"
    exp = experiment.load(args.experiment, [token_setting, "scenario.prices=history"])
    scenario = experiment.build(exp)
    if scenario.day_step != 1 or scenario.prices.days < args.days:
        raise ValueError("experiment must support daily observations and cover the requested horizon")
    tokens = [token[0] for token in scenario.tokens]
    if set(tokens) != set(ARCHIVE_MAPPING):
        raise ValueError("archive mapping requires LABR, NRGC and PAXG")
    source_digest = grid.digest(args.experiment.read_bytes())
    selected = [source for source in price_pack["archive_tapes"]
                if args.profile is None or source["label"] in args.profile]
    if args.profile and set(args.profile) != {source["label"] for source in selected}:
        raise ValueError("selected profile is absent from archive price pack")
    if not selected:
        raise ValueError("selected archive price pack contains no tapes")
    by_price = defaultdict(list)
    prices_by_digest = {}
    for source in selected:
        daily = source["closes"]
        if len(daily) != HORIZON:
            raise ValueError(f"partial tape for {source['label']}")
        prices = []
        for index, token in enumerate(tokens):
            j = ARCHIVE_MAPPING[token]
            opening = scenario.prices.day0(index)
            prices.append([round(opening * (close[j] / daily[0][j])) for close in daily[:args.days]])
        signature = grid.digest(grid.json_bytes([tokens, prices]))
        prices_by_digest[signature] = prices
        by_price[signature].append({key: value for key, value in source.items() if key != "closes"})
    cells = []
    tapes = []
    for signature, sources in by_price.items():
        label = sources[0]["label"]
        prices = prices_by_digest[signature]
        price_digest, files = grid.price_files(args.output / "prices", tokens, prices)
        tapes.append(dict(regime=label, price_digest=price_digest, archive_sources=sources))
        for seed in dict.fromkeys(args.seed or (grid.SEEDS[0],)):
            group = f"{label}--seed-{seed}"
            for arm, upkeep in grid.CONTROLS.items():
                ident = f"{group}--{arm}"
                settings = [token_setting, "scenario.prices=history", f"scenario.seed={seed}",
                            f"machine.upkeep={upkeep}"]
                fingerprint = grid.digest(grid.json_bytes([ident, args.days, settings, price_digest,
                                                           source_digest, runtime, grid.CODE_SIZE_LIMIT]))
                path = f"cells/{ident}-{fingerprint[:16]}"
                cells.append(dict(id=ident, group=group, regime=label, noise_sigma=None, seed=seed, arm=arm,
                                  onset="standard", speed=None, response="production" if arm == "production" else "off",
                                  status="planned", output=path + ".json", log=path + ".log",
                                  fingerprint=fingerprint, price_digest=price_digest, settings=settings,
                                  price_files=[str(file.relative_to(args.output)) for file in files],
                                  expected_prices=[list(row) for row in zip(*prices)]))
    return dict(schema_version=1, backend="pyrevm", code_size_limit=grid.CODE_SIZE_LIMIT,
                preset="archive", days=args.days, ticks_per_day=scenario.ticks_per_day,
                tokens=tokens, token_weights_bp={token[0]: token[3] for token in scenario.tokens},
                archive_mapping=ARCHIVE_MAPPING, archive_tapes=tapes,
                archive_sampling="External feed at archive integer days 0 through 120, normalized to day 0",
                archive_pack_sha256=grid.digest(grid.json_bytes(price_pack)),
                experiment_sha256=source_digest, runtime=runtime, cells=cells)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive-root", type=Path, help="Repository's archived doc/control-lab directory")
    source.add_argument("--price-pack", type=Path, help="Previously prepared portable price pack")
    parser.add_argument("--prepare-pack", type=Path, help="Write sampled input data and exit")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--experiment", type=Path,
                        default=grid.ROOT / "alberta_buck/sim/experiments/economy.toml")
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--profile", action="append", help="Select a pack tape by label, repeatable")
    parser.add_argument("--days", type=int, default=HORIZON, help="Number of daily closes, including day zero")
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=7200, help="Seconds per cell")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    if not 3 <= args.days <= HORIZON or args.jobs < 1 or args.timeout < 1:
        parser.error(f"use 3–{HORIZON} closes, at least one job and a positive timeout")
    if args.prepare_pack and args.price_pack:
        parser.error("--prepare-pack requires --archive-root")
    if not args.prepare_pack and not args.output:
        parser.error("provide --output or --prepare-pack")
    try:
        pack = prepare_pack(args.archive_root) if args.archive_root else json.loads(args.price_pack.read_bytes())
        if args.prepare_pack:
            args.prepare_pack.parent.mkdir(parents=True, exist_ok=True)
            grid.replace_json(args.prepare_pack, pack)
            print(f"Saved {len(pack['archive_tapes'])} archive price selections to {args.prepare_pack}")
            return 0
        args.output = args.output.resolve()
        args.experiment = args.experiment.resolve()
        args.output.mkdir(parents=True, exist_ok=True)
        with (args.output / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for folder in ("cells", "prices"):
                (args.output / folder).mkdir(exist_ok=True)
            runtime = grid.runtime_inputs()
            runtime["archive_driver"] = grid.digest(Path(__file__).read_bytes())
            return grid.execute(args, plan(args, pack, runtime))
    except KeyboardInterrupt:
        return 130
    except BlockingIOError:
        parser.error(f"another campaign is using {args.output}")
    except (OSError, ValueError, KeyError, StopIteration) as error:
        parser.error(str(error))


if __name__ == "__main__":
    sys.exit(main())
