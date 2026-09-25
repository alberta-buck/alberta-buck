"""The kernel-backend gate: flipping alberta_buck.wallet onto the compiled
buck-identity kernel changes NOTHING it emits.

Three gates:

1. The canonical identity vectors (test/vectors/identity.json) reproduce
   BYTE-identically under the kernel backend -- the committed file, the
   forge tests' fixture, regenerates with zero diff.
2. The nonce-inclusive kernel vectors (core/vectors/
   identity-kernel-vectors.json) are CURRENT with the py_ecc reference
   (the emitter forces the py path itself) -- guarding the cross-language
   replay suites' ground truth.
3. Spot parity: the same seeded draws through both backends produce the
   same proofs.

Skips (except gate 2) when the binding is not built
(make nix-core-build-py).
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from alberta_buck.wallet import kernel_active

_REPO = Path(__file__).resolve().parents[2]

pytestmark = []

kernel_built = kernel_active()
needs_kernel = pytest.mark.skipif(
    not kernel_built, reason="kernel binding not built (make nix-core-build-py)"
)


def _seeded(seed: int):
    rnd = random.Random(seed)
    return lambda: rnd.getrandbits(256)


@needs_kernel
def test_identity_vectors_reproduce_bit_identically():
    """build_vectors() under the kernel backend == the committed fixture."""
    from alberta_buck.wallet.vectors import build_vectors

    assert kernel_active()
    built = build_vectors()
    committed = json.loads((_REPO / "test/vectors/identity.json").read_text())
    assert built == committed, "kernel-backend vectors diverge from the committed fixture"
    # ... and byte-identically through the same serialization.
    text = json.dumps(built, indent=2, sort_keys=True) + "\n"
    assert text == (_REPO / "test/vectors/identity.json").read_text()


def test_kernel_reference_vectors_current():
    """The committed cross-language vectors match the py_ecc reference NOW
    (the emitter forces BUCK_IDENTITY_BACKEND=py internally)."""
    from alberta_buck.wallet.kernel_vectors import build_kernel_vectors

    built = build_kernel_vectors()
    committed = json.loads(
        (_REPO / "core/vectors/identity-kernel-vectors.json").read_text()
    )
    assert built == committed, (
        "identity-kernel-vectors.json is stale; regenerate with"
        " make nix-venv-core-identity-vectors (ABI-break-level event)"
    )


@needs_kernel
def test_spot_parity_same_draws_same_proofs(monkeypatch):
    """The same seeded rng through py and kernel backends yields identical
    keypairs, signatures, proofs, and hashes."""
    from alberta_buck.wallet import (
        ps_keygen, ps_sign, ps_present,
        identity_keygen, elgamal_encrypt,
        registration_prove, registration_verify,
        chaum_pedersen_prove, poseidon,
    )
    from alberta_buck.wallet.bn254 import G1, mul, rand_scalar

    def run():
        rng = _seeded(0xC0FFEE)
        issuer = ps_keygen(rng=rng)
        m = rand_scalar(rng)
        sigma = ps_sign(issuer, m, rng=rng)
        sigma_p, _, b = ps_present(sigma, issuer.pk_Y1, rng=rng)
        kp = identity_keygen(rng=rng)
        r = rand_scalar(rng)
        E = elgamal_encrypt(mul(G1, m), kp.pk, r)
        proof = registration_prove(sigma_p, b, m, r, kp.pk, E, 0xA11CE, kp.sk, rng=rng)
        ok = registration_verify(sigma_p, E, kp.pk, issuer.pk_X, issuer.pk_Y,
                                 proof, 0xA11CE)
        E2 = elgamal_encrypt(mul(G1, m), kp.pk, rand_scalar(rng))
        cp = chaum_pedersen_prove(E, E2, kp.pk, kp.pk, kp.sk, r,
                                  1, 2, 1, rng=rng)
        h = poseidon([1, 2, 3])
        return issuer, sigma, sigma_p, kp, E, proof, ok, cp, h

    monkeypatch.setenv("BUCK_IDENTITY_BACKEND", "py")
    ref = run()
    monkeypatch.setenv("BUCK_IDENTITY_BACKEND", "kernel")
    fast = run()

    assert ref == fast
    assert ref[6] is True and fast[6] is True
