"""Precompute a selector grid, sharing cases whose active inputs are identical."""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import itertools
import json
import multiprocessing
from pathlib import Path
import time
import tomllib

from .cli import atomic_json, fingerprint, worker
from .model import STAGES, dependencies, digest, feed_rows, load_config, own_path, validate


def load_grid(path):
    grid = tomllib.loads(Path(path).read_text())
    if set(grid) != {"profiles", "walk", "flows"}:
        raise ValueError("campaign needs profiles, walk and flows")
    if set(grid["flows"]) != {"low", "medium", "high"}:
        raise ValueError("flows must define low, medium and high daily rates")
    for name in ("profiles", "walk"):
        if not grid[name] or len(grid[name]) != len(set(grid[name])):
            raise ValueError(f"{name} must be nonempty and distinct")
    return grid


def plan(cfg, grid, code, stages=None):
    """Expand every visible combination, then deduplicate active dependencies.

    Each case retains its selectors even when its disabled controls map to
    the same EVM run. Source code and the compiled contracts remain part of
    the content address. Renderer changes cannot invalidate this cache.
    """
    stages = list(STAGES) if stages is None else stages
    profiles = {p["name"]: p for p in cfg["profiles"]}
    if any(p not in profiles for p in grid["profiles"]):
        raise ValueError("campaign profile is not in the simulation configuration")
    axes = [
        dict(id="profile", label="Price profile", values=[dict(id=p, label=p) for p in grid["profiles"]]),
        dict(id="walk", label="Random walk", values=[dict(id=str(x), label=f"{x:g}×", value=x) for x in grid["walk"]]),
    ]
    for name, flag, unit in (("buyer", "demand", "USDC"), ("seller", "supply", "BUCK")):
        axes.append(dict(id=name, label=name.title() + " flow", requires=flag, values=[
            dict(id=k, label=f"{k.title()} · {v:,.0f} {unit}/day", value=v)
            for k, v in grid["flows"].items()]))
    runs, cases = {}, {}
    for p, walk in itertools.product(grid["profiles"], grid["walk"]):
        if walk and own_path(profiles[p]):
            continue  # a complete price path has no random-walk variant to select
        feed_cfg = copy.deepcopy(cfg)
        feed_cfg["feed"]["walk_amplification"] = walk
        feed_rows(feed_cfg, profiles[p])  # preflight all feeds before any EVM work
        for stage in stages:
            cases[stage] = cases.get(stage, {})
            for buyer, seller in itertools.product(grid["flows"], repeat=2):
                resolved = copy.deepcopy(feed_cfg)
                # Canonical inactive controls keep raw JSON unambiguous too.
                if STAGES[stage].get("demand"):
                    resolved["demand"]["rate_at_par_per_day"] = grid["flows"][buyer]
                if STAGES[stage].get("supply"):
                    resolved["supply"]["rate_at_par_per_day"] = grid["flows"][seller]
                validate(resolved)
                deps = dependencies(resolved, profiles[p], stage)
                key = digest(dict(schema=1, implementation=code, inputs=deps))
                selector = ":".join((p, str(walk), buyer, seller))
                cases[stage][selector] = key
                if key not in runs:
                    runs[key] = dict(config=resolved, profile=profiles[p], stage=stage,
                                     file=f"{p}-{stage}-{key[:16]}.json")
    return dict(schema=1, kind="control-lab-campaign", implementation=code,
                requested_config=cfg, grid=grid, axes=axes,
                stages={s: STAGES[s] for s in stages}, cases=cases,
                visible_cases=sum(len(c) for c in cases.values()), unique_runs=len(runs)), runs


def cached_entry(path, key, days, tpd):
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text())
        frames = result["frames"]
        if (result["cache_key"] != key or len(frames) != days * tpd + 1
                or frames[-1]["day"] != days):
            return None
        return dict(profile=result["profile"]["name"], stage=result["stage"], cache_key=key,
                    file=path.name, summary=result["summary"])
    except (ValueError, KeyError, IndexError):
        return None


def run_campaign(manifest, runs, out, jobs, force=False):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    previous = None
    if (out / "manifest.json").exists():
        previous = json.loads((out / "manifest.json").read_text())
    manifest.update(status="running", started_at=datetime.now(timezone.utc).isoformat(), entries={})
    pending = []
    for key, spec in runs.items():
        cfg = spec["config"]
        entry = None if force else cached_entry(out / spec["file"], key, cfg["days"], cfg["ticks_per_day"])
        if entry:
            manifest["entries"][key] = entry
        else:
            pending.append((cfg, spec["profile"], spec["stage"], key,
                            manifest["implementation"], str(out / spec["file"])))
    manifest["cached_runs"] = len(manifest["entries"])
    # Re-rendering a fully cached campaign must not replace its measured
    # execution duration with the time it took to read files from disk.
    if (not pending and previous and previous.get("status") == "complete"
            and previous.get("cases") == manifest["cases"]):
        for field in ("started_at", "finished_at", "elapsed_seconds", "execution_jobs"):
            if field in previous:
                manifest[field] = previous[field]
        manifest.update(status="complete", last_cache_check_at=datetime.now(timezone.utc).isoformat())
        atomic_json(out / "manifest.json", manifest)
        atomic_json(out / "progress.json", manifest)
        print(f"All {len(runs)} runs cached; preserved the original execution timing", flush=True)
        return
    manifest["execution_jobs"] = jobs
    atomic_json(out / "progress.json", manifest)
    print(f"{manifest['visible_cases']} selector cases → {len(runs)} distinct runs; "
          f"{len(manifest['entries'])} cached, {len(pending)} pending, {jobs} workers", flush=True)
    errors = []
    with ProcessPoolExecutor(max_workers=jobs, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = {pool.submit(worker, *args): args for args in pending}
        for future in as_completed(futures):
            args = futures[future]
            try:
                entry = future.result()
                manifest["entries"][entry["cache_key"]] = entry
                print(f"{len(manifest['entries'])}/{len(runs)} {entry['profile']}/{entry['stage']} "
                      f"{entry['cache_key'][:8]}: {entry['summary']['seconds']:.1f}s", flush=True)
            except Exception as exc:
                errors.append(dict(cache_key=args[3], error=repr(exc)))
                print(f"FAILED {args[1]['name']}/{args[2]} {args[3][:8]}: {exc!r}", flush=True)
            manifest.update(elapsed_seconds=round(time.perf_counter() - started, 2), errors=errors)
            atomic_json(out / "progress.json", manifest)
    if errors:
        manifest["status"] = "incomplete"
        atomic_json(out / "progress.json", manifest)
        raise RuntimeError(f"{len(errors)} cases failed; rerun to resume successful cached work")
    manifest.update(status="complete", finished_at=datetime.now(timezone.utc).isoformat(),
                    elapsed_seconds=round(time.perf_counter() - started, 2))
    atomic_json(out / "manifest.json", manifest)
    atomic_json(out / "progress.json", manifest)
    print(f"Complete: {len(runs)} runs in {manifest['elapsed_seconds'] / 60:.1f} minutes", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="doc/control-lab.toml")
    ap.add_argument("--grid", default="doc/control-lab-campaign.toml")
    ap.add_argument("--out", default="doc/control-lab/interactive")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--days", type=int, help="short smoke override; default comes from config")
    ap.add_argument("--stages", help="comma-separated stages; default is all")
    ap.add_argument("--plan", action="store_true", help="show size without running an EVM")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    args = ap.parse_args(argv)
    if args.jobs < 1:
        ap.error("--jobs must be positive")
    if not args.render_only:
        cfg, grid = load_config(args.config), load_grid(args.grid)
        if args.days is not None:
            cfg["days"] = args.days
        validate(cfg)
        stages = args.stages.split(",") if args.stages else list(STAGES)
        if len(stages) != len(set(stages)) or any(s not in STAGES for s in stages):
            ap.error("--stages must name distinct known stages")
        # Planning does not need built artifacts or any EVM imports.
        manifest, runs = plan(cfg, grid, {"sha256": "plan"} if args.plan else fingerprint(), stages)
        if args.plan:
            print(json.dumps(dict(days=cfg["days"], visible_cases=manifest["visible_cases"],
                unique_runs=len(runs), jobs=args.jobs,
                approximate_hours_at_40_seconds_per_run=round(len(runs) * 40 / args.jobs / 3600, 2),
                approximate_raw_megabytes=round(len(runs) * 1.5), axes=manifest["axes"]), indent=2))
            return 0
        run_campaign(manifest, runs, args.out, args.jobs, args.force)
        from .audit import audit
        atomic_json(Path(args.out) / "audit.json", audit(args.out))
    from .interactive import render
    render(Path(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
