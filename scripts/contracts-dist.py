#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Emit (and verify) the published alberta-buck-contracts bundle.

The contracts are the mathematical specification of the BUCK system, and
nothing deploys them at a fixed address: every world -- the Python sim, the
JS sim, the browser demos, the tests -- deploys fresh and learns addresses
from the receipts.  So what ships is (abi, bytecode) pairs, not a
deployment registry.

Reproducibility is the whole point of this script, and it is enforced
rather than hoped for:

  * the compiler is pinned by [profile.default] in foundry.toml, and
    --check asserts every artifact records that exact version;
  * only contracts WE own are emitted (see CONTRACTS below) -- the Uniswap
    implementations are BUSL-1.1/GPL-2.0/GPL-3.0 and consumers take them
    from Uniswap's own published packages, so we never become the
    redistributor of four licences we do not control;
  * compiler.json records the settings and the git commit, so a published
    package can be traced back to a build.

Usage:
    python3 scripts/contracts-dist.py --emit    # write dist/contracts/
    python3 scripts/contracts-dist.py --check   # verify without writing
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "out"                          # [profile.default] out
DEST = REPO / "dist" / "contracts"

# The pin lives in foundry.toml; this is the value --check asserts against
# every emitted artifact.  Read from the config rather than duplicated, so
# the two cannot drift apart.
def pinned_solc() -> str:
    txt = (REPO / "foundry.toml").read_text()
    in_default = False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_default = s == "[profile.default]"
        elif in_default and s.startswith("solc"):
            return s.split("=", 1)[1].strip().strip('"')
    raise SystemExit("foundry.toml: [profile.default] has no solc pin")


# Contracts we own and publish.  Deliberately an allowlist: a wildcard over
# out/ would sweep in interface stubs, test fixtures and the vendored
# Uniswap implementations, which share the directory.
CONTRACTS = [
    "Buck",                    # ERC-20, demurrage + credit limits
    "BuckCredit",              # ERC-721, insured asset with depreciation
    "BuckKControllerDirect",   # the on-chain PID controller
    "IdentityRegistry",        # identity accumulator + verifier wiring
    "BuckBasketProRata",       # basket: pro-rata redemption
    "BuckBasketUniswapV3",     # basket: V3-routed
    "SimLP",                   # simulation liquidity helper
    "MockERC20",               # test/sim token
]

# Contracts a BUCK world also needs, which we deliberately do NOT ship.
# Recorded in the bundle so consumers know what to install alongside.
EXTERNAL = {
    "UniswapV3Factory": "@uniswap/v3-core",
    "UniswapV3Pool": "@uniswap/v3-core",
    "WETH9": "@uniswap/v2-periphery",
    "UniversalRouter": "@uniswap/universal-router",
}


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True)
        dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                               capture_output=True, text=True, check=True)
        return out.stdout.strip() + ("-dirty" if dirty.stdout.strip() else "")
    except Exception:
        return "unknown"


def load(name: str) -> dict:
    f = BUILD / f"{name}.sol" / f"{name}.json"
    if not f.exists():
        raise SystemExit(
            f"missing {f.relative_to(REPO)} -- run: make contracts-dist-build")
    return json.loads(f.read_text())


def collect(expect_solc: str):
    """Read every published artifact, asserting the compiler as we go."""
    contracts, settings, problems = {}, None, []
    for name in CONTRACTS:
        art = load(name)
        md = art.get("metadata") or {}
        ver = md.get("compiler", {}).get("version", "?")
        if not ver.startswith(expect_solc):
            problems.append(f"{name}: compiled with {ver}, expected {expect_solc}")
        deployed = art.get("deployedBytecode", {}).get("object", "")
        contracts[name] = {
            "abi": art["abi"],
            "bytecode": art["bytecode"]["object"],
            "deployedBytecode": deployed,
        }
        if settings is None and md:
            s = md.get("settings", {})
            settings = {
                "solc": ver,
                "viaIR": s.get("viaIR", False),
                "optimizer": s.get("optimizer", {}),
                "evmVersion": s.get("evmVersion"),
            }
    return contracts, settings, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--emit", action="store_true", help="write dist/contracts/")
    ap.add_argument("--check", action="store_true",
                    help="verify the build without writing")
    args = ap.parse_args()
    if not (args.emit or args.check):
        ap.error("one of --emit or --check is required")

    expect = pinned_solc()
    contracts, settings, problems = collect(expect)

    # Nothing third-party may carry bytecode into the bundle.
    for name in EXTERNAL:
        if name in contracts:
            problems.append(f"{name} is third-party and must not be published")

    if problems:
        print("contracts-dist: FAILED", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    bundle = {"contracts": contracts}
    payload = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()

    meta = {
        "solc": settings["solc"],
        "viaIR": settings["viaIR"],
        "optimizer": settings["optimizer"],
        "evmVersion": settings["evmVersion"],
        "commit": git_commit(),
        "sha256": digest,
        "external": EXTERNAL,
    }

    print(f"contracts-dist: {len(contracts)} contracts, solc {settings['solc']}, "
          f"viaIR={settings['viaIR']}, runs={settings['optimizer'].get('runs')}")
    print(f"  sha256 {digest}")
    for n, c in contracts.items():
        print(f"    {n:24s} {len(c['bytecode'])//2:>7} bytes")

    if args.emit:
        DEST.mkdir(parents=True, exist_ok=True)
        (DEST / "contracts.json").write_text(json.dumps(bundle, indent=1) + "\n")
        (DEST / "compiler.json").write_text(json.dumps(meta, indent=2) + "\n")
        # The slot exists so that adding a real deployment later is not a
        # breaking change to the package's shape.
        dep = DEST / "deployments.json"
        if not dep.exists():
            dep.write_text("{}\n")
        print(f"  wrote {DEST.relative_to(REPO)}/")

    return 0


if __name__ == "__main__":
    sys.exit(main())
