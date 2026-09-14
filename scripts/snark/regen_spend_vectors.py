#!/usr/bin/env python3
"""Re-prove committed spend vectors against the current spend zkey.

Splices new Groth16 proofs into alberta_buck/test/vectors/e2e/{a1,a2,b1}.json
and writes build/snark/spend/fixtures/spend_leaf0_to_bob.json.  Mint,
membership, and note-binding artifacts are left untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from alberta_buck.review.integration import spend_prove  # noqa: E402

VEC_DIR = ROOT / "alberta_buck" / "test" / "vectors" / "e2e"
FIX_DIR = ROOT / "build" / "snark" / "spend" / "fixtures"
FLAVORS = ("a1", "a2", "b1")


def snark_to_fixture_proof(p: dict) -> dict:
    return {
        "pA": [p["pi_a"][0], p["pi_a"][1]],
        "pB": [
            [p["pi_b"][0][1], p["pi_b"][0][0]],
            [p["pi_b"][1][1], p["pi_b"][1][0]],
        ],
        "pC": [p["pi_c"][0], p["pi_c"][1]],
    }


def prove_opening(name: str, witness: dict) -> dict:
    tmp = ROOT / "build" / "snark" / "spend" / "_prove" / name
    result = spend_prove(tmp, witness)
    flavor = str(witness["flavor"])
    pub = result["publicSignals"]
    if pub[5] != flavor:
        raise SystemExit(f"{name}: publicSignals[5] flavor {pub[5]} != {flavor}")
    if pub[6] != str(witness["issuanceCommitment"]):
        raise SystemExit(
            f"{name}: publicSignals[6] issuance commitment "
            f"{pub[6]} != {witness['issuanceCommitment']}"
        )
    return result


def main() -> None:
    FIX_DIR.mkdir(parents=True, exist_ok=True)
    bob = None
    for flavor in FLAVORS:
        path = VEC_DIR / f"{flavor}.json"
        world = json.loads(path.read_text())
        witness = world["spend"]["witness"]
        issuance_commitment = (
            world["opening"]["cm"] if flavor == "b1" else "0"
        )
        witness["issuanceCommitment"] = str(issuance_commitment)
        print(f"[regen] proving e2e {flavor} spend (flavor={witness['flavor']})")
        result = prove_opening(f"e2e_{flavor}", witness)
        proof = snark_to_fixture_proof(result["proof"])
        world["spend"]["proof"] = proof
        world["spend"]["proofBytes"] = "0x" + result["proofBytes"].hex()
        world["spend"]["public"]["flavor"] = str(witness["flavor"])
        world["spend"]["public"]["issuanceCommitment"] = str(
            issuance_commitment
        )
        path.write_text(json.dumps(world, indent=2) + "\n")
        print(f"  wrote {path}")
        if flavor == "a1":
            bob = {
                "spend": {
                    "leafIndex": world["spend"]["leafIndex"],
                    "public": world["spend"]["public"],
                    "witness": witness,
                    "proof": proof,
                    "proofBytes": world["spend"]["proofBytes"],
                }
            }
    if bob is None:
        raise SystemExit("a1 e2e vector missing")
    out = FIX_DIR / "spend_leaf0_to_bob.json"
    out.write_text(json.dumps(bob, indent=2) + "\n")
    print(f"  wrote {out}")


if __name__ == "__main__":
    main()
