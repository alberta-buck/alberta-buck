"""The wallet/registry kernel-backend gate: flipping the envelope,
receipt builders, tier-1 verifier and certificate family onto the
compiled buck-wallet / buck-registry kernels changes NOTHING they emit.

Three gates, mirroring test_kernel_backend.py:

1. The committed wallet kernel vectors
   (core/vectors/wallet-kernel-vectors.json) are CURRENT with the Python
   reference -- the cross-language suites' ground truth regenerates with
   zero diff.
2. Same for the registry kernel vectors.
3. Spot parity: the same seeded draws through both backends produce
   byte-identical receipts (canonical bytes, id, envelope), identical
   verify results, and identical certificate wire bytes.

Gates 1-2 run backend-free (the emitters force the py path themselves);
gate 3 skips when the binding is not built (make nix-core-build-py).
"""

from __future__ import annotations

import json
import os
import random
from contextlib import contextmanager
from pathlib import Path

import pytest

from alberta_buck.wallet import kernel_active

_REPO = Path(__file__).resolve().parents[2]

kernel_built = kernel_active()
needs_kernel = pytest.mark.skipif(
    not kernel_built, reason="kernel binding not built (make nix-core-build-py)"
)


def _rng(seed: int):
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


@contextmanager
def _backend(mode: str):
    prev = os.environ.get("BUCK_IDENTITY_BACKEND")
    os.environ["BUCK_IDENTITY_BACKEND"] = mode
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("BUCK_IDENTITY_BACKEND", None)
        else:
            os.environ["BUCK_IDENTITY_BACKEND"] = prev


def test_wallet_vectors_current():
    from alberta_buck.wallet.wallet_kernel_vectors import build_wallet_vectors

    committed = json.loads(
        (_REPO / "core/vectors/wallet-kernel-vectors.json").read_text())
    assert build_wallet_vectors() == committed, (
        "wallet-kernel-vectors.json is stale; regenerate with "
        "make nix-venv-core-wallet-vectors AND rerun the cargo/pytest/node suites"
    )


def test_registry_vectors_current():
    from alberta_buck.registry.kernel_vectors import build_registry_vectors

    committed = json.loads(
        (_REPO / "core/vectors/registry-kernel-vectors.json").read_text())
    assert build_registry_vectors() == committed, (
        "registry-kernel-vectors.json is stale; regenerate with "
        "make nix-venv-core-registry-vectors AND rerun the cargo/pytest/node suites"
    )


def _sample_receipt(rng):
    """A seeded eoa-pub receipt through whatever backend is active."""
    from alberta_buck.wallet.bn254 import G1, mul, rand_scalar
    from alberta_buck.wallet.elgamal import elgamal_encrypt
    from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
    from alberta_buck.wallet.build_receipt import build_eoa_pub
    from alberta_buck.wallet.envelope import serialize_core, envelope_text, receipt_id
    from alberta_buck.wallet.verify_receipt import verify_receipt

    def party(fields):
        canonical = canonical_identity_data(fields)
        m = identity_scalar(canonical)
        M = mul(G1, m)
        sk = rand_scalar(rng)
        pk = mul(G1, sk)
        E = elgamal_encrypt(M, pk, rand_scalar(rng))
        return canonical, m, M, sk, pk, E

    p_can, _, p_M, _, p_pk, _ = party({"given_name": "Payer", "issuer_id": "svc"})
    q_can, _, q_M, q_sk, q_pk, q_E = party({"given_name": "Payee", "issuer_id": "svc"})
    core = build_eoa_pub(
        chainid=1, contracts={"registry": "0x" + "1d" * 20},
        payer_addr=0xAA1, payer_identity=p_can, payer_M=p_M, payer_pk=p_pk,
        payee_addr=0xBB2, payee_identity=q_can, payee_M=q_M, payee_pk=q_pk,
        payee_sk=q_sk, payee_E_addr=q_E,
        value=123_456789, block_time=1_780_000_000,
        txhash="0x" + "ab" * 32, block=42, logindex=0, rng=rng)
    blob = serialize_core(core)
    res = verify_receipt(core)
    return blob, receipt_id(blob), envelope_text(blob), (res.ok, res.reason, res.value)


@needs_kernel
def test_receipt_build_parity():
    with _backend("kernel"):
        a = _sample_receipt(_rng(0xC0FFEE))
    with _backend("py"):
        b = _sample_receipt(_rng(0xC0FFEE))
    assert a == b


@needs_kernel
def test_certificate_parity():
    from alberta_buck.registry.certificate import (
        registry_sign_certificate, seal_certificate, unseal_certificate,
        registry_verify_certificate,
    )
    from alberta_buck.wallet.bn254 import G1, mul, rand_scalar
    from alberta_buck.wallet.identity import canonical_identity_data

    canonical = canonical_identity_data(
        {"given_name": "Gate", "surname": "Parity", "issuer_id": "svc"})

    def run(mode):
        with _backend(mode):
            rng = _rng(0xCE47)
            sk = rand_scalar(rng)
            client_sk = rand_scalar(rng)
            signed = registry_sign_certificate(
                registry_sk=sk, registry_id="ca-ab-2026",
                canonical_identity=canonical, serial=3,
                issued_at=1_770_000_000, expires_at=0, chainid=1, rng=rng)
            sealed = seal_certificate(signed, mul(G1, client_sk), rng=rng)
            unsealed = unseal_certificate(sealed, client_sk)
            ok = registry_verify_certificate(signed, 1)
            return signed.serialize(), sealed.envelope, unsealed.serialize(), ok

    assert run("kernel") == run("py")
