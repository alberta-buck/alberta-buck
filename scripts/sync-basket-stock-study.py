"""Mirror one datasim campaign and its recorded arms to the local simulation dashboard."""
import argparse
import fcntl
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


def transfer(remote, destination, names):
    if names:
        subprocess.run(["rsync", "-rtz", "--files-from=-", "--relative", "-e",
                        "ssh -o BatchMode=yes -o ConnectTimeout=10", remote.rstrip("/") + "/",
                        str(destination) + "/"], input="\n".join(names) + "\n", text=True, check=True)


def sync(args):
    folder = args.directory
    folder.mkdir(parents=True, exist_ok=True)
    args.runs.mkdir(parents=True, exist_ok=True)
    remote = f"{args.remote}:{args.remote_root}"
    source = f"{remote}/build/sim/{args.campaign}"
    # Fetch a specific manifest first. Its completed captures are immutable and
    # its cell IDs identify which dashboard records belong to this study.
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        temp = Path(tmp)
        transfer(source, temp, ["manifest.json"])
        manifest = json.loads((temp / "manifest.json").read_text())
        if (folder / "manifest.json").exists() and not (folder / ".stock-mirror").exists():
            raise ValueError("destination contains a local campaign; choose a mirror directory")
        cells = manifest["cells"]
        safe = re.compile(r"^[A-Za-z0-9._-]+$")
        prefixes = []
        files = []
        for cell in cells:
            ident = cell["id"]
            if not safe.fullmatch(ident) or not ident.startswith("stock-"):
                raise ValueError(f"unexpected cell ID: {ident}")
            prefixes.append(ident)
            if cell["status"] == "complete":
                files.append(f"cells/{ident}.json")
        transfer(source, folder, files)
        # Older supervisors publish the pair's IDs only when both arms finish.
        # List the standard records (not their payloads) to discover live arms.
        listing = subprocess.check_output(["rsync", "-r", "--list-only", "--include=*/",
                    "--include=manifest.json", "--exclude=*", remote + "/build/runs/"], text=True)
        available = []
        for line in listing.splitlines():
            name = line.split()[-1] if line.split() else ""
            if name.endswith("/manifest.json"):
                ident = name.split("/")[0]
                if any(ident == p + "-" + arm or re.fullmatch(re.escape(p) + r"-try\d+-" + arm, ident)
                       for p in prefixes for arm in ("active", "maintenance")):
                    available.append(ident)
        # Filter under each approved run directory; optional streams may not exist.
        if available:
            patterns = [f"--include=/{name}/" for name in available]
            subprocess.run(["rsync", "-rtz", *patterns, "--exclude=/*/", "--include=manifest.json",
                            "--include=opening.json", "--include=frames.jsonl", "--include=agents.jsonl", "--include=events.jsonl",
                            "--include=interventions.jsonl", "--exclude=*", remote + "/build/runs/",
                            str(args.runs) + "/"], check=True)
        manifest["run_root"] = str(args.runs.resolve())
        (temp / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (folder / ".stock-mirror").write_text(source + "\n")
        (temp / "manifest.json").replace(folder / "manifest.json")
    print(f"{len(files)}/{len(cells)} complete pairs, {len(available)} arm records mirrored", flush=True)
    return all(c["status"] in ("failed", "complete") for c in cells)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--runs", type=Path, default=Path("build/runs"))
    p.add_argument("--remote", default="datasim.local")
    p.add_argument("--remote-root", default="src/alberta-buck-stock-flow-5bb86e77")
    p.add_argument("--campaign", default="stock-grid-36d")
    p.add_argument("--watch", type=int, metavar="SECONDS")
    a = p.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.@_-]*", a.remote):
        p.error("invalid SSH host")
    if (not re.fullmatch(r"[A-Za-z0-9_/.-]+", a.remote_root) or ".." in a.remote_root.split("/")
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", a.campaign)):
        p.error("invalid campaign or remote study path")
    if a.watch is not None and a.watch < 1:
        p.error("--watch must be positive")
    a.directory.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            with (a.directory / ".sync.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                done = sync(a)
                subprocess.run([sys.executable, str(Path(__file__).with_name("summarize-basket-stock-grid.py")),
                                str(a.directory), "--output", str(a.directory / "summary.md"),
                                "--runs", str(a.runs)], check=True)
            if done or not a.watch:
                break
        except (OSError, subprocess.SubprocessError) as error:
            if not a.watch:
                raise
            print(f"mirror retry after {error}", flush=True)
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
