"""Registry-kernel conformance, Python side.

Replays the certificate + registry-Schnorr sections of
core/vectors/registry-kernel-vectors.json (emitted by the Python
reference via alberta_buck.registry.kernel_vectors) through the
buck_core.buck_registry binding.  Certificates cross the ABI as their
wire bytes -- the format the Python SignedCertificate/SealedCertificate
classes pin.  The tree/aggregator scenarios are covered by the cargo
and JS suites (they are class-shaped and bound for JS; the Python
reference keeps its own vector-locked implementation).

Build the kernel binding first: make nix-core-build-py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

br = pytest.importorskip(
    "buck_core.buck_registry",
    reason="kernel binding not built (make nix-core-build-py)",
)

_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def rv() -> dict:
    return json.loads((_REPO / "core/vectors/registry-kernel-vectors.json").read_text(encoding="utf-8"))


def _i(h: str) -> int:
    return int(h, 16)


def _pt(j: dict) -> tuple:
    return (_i(j["x"]), _i(j["y"]))


def _bytes(h: str) -> bytes:
    return bytes.fromhex(h[2:] if h.startswith("0x") else h)


def test_registry_schnorr(rv):
    s = rv["registry_schnorr"]
    e, sig_s, r_pt = br.registry_schnorr_sign(
        _i(s["sk"]), _bytes(s["msg_hash"]), s["registry_id"], s["chainid"],
        _i(s["k"]))
    assert e == _i(s["proof"]["e"])
    assert sig_s == _i(s["proof"]["s"])
    assert r_pt == _pt(s["proof"]["R"])
    assert br.registry_schnorr_verify(
        _pt(s["pk"]), e, sig_s, r_pt, _bytes(s["msg_hash"]),
        s["registry_id"], s["chainid"])
    assert not br.registry_schnorr_verify(
        _pt(s["pk"]), e, sig_s, r_pt, _bytes(s["msg_hash"]),
        "other-registry", s["chainid"])


def test_certificate_wire_and_sealing(rv):
    c = rv["certificate"]
    wire = br.registry_sign_certificate(
        _i(c["registry_sk"]), c["registry_id"], c["canonical_identity"],
        c["serial"], c["issued_at"], c["expires_at"], c["chainid"], _i(c["k"]))
    assert wire == _bytes(c["signed_wire"])
    assert br.registry_verify_certificate(wire, c["chainid"])
    assert br.registry_verify_certificate(wire, 2) == c["wrong_chainid_verifies"]

    env = br.seal_certificate(wire, _pt(c["client_pk"]), _i(c["r_seal"]))
    assert env == _bytes(c["sealed_envelope"])
    assert br.unseal_certificate(env, _i(c["client_sk"])) == wire
    with pytest.raises(ValueError):
        br.unseal_certificate(env, _i(c["client_sk"]) ^ 1)
