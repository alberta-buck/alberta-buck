"""Registry-kernel conformance, Python side.

Replays the certificate + registry-Schnorr sections of
core/vectors/registry-kernel-vectors.json (emitted by the Python
reference via alberta_buck.registry.kernel_vectors) through the
buck_core.buck_registry binding, and the salt and issuance-gate sections
through the free functions.  Certificates cross the ABI as their wire
bytes -- the format the Python SignedCertificate/SealedCertificate
classes pin.  The tree, aggregator, feature and regulator scenarios are
covered by the cargo and JS suites (they are class-shaped and bound for
JS; the Python reference keeps its own vector-locked implementation).

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


def test_salts(rv):
    bi = pytest.importorskip("buck_core.buck_identity")
    s = rv["salt"]
    for t, tag in s["tree_tags"].items():
        assert bi.tree_tag(t) == _i(tag), t
    for c in s["cases"]:
        assert bi.derive_salt(_i(s["secret"]), c["tree_id"], c["counter"]) == _i(c["salt"])
    with pytest.raises(ValueError):
        bi.derive_salt(0, "kyc:x")
    with pytest.raises(ValueError):
        bi.tree_tag("")


def test_issuance_gate(rv):
    r = rv["regulator"]
    e = r["envelope"]
    env = (True, e["face_band"], e["dep_types"], e["max_dep_rate"], e["max_premium_rate"],
           e["expires_at"], [_i(x) for x in r["scopes"]])
    for c in r["cases"]:
        got = br.check_issuance(env, _i(c["scope"]), int(c["face"]), c["dep_type"], c["dep_rate"],
                                c["premium_rate"], c["now"])
        assert got == c["want"], c
    for face, band in r["bands"]:
        assert br.band_for_face(int(face)) == band
    for n, k in zip(r["predicate_names"], r["subtree_keys"]):
        assert br.subtree_key(n) == _i(k)
    assert br.scope_id(f"regulator:{r['jurisdiction']}:scope:asset:bicycle") in map(_i, r["scopes"])
