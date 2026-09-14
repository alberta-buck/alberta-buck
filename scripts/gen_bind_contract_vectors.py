#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regenerate the CREATE2 target-bound credential in bind_contract.json."""

from __future__ import annotations

import json
from pathlib import Path
import random

from alberta_buck.wallet.bn254 import point_to_words, words_to_point
from alberta_buck.wallet.elgamal import ElGamalCiphertext
from alberta_buck.wallet.nizk import bind_contract_prove
from alberta_buck.wallet.ps import PSSignature
from alberta_buck.wallet.transcript import keccak_raw


ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ROOT / "test/vectors/identity.json"
BINDING = ROOT / "test/vectors/bind_contract.json"
TOY_ARTIFACT = ROOT / "out/ToyContract.sol/ToyContract.json"


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
    artifact = json.loads(TOY_ARTIFACT.read_text())

    row = identity["alice"]
    create2 = binding["create2"]
    initcode = bytes.fromhex(artifact["bytecode"]["object"].removeprefix("0x"))
    deployer = bytes.fromhex(create2["deployer"].removeprefix("0x"))
    salt = _integer(create2["salt"]).to_bytes(32, "big")
    predicted = keccak_raw(b"\xff" + deployer + salt + keccak_raw(initcode))[-20:]
    target = int.from_bytes(predicted, "big")

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
    pool_seeded = random.Random(0xB10D)
    pool_proof = bind_contract_prove(
        sigma, _integer(row["m"]), _integer(row["r"]), pk, ciphertext,
        _integer(pool["target"]), _integer(row["elgamal_kp"]["sk"]),
        chainid=1, rng=lambda: pool_seeded.getrandbits(256),
        registry=pool_registry,
    )
    pool["registry"] = f"0x{pool_registry:040x}"
    pool["registration_proof"] = _proof_json(pool_proof)

    seeded = random.Random(0xC2EA7E)
    proof = bind_contract_prove(
        sigma, _integer(row["m"]), _integer(row["r"]), pk, ciphertext,
        target, _integer(row["elgamal_kp"]["sk"]), chainid=1,
        rng=lambda: seeded.getrandbits(256),
        registry=_integer(create2["registry"]),
    )

    create2["predicted"] = "0x" + predicted.hex()
    create2["pk"] = _point_json(pk)
    create2["ciphertext"] = {
        "R": _point_json(ciphertext.R), "C": _point_json(ciphertext.C)
    }
    create2["ps_sig_rerand"] = {
        "sigma_1": _point_json(sigma.sigma_1),
        "sigma_2": _point_json(sigma.sigma_2),
    }
    create2["registration_proof"] = _proof_json(proof)
    BINDING.write_text(json.dumps(binding, indent=2) + "\n")
    print(f"updated {BINDING.relative_to(ROOT)} for {create2['predicted']}")


if __name__ == "__main__":
    main()
