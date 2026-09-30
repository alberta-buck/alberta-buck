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
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "out"                          # [profile.default] out
# Two destinations, one build: the npm package and the Python package data.
# Emitting both from a single collect() is what keeps them from drifting --
# the same bytes, the same compiler record, the same sha256.
NPM = REPO / "core" / "contracts"
PY_ = REPO / "core" / "contracts" / "python" / "buck_contracts"

# The pin lives in foundry.toml; this is the value --check asserts against
# every emitted artifact.  Read from the config rather than duplicated, so
# the two cannot drift apart.
def pinned_solc() -> str:
    txt = (REPO / "foundry.toml").read_text(encoding="utf-8")
    in_default = False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_default = s == "[profile.default]"
        elif in_default and s.startswith("solc"):
            return s.split("=", 1)[1].strip().strip('"')
    raise SystemExit("foundry.toml: [profile.default] has no solc pin")


# Groth16 verifiers, from a DEVELOPMENT trusted setup (see DEV_SETUP).
GROTH16 = (
    [f"MintBatchN{n}Groth16Verifier" for n in (1, 2, 4, 8, 16, 32)]
    + [f"MintBatchA2N{n}Groth16Verifier" for n in (1, 2, 4, 8, 16, 32)]
    + ["SpendGroth16Verifier", "DepositFoldA1Verifier", "DepositFoldA2Verifier",
       "IdentityMembershipB1Verifier"])

# Contracts we own and publish.  Deliberately an allowlist: a wildcard over
# out/ would sweep in interface stubs, test fixtures and the vendored
# Uniswap implementations, which share the directory.
CONTRACTS = [
    "Buck",                    # ERC-20, demurrage + credit limits
    "BuckCredit",              # ERC-721, insured asset with depreciation
    "BuckKControllerDirect",   # the on-chain PID controller
    "IdentityRegistry",        # identity accumulator + verifier wiring
    "BuckBasketEquity",        # the equity basket: a credit holder
    "BuckBasketEquityWheel",   # its components facet
    "BuckBasketUniswapV3",     # its venue facet (V3 pools)
    "EquityTurnDirector",      # its turn director
    "BasketWheel",             # the work wheel a keeper turns
    "EquityDesk",              # the monetary desk: its own credit holder
    "SimLP",                   # simulation liquidity helper
    "MockERC20",               # test/sim token
    # The Notes pool and the adapters that give it its verifiers.
    "Notes",
    "MintVerifierAdapter",     # public-issuer batches, dispatched by N
    "MintVerifierA2Adapter",   # private-issuer (A2) batches, dispatched by N
    "SpendVerifierAdapter",    # the one note proof every spend reuses
    "DepositFoldVerifierAdapter",           # A1/A2 folded deposit gates
    "IdentityMembershipB1VerifierAdapter",  # B1 depositor membership
] + GROTH16

# Until v1.0.0 every verifier above comes from scripts/snark/setup*.sh, which
# contributes FIXED, PUBLISHED entropy: the toxic waste is known, so anyone can
# forge a proof these verifiers accept.  That is deliberate -- it lets a
# simulation model forged-proof attacks and the defences around them -- and the
# bundle says so in data, not only in prose, so no consumer can miss it.
DEV_SETUP = {
    "kind": "development",
    "until": "1.0.0",
    "entropy": "fixed, published strings in scripts/snark/setup*.sh",
    "consequence": "the toxic waste is public: anyone can forge a proof these verifiers accept",
    "purpose": "simulation of forged-proof attacks and their defences; not for value",
}

# Contracts not produced by solc: circomlibjs's Poseidon hashers, whose creation
# code lives in a hex constant (src/PoseidonT*Bytecode.sol) and whose ABI is our
# interface.  IdentityRegistry needs both (setIdentityPoseidon/T4).
GENERATED = {
    "PoseidonT3": ("PoseidonT3Bytecode.sol", "IPoseidonT3"),
    "PoseidonT4": ("PoseidonT4Bytecode.sol", "IPoseidonT4"),
}

EIP170 = 24_576                # runtime code limit
EIP3860 = 2 * EIP170           # initcode limit

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
    return json.loads(f.read_text(encoding="utf-8"))


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
        if name in GROTH16:
            contracts[name]["trustedSetup"] = DEV_SETUP["kind"]
        if settings is None and md:
            s = md.get("settings", {})
            settings = {
                "solc": ver,
                "viaIR": s.get("viaIR", False),
                "optimizer": s.get("optimizer", {}),
                "evmVersion": s.get("evmVersion"),
            }
    return contracts, settings, problems


def generated(sol: str, iface: str) -> dict:
    """A circomlibjs contract: its creation code from the Solidity hex constant,
    its ABI from our interface, its runtime from the creation prefix."""
    src = (REPO / "src" / sol).read_text(encoding="utf-8")
    code = re.search(r'bytes internal constant BYTECODE = hex"([0-9a-fA-F]+)";', src).group(1).lower()
    # CODESIZE; CODECOPY(0, 12, size); RETURN(0, n): the runtime is code[12:12+n].
    pre = re.fullmatch(r"38600c60003961([0-9a-f]{4})6000f3", code[:24])
    if not pre or len(code) // 2 - 12 != int(pre.group(1), 16):
        raise SystemExit(f"{sol}: not circomlibjs's creation code")
    return {"abi": load(iface)["abi"], "bytecode": "0x" + code, "deployedBytecode": "0x" + code[24:]}


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

    for name, (sol, iface) in GENERATED.items():
        contracts[name] = generated(sol, iface)

    # Nothing third-party may carry bytecode into the bundle.
    for name in EXTERNAL:
        if name in contracts:
            problems.append(f"{name} is third-party and must not be published")

    # Everything shipped must deploy where the size limits hold.
    for name, c in contracts.items():
        runtime, init = len(c["deployedBytecode"]) // 2 - 1, len(c["bytecode"]) // 2 - 1
        if runtime > EIP170:
            problems.append(f"{name}: {runtime}-byte runtime exceeds EIP-170 ({EIP170})")
        if init > EIP3860:
            problems.append(f"{name}: {init}-byte initcode exceeds EIP-3860 ({EIP3860})")

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
        "trustedSetup": {**DEV_SETUP, "verifiers": GROTH16},
        "generated": {name: {"source": f"src/{sol}", "abi": iface,
                             "generator": "circomlibjs poseidon_gencontract"}
                      for name, (sol, iface) in GENERATED.items()},
    }

    print(f"contracts-dist: {len(contracts)} contracts, solc {settings['solc']}, "
          f"viaIR={settings['viaIR']}, runs={settings['optimizer'].get('runs')}")
    print(f"  sha256 {digest}")
    for n, c in contracts.items():
        mark = "  DEVELOPMENT trusted setup" if n in GROTH16 else ""
        print(f"    {n:36s} {len(c['deployedBytecode'])//2 - 1:>6} bytes runtime{mark}")

    if args.emit:
        contracts_txt = json.dumps(bundle, indent=1) + "\n"
        compiler_txt = json.dumps(meta, indent=2) + "\n"
        for dest in (NPM, PY_):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "contracts.json").write_text(contracts_txt)
            (dest / "compiler.json").write_text(compiler_txt)
            # The slot exists so adding a real deployment later is not a
            # breaking change to the package's shape.
            dep = dest / "deployments.json"
            if not dep.exists():
                dep.write_text("{}\n")
            print(f"  wrote {dest.relative_to(REPO)}/")

    return 0


if __name__ == "__main__":
    sys.exit(main())
