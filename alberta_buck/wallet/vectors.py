"""Canonical JSON test-vector emission.

Produces a single JSON file containing all the data the Solidity tests need to
exercise the IdentityRegistry verifier paths against the Python reference:

* PS keypair (issuer)
* Identity records (Alice, Bob): canonical_data, m, ElGamal keypair, ciphertext
* PS signatures (raw and rerandomized)
* Registration NIZK proofs (with negative variants the Solidity tests should reject)
* Chaum-Pedersen re-encryption proof (Alice -> Bob)

All scalars are 0x-prefixed 64-hex-char uint256s.  G1 points are
``{"x": "0x...", "y": "0x..."}``; G2 points use ``{"x": [c0, c1], "y": [c0, c1]}``
to match BN254.sol's struct layout.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Dict

from alberta_buck.wallet.bn254 import (
    G1, ORDER, mul, point_to_words, scalar_to_hex, rand_scalar,
)
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.ps import ps_keygen, ps_sign, ps_rerandomize
from alberta_buck.wallet.elgamal import identity_keygen, elgamal_encrypt
from alberta_buck.wallet.nizk import registration_prove, RegistrationProof
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove


def _g1(P) -> Dict[str, str]:
    x, y = point_to_words(P)
    return {"x": scalar_to_hex(x), "y": scalar_to_hex(y)}


def _g2(P) -> Dict[str, Any]:
    x_coeffs = P[0].coeffs
    y_coeffs = P[1].coeffs
    return {
        "x": [scalar_to_hex(int(x_coeffs[0])), scalar_to_hex(int(x_coeffs[1]))],
        "y": [scalar_to_hex(int(y_coeffs[0])), scalar_to_hex(int(y_coeffs[1]))],
    }


def _seeded_rng(seed: int):
    rnd = random.Random(seed)
    return lambda: rnd.getrandbits(256)


ALICE_FIELDS = {
    "given_name":    "Alice",
    "family_name":   "Johnson",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Alberta Identity Card",
    "id_number":     "AIC-2026-4839201",
    "date_of_birth": "1992-03-15",
    "issuer_id":     "atb-financial-ca",
    "issued_at":     "2026-01-20T14:30:00Z",
    "epoch":         42,
}

BOB_FIELDS = {
    "given_name":    "Bob",
    "family_name":   "Smith",
    "jurisdiction":  "Alberta, Canada",
    "id_type":       "Corporate Registration",
    "id_number":     "AB-CORP-2026-00182",
    "date_of_birth": "1985-07-22",
    "issuer_id":     "atb-financial-ca",
    "issued_at":     "2026-02-01T09:00:00Z",
    "epoch":         42,
}

ALICE_ADDR = 0xa11ce0000000000000000000000000000000a11ce
BOB_ADDR   = 0xb0b0000000000000000000000000000000000b0b
CHAINID    = 1


@dataclass
class _Party:
    fields: Dict[str, Any]
    addr: int
    canonical: str
    m: int
    M: Any
    sigma: Any
    sigma_p: Any
    kp: Any
    r: int
    E: Any
    proof: RegistrationProof


def _build_party(rng, issuer, fields, addr) -> _Party:
    canonical = canonical_identity_data(fields)
    m = identity_scalar(canonical)
    sigma = ps_sign(issuer, m, rng=rng)
    sigma_p, _ = ps_rerandomize(sigma, rng=rng)
    kp = identity_keygen(rng=rng)
    r = rand_scalar(rng)
    M = mul(G1, m)
    E = elgamal_encrypt(M, kp.pk, r)
    proof = registration_prove(sigma_p, m, r, kp.pk, E, addr, rng=rng)
    return _Party(fields, addr, canonical, m, M, sigma, sigma_p, kp, r, E, proof)


def _party_to_json(p: _Party) -> Dict[str, Any]:
    return {
        "fields": p.fields,
        "canonical_identity_data": p.canonical,
        "m": scalar_to_hex(p.m),
        "M": _g1(p.M),
        "ps_sig_raw":    {"sigma_1": _g1(p.sigma.sigma_1),   "sigma_2": _g1(p.sigma.sigma_2)},
        "ps_sig_rerand": {"sigma_1": _g1(p.sigma_p.sigma_1), "sigma_2": _g1(p.sigma_p.sigma_2)},
        "elgamal_kp":    {"sk": scalar_to_hex(p.kp.sk), "pk": _g1(p.kp.pk)},
        "r":             scalar_to_hex(p.r),
        "ciphertext":    {"R": _g1(p.E.R), "C": _g1(p.E.C)},
        "registrant":    scalar_to_hex(p.addr),
        "registration_proof": {
            "e":    scalar_to_hex(p.proof.e),
            "s_m":  scalar_to_hex(p.proof.s_m),
            "s_r":  scalar_to_hex(p.proof.s_r),
            "A_ps": _g1(p.proof.A_ps),
            "T_C":  _g1(p.proof.T_C),
            "T_R":  _g1(p.proof.T_R),
        },
    }


def build_vectors(seed: int = 0xa1bc_b0ca) -> Dict[str, Any]:
    """Deterministic vector set keyed by `seed`."""
    rng = _seeded_rng(seed)

    issuer = ps_keygen(rng=rng)
    alice  = _build_party(rng, issuer, ALICE_FIELDS, ALICE_ADDR)
    bob    = _build_party(rng, issuer, BOB_FIELDS,   BOB_ADDR)

    # Approve flow: Alice re-encrypts her M for Bob.
    r_prime = rand_scalar(rng)
    E_for_bob = elgamal_encrypt(alice.M, bob.kp.pk, r_prime)
    cp = chaum_pedersen_prove(
        alice.E, E_for_bob, alice.kp.pk, bob.kp.pk,
        alice.kp.sk, r_prime,
        ALICE_ADDR, BOB_ADDR, CHAINID,
        rng=rng,
    )

    return {
        "$schema_version": 1,
        "seed":    f"0x{seed:064x}",
        "ORDER":   f"0x{ORDER:064x}",
        "chainid": scalar_to_hex(CHAINID),
        "issuer": {
            "sk_x": scalar_to_hex(issuer.sk_x),
            "sk_y": scalar_to_hex(issuer.sk_y),
            "pk_X": _g2(issuer.pk_X),
            "pk_Y": _g2(issuer.pk_Y),
        },
        "alice": _party_to_json(alice),
        "bob":   _party_to_json(bob),
        "approve": {
            "sender":   scalar_to_hex(ALICE_ADDR),
            "spender":  scalar_to_hex(BOB_ADDR),
            "chainid":  scalar_to_hex(CHAINID),
            "E_alice":   {"R": _g1(alice.E.R),  "C": _g1(alice.E.C)},
            "E_for_bob": {"R": _g1(E_for_bob.R), "C": _g1(E_for_bob.C)},
            "r_prime":  scalar_to_hex(r_prime),
            "cp_proof": {
                "e":  scalar_to_hex(cp.e),
                "s1": scalar_to_hex(cp.s1),
                "s2": scalar_to_hex(cp.s2),
                "T1": _g1(cp.T1),
                "T2": _g1(cp.T2),
                "T3": _g1(cp.T3),
            },
        },
    }


def emit_vectors(path: str, seed: int = 0xa1bc_b0ca) -> Dict[str, Any]:
    """Build vectors and write to `path` as pretty-printed JSON."""
    data = build_vectors(seed=seed)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return data
