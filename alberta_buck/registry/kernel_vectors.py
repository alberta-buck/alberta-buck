"""Nonce-inclusive cross-language REGISTRY kernel vectors.

Emits ``core/vectors/registry-kernel-vectors.json`` from the pure-Python
reference path (``BUCK_IDENTITY_BACKEND=py``) so the Rust (``cargo``),
Python (``pytest``) and JS (``node --test``) suites can replay the
`buck-registry` crate's surface bit-identically: the incremental
identity Merkle tree (stepwise roots, paths, leaf replacement, event-log
reconstruction), the certificate family (signing hash, wire formats,
registry Schnorr, ElGamal sealing/unsealing) and the central aggregator
(enrollment, sub-root updates, full and AND-composed membership proofs)
plus the feature-authority tree.

Timestamps are fixed constants (the kernel takes them as arguments); the
rng stream is this module's own fresh seed -- the pinned streams behind
``vectors.py``/``kernel_vectors.py`` are untouched.

Regenerate: ``make nix-venv-core-registry-vectors``.
"""

from __future__ import annotations

import json
import os
import random
from typing import Any, Callable, Dict, List

from alberta_buck.wallet.bn254 import (
    G1, ORDER, mul, point_to_words, rand_scalar, scalar_to_hex,
)
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.registry.tree import IdentityMerkleTree, identity_leaf
from alberta_buck.registry.certificate import (
    registry_schnorr_sign, registry_schnorr_verify,
    registry_sign_certificate, registry_verify_certificate,
    seal_certificate, unseal_certificate,
)
from alberta_buck.registry.merkle_service import CentralMerkleService, SubTreeKind
from alberta_buck.registry.feature_authority import FeatureAuthority


def _seeded_rng(seed: int) -> Callable[[], int]:
    rnd = random.Random(seed)
    return lambda: rnd.getrandbits(256)


def _replay(vals: List[int]) -> Callable[[], int]:
    it = iter(vals)
    return lambda: next(it)


def _hx(v: int) -> str:
    return f"0x{v:064x}"


def _g1(P) -> Dict[str, str]:
    x, y = point_to_words(P)
    return {"x": _hx(x), "y": _hx(y)}


def _ct(E) -> Dict[str, Any]:
    return {"R": _g1(E.R), "C": _g1(E.C)}


def _proof_json(p) -> Dict[str, Any]:
    return {
        "leaf": _hx(p.leaf),
        "siblings": [_hx(s) for s in p.siblings],
        "index_bits": list(p.index_bits),
        "root": _hx(p.root),
        "leaf_index": p.leaf_index,
    }


ISSUED_AT = 1770000000
TS0 = 1770000100.0


def build_registry_vectors(seed: int = 0x3E6157335EED) -> Dict[str, Any]:
    prev = os.environ.get("BUCK_IDENTITY_BACKEND")
    os.environ["BUCK_IDENTITY_BACKEND"] = "py"
    try:
        return _build(seed)
    finally:
        if prev is None:
            os.environ.pop("BUCK_IDENTITY_BACKEND", None)
        else:
            os.environ["BUCK_IDENTITY_BACKEND"] = prev


def _build(seed: int) -> Dict[str, Any]:
    rng = _seeded_rng(seed)
    draw = lambda: rand_scalar(rng)

    out: Dict[str, Any] = {
        "$schema_version": 1,
        "backend": "py",
        "seed": _hx(seed),
    }

    # ---- identity Merkle tree ---------------------------------------------
    # Stepwise roots pin the incremental semantics; a replacement row pins
    # the aggregator's leaf-update path; from_leaves is replayed by
    # rebuilding from the recorded list.
    points = [mul(G1, draw()) for _ in range(5)]
    leaves = [identity_leaf(P) for P in points]
    tree = IdentityMerkleTree(depth=12)
    roots_after_insert = []
    for leaf in leaves:
        tree.insert_leaf(leaf)
        roots_after_insert.append(_hx(tree.root()))
    paths = {str(i): _proof_json(tree.path(i)) for i in (0, 2, 4)}
    replacement_leaf = identity_leaf(mul(G1, draw()))
    tree.leaves[1] = replacement_leaf
    tree._root_dirty = True
    root_after_replace = tree.root()
    path_after_replace = tree.path(1)
    out["tree"] = {
        "depth": 12,
        "points": [_g1(P) for P in points],
        "leaves": [_hx(l) for l in leaves],
        "roots_after_insert": roots_after_insert,
        "empty_root": _hx(IdentityMerkleTree(depth=12).root()),
        "paths": paths,
        "replace": {"index": 1, "leaf": _hx(replacement_leaf),
                    "root": _hx(root_after_replace),
                    "path": _proof_json(path_after_replace)},
    }

    # ---- registry Schnorr over an arbitrary 32-byte message ----------------
    sk_reg = draw()
    msg = (draw() % (1 << 256)).to_bytes(32, "big")
    k = draw()
    sig = registry_schnorr_sign(sk_reg, msg, "ca-ab-2026", chainid=1,
                                rng=_replay([k]))
    assert registry_schnorr_verify(mul(G1, sk_reg), sig, msg, "ca-ab-2026", 1)
    out["registry_schnorr"] = {
        "sk": scalar_to_hex(sk_reg), "pk": _g1(mul(G1, sk_reg)),
        "msg_hash": "0x" + msg.hex(), "registry_id": "ca-ab-2026",
        "chainid": 1, "k": scalar_to_hex(k),
        "proof": {"e": scalar_to_hex(sig.e), "s": scalar_to_hex(sig.s),
                  "R": _g1(sig.R)},
    }

    # ---- certificate: sign / wire / seal / unseal --------------------------
    fields = {"given_name": "Dana", "surname": "Prairie",
              "dob": "1985-06-15", "issuer_id": "svc-alberta"}
    canonical = canonical_identity_data(fields)
    m = identity_scalar(canonical)
    M = mul(G1, m)
    k_cert = draw()
    signed = registry_sign_certificate(
        registry_sk=sk_reg, registry_id="ca-ab-2026",
        canonical_identity=canonical, serial=7,
        issued_at=ISSUED_AT, expires_at=0, chainid=1,
        rng=_replay([k_cert]))
    assert registry_verify_certificate(signed, 1)
    client_sk = draw()
    client_pk = mul(G1, client_sk)
    r_seal = draw()
    sealed = seal_certificate(signed, client_pk, rng=_replay([r_seal]))
    unsealed = unseal_certificate(sealed, client_sk)
    assert unsealed == signed
    out["certificate"] = {
        "registry_sk": scalar_to_hex(sk_reg),
        "registry_pk": _g1(mul(G1, sk_reg)),
        "registry_id": "ca-ab-2026",
        "canonical_identity": canonical,
        "m": scalar_to_hex(m), "M": _g1(M),
        "serial": 7, "issued_at": ISSUED_AT, "expires_at": 0, "chainid": 1,
        "k": scalar_to_hex(k_cert),
        "signature": {"e": scalar_to_hex(signed.signature.e),
                      "s": scalar_to_hex(signed.signature.s),
                      "R": _g1(signed.signature.R)},
        "hash_bytes": "0x" + signed.cert.to_hash_bytes().hex(),
        "cert_wire": "0x" + signed.cert.serialize().hex(),
        "signed_wire": "0x" + signed.serialize().hex(),
        "client_sk": scalar_to_hex(client_sk), "client_pk": _g1(client_pk),
        "r_seal": scalar_to_hex(r_seal),
        "sealed_ct": _ct(sealed.ct),
        "sealed_envelope": "0x" + sealed.envelope.hex(),
        "wrong_chainid_verifies": registry_verify_certificate(signed, 2),
    }

    # ---- central aggregator scenario ---------------------------------------
    # Two KYC registries + one feature authority; enroll, insert, update,
    # full + composed proofs.  Timestamps fixed.
    reg_a = IdentityMerkleTree(depth=12)
    reg_b = IdentityMerkleTree(depth=12)
    feat = FeatureAuthority("feature:age-over-18", tree_depth=10)

    ids_a = [mul(G1, draw()) for _ in range(3)]
    ids_b = [mul(G1, draw()) for _ in range(2)]
    for P in ids_a:
        reg_a.insert_leaf(identity_leaf(P))
    for P in ids_b:
        reg_b.insert_leaf(identity_leaf(P))

    svc = CentralMerkleService(depth=10)
    svc.enroll_registry("ca-ab-2026", reg_a.root(), timestamp=TS0)
    svc.enroll_registry("ca-bc-2026", reg_b.root(), timestamp=TS0 + 1)
    svc.enroll_feature("feature:age-over-18", feat.sub_root, timestamp=TS0 + 2)
    root_after_enroll = svc.identity_root

    # The first identity of registry A gains the age feature.
    feat.attest(ids_a[0], evidence_hash=b"\x11" * 32)
    feat._records[0].attested_at = TS0 + 3          # pin the timestamp
    root_after_attest = svc.update_sub_root("feature:age-over-18",
                                            feat.sub_root, timestamp=TS0 + 3)

    # Registry A registers one more identity and pushes.
    extra = mul(G1, draw())
    reg_a.insert_leaf(identity_leaf(extra))
    root_final = svc.update_sub_root("ca-ab-2026", reg_a.root(),
                                     timestamp=TS0 + 4)

    sub_proof = reg_a.path(0)
    mx, my = point_to_words(ids_a[0])
    full = svc.full_proof("ca-ab-2026", sub_proof, mx, my)
    assert full.verify()
    feat_proof = feat.membership_proof_for_identity(ids_a[0])
    composed = svc.composed_proof(
        [("ca-ab-2026", sub_proof), ("feature:age-over-18", feat_proof)],
        mx, my)
    assert composed.verify()
    agg = svc.aggregator_proof("ca-bc-2026")
    assert agg.verify()

    out["aggregator"] = {
        "depth": 10,
        "reg_a": {"depth": 12,
                  "points": [_g1(P) for P in ids_a] + [_g1(extra)],
                  "leaves": [_hx(l) for l in reg_a.leaves]},
        "reg_b": {"depth": 12, "points": [_g1(P) for P in ids_b],
                  "leaves": [_hx(l) for l in reg_b.leaves]},
        "feature": {"id": "feature:age-over-18", "depth": 10,
                    "leaves": [_hx(l) for l in feat._tree.leaves],
                    "sub_root": _hx(feat.sub_root)},
        "enrolls": [
            {"id": "ca-ab-2026", "kind": SubTreeKind.KYC, "ts": TS0},
            {"id": "ca-bc-2026", "kind": SubTreeKind.KYC, "ts": TS0 + 1},
            {"id": "feature:age-over-18", "kind": SubTreeKind.FEATURE,
             "ts": TS0 + 2},
        ],
        "root_after_enroll": _hx(root_after_enroll),
        "root_after_attest": _hx(root_after_attest),
        "root_final": _hx(root_final),
        "identity": _g1(ids_a[0]),
        "sub_proof": _proof_json(sub_proof),
        "full_proof": {
            "aggregator": {
                "sub_root": _hx(full.aggregator_proof.sub_root),
                "siblings": [_hx(s) for s in full.aggregator_proof.siblings],
                "index_bits": list(full.aggregator_proof.index_bits),
                "aggregator_root": _hx(full.aggregator_proof.aggregator_root),
                "leaf_index": full.aggregator_proof.aggregator_leaf_index,
            },
            "verify": full.verify(),
        },
        "feature_proof": _proof_json(feat_proof),
        "composed_verify": composed.verify(),
        "agg_proof_b": {
            "sub_root": _hx(agg.sub_root),
            "siblings": [_hx(s) for s in agg.siblings],
            "index_bits": list(agg.index_bits),
            "aggregator_root": _hx(agg.aggregator_root),
            "leaf_index": agg.aggregator_leaf_index,
        },
    }

    # ---- feature authority: duplicate + revoke -----------------------------
    fa = FeatureAuthority("feature:has-license", tree_depth=10)
    pa, pb = mul(G1, draw()), mul(G1, draw())
    fa.attest(pa, evidence_hash=None)
    fa._records[0].attested_at = TS0
    fa.attest(pb, evidence_hash=b"\x22" * 32)
    fa._records[1].attested_at = TS0 + 1
    root_two = fa.sub_root
    dup_rejected = False
    try:
        fa.attest(pa)
    except ValueError:
        dup_rejected = True
    revoked_idx = fa.revoke(pa)
    out["feature_authority"] = {
        "id": "feature:has-license", "depth": 10,
        "points": [_g1(pa), _g1(pb)],
        "root_after_two": _hx(root_two),
        "dup_rejected": dup_rejected,
        "revoked_index": revoked_idx,
        "root_after_revoke": _hx(fa.sub_root),
        "leaves_after_revoke": [_hx(l) for l in fa._tree.leaves],
        "has_pa": fa.has_identity(pa),
        "has_pb": fa.has_identity(pb),
    }

    return out


def emit_registry_vectors(path: str, seed: int = 0x3E6157335EED) -> Dict[str, Any]:
    data = build_registry_vectors(seed=seed)
    with open(path, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    return data


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "core/vectors/registry-kernel-vectors.json"
    data = emit_registry_vectors(out)
    sys.stderr.write(f"wrote {out} ({len(json.dumps(data))} bytes JSON)\n")
