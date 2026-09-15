#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate the direct target-bound credential in bind_contract.json."""

from __future__ import annotations

import json
from pathlib import Path
import random

from alberta_buck.wallet.bn254 import point_to_words, words_to_point
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.nizk import bind_contract_prove
from alberta_buck.wallet.ps import PSSignature


ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ROOT / "test/vectors/identity.json"
BINDING = ROOT / "test/vectors/bind_contract.json"


def _integer(value) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


def _point(value):
    return words_to_point(_integer(value["x"]), _integer(value["y"]))


def _point_json(value) -> dict[str, str]:
    x, y = point_to_words(value)
    return {"x": f"0x{x:064x}", "y": f"0x{y:064x}"}


def _scalar(value: int) -> str:
    return f"0x{value:064x}"


def _proof_json(proof) -> dict:
    return {
        "e": _scalar(proof.e),
        "s_m": _scalar(proof.s_m),
        "s_r": _scalar(proof.s_r),
        "s_sk": _scalar(proof.s_sk),
        "A_ps": _point_json(proof.A_ps),
        "T_C": _point_json(proof.T_C),
        "T_R": _point_json(proof.T_R),
        "T_key": _point_json(proof.T_key),
    }


def main() -> None:
    identity = json.loads(IDENTITY.read_text())
    binding = json.loads(BINDING.read_text())
    row = identity["alice"]
    sigma = PSSignature(
        _point(row["ps_sig_rerand"]["sigma_1"]),
        _point(row["ps_sig_rerand"]["sigma_2"]),
    )
    pk = _point(row["elgamal_kp"]["pk"])
    ciphertext = ElGamalCiphertext(
        _point(row["ciphertext"]["R"]), _point(row["ciphertext"]["C"])
    )
    pool = binding["pool"]
    pool_registry = _integer(identity["registry"])
    target = _integer(pool["target"])
    pool_seeded = random.Random(0xB10D)
    pool_proof = bind_contract_prove(
        sigma, _integer(row["m"]), _integer(row["r"]), pk, ciphertext,
        target, _integer(row["elgamal_kp"]["sk"]),
        chainid=1, rng=lambda: pool_seeded.getrandbits(256),
        registry=pool_registry,
    )
    pool["registry"] = f"0x{pool_registry:040x}"
    pool["pk"] = _point_json(pk)
    pool["ciphertext"] = {
        "R": _point_json(ciphertext.R), "C": _point_json(ciphertext.C)
    }
    pool["ps_sig_rerand"] = {
        "sigma_1": _point_json(sigma.sigma_1),
        "sigma_2": _point_json(sigma.sigma_2),
    }
    pool["registration_proof"] = _proof_json(pool_proof)
    BINDING.write_text(json.dumps({"pool": pool}, indent=2) + "\n")
    print(f"updated {BINDING.relative_to(ROOT)} for 0x{target:040x}")


if __name__ == "__main__":
    main()
