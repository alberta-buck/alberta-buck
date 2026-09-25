"""Emit the insurer-gate parity vectors: test/vectors/insurer_gate.json.

The Python reference (alberta_buck/registry/regulator.py and
alberta_buck/wallet/verifiable_decrypt.py) builds an insurance regulator's
predicate subtrees, enrolls them under an aggregator beside an identity
registry, proves an insurer's identity opening, and decides a set of issuance
cases with check_issuance.  test/InsurerGateVectors.t.sol replays all of it
against IdentityRegistry and BuckCredit, so the subtree names, the tagged leaf,
the composed paths, the opening transcript, the band ladder and every refusal
reason are pinned to the reference byte for byte.

Run:  nix develop --command python scripts/gen_insurer_gate_vectors.py
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alberta_buck.registry.merkle_service import CentralMerkleService
from alberta_buck.registry.regulator import (
    DepreciationType, InsuranceRegulator, InsurerEnvelope, IssuanceRefused,
    check_issuance, scope_id, subtree_key, GENERAL_SCOPE,
)
from alberta_buck.registry.tree import IdentityMerkleTree, KYC_SUBTREE_DEPTH, identity_leaf
from alberta_buck.wallet.bn254 import G1, mul, point_to_words, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.verifiable_decrypt import (
    identity_opening_prove, identity_opening_verify,
)

REGISTRY   = 0x00000000000000000000000000000000000ACC5E
INSURER    = 0x00000000000000000000000000000000000001E5
CHAINID    = 1
T0         = 1_800_000_000
PERIOD     = 90 * 24 * 3600
JURISDICTION = "ca-ab"


def _pt(P):
    x, y = point_to_words(P)
    return {"x": str(x), "y": str(y)}


def build():
    rs = random.Random(0x1A5E)
    rng = lambda: rs.getrandbits(256)
    m = rand_scalar(rng)
    sk = rand_scalar(rng)
    r_E = rand_scalar(rng)
    M = mul(G1, m)
    pk = mul(G1, sk)
    E = elgamal_encrypt(M, pk, r_E)

    reg = InsuranceRegulator(JURISDICTION)
    env = InsurerEnvelope(standing=True, face_band=5,
                          dep_types=frozenset({DepreciationType.NONE, DepreciationType.LINEAR}),
                          max_dep_rate=1000, max_premium_rate=500, expires_at=T0 + PERIOD)
    env = reg.attest(M, env, ["asset:bicycle"], general=True)
    # A second insurer in every subtree, so no path is a one-leaf path.
    other = InsurerEnvelope(standing=True, face_band=5, dep_types=frozenset(env.dep_types),
                            max_dep_rate=1000, max_premium_rate=500, expires_at=T0 + PERIOD)
    reg.attest(mul(G1, rand_scalar(rng)), other, ["asset:bicycle"], general=True)

    # The aggregator: an identity registry first, then each regulator subtree.
    svc = CentralMerkleService()
    kyc = IdentityMerkleTree(depth=KYC_SUBTREE_DEPTH, private=True)
    kyc.insert_leaf(0x5EED)
    svc.enroll_registry("registry:ca-ab:kyc", kyc.root())
    names = reg.predicate_names(env, ["asset:bicycle"])
    sub_roots = reg.sub_roots()
    for name in names:
        svc.enroll_feature(name, sub_roots[name])
    root = svc.identity_root

    subtrees, paths = [], []
    for name in names:
        sfx = name[len(reg.subtree_id("")):]
        sub = reg.membership_proof(M, sfx)
        assert sub is not None and sub.verify(), name
        full = svc.full_proof(name, sub)
        assert full.verify() and full.identity_root == root
        subtrees.append({"name": name, "key": hex(subtree_key(name)),
                         "slot": svc.get_sub_tree(name).aggregator_leaf_index,
                         "depth": reg.tree_depth})
        paths.append({"sub": [str(x) for x in sub.siblings], "subIndex": sub.leaf_index,
                      "agg": [str(x) for x in full.aggregator_proof.siblings]})

    op = identity_opening_prove(E, sk, M, INSURER, CHAINID, REGISTRY, rng=rng)
    assert identity_opening_verify(E, pk, M, op, INSURER, CHAINID, REGISTRY)

    bicycle = scope_id(reg.scope_name("asset:bicycle"))
    car = scope_id(reg.scope_name("asset:car"))
    cases = []
    for label, scope, face, dep, dep_rate, prem, now in [
        ("general, band 5, linear",    GENERAL_SCOPE, 99_999 * 10**6, 1, 1000, 500, T0),
        ("bicycle, eighty BUCK",       bicycle,       80 * 10**6,      1, 500,  300, T0),
        ("band 6",                     GENERAL_SCOPE, 100_000 * 10**6, 0, 0,    0,   T0),
        ("declining balance",          GENERAL_SCOPE, 10**6,           2, 0,    0,   T0),
        ("depreciation rate 1001",     GENERAL_SCOPE, 10**6,           1, 1001, 0,   T0),
        ("premium 501",                GENERAL_SCOPE, 10**6,           1, 0,    501, T0),
        ("an unattested scope",        car,           10**6,           0, 0,    0,   T0),
        ("after the period",           GENERAL_SCOPE, 10**6,           0, 0,    0,   T0 + PERIOD + 1),
    ]:
        try:
            check_issuance(env, scope=scope, face_units=face, dep_type=dep,
                           dep_rate=dep_rate, premium_rate=prem, now=now)
            expect = ""
        except IssuanceRefused as exc:
            expect = str(exc)
        cases.append({"label": label, "scope": hex(scope), "face": str(face), "depType": dep,
                      "depRate": dep_rate, "premium": prem, "at": now, "expect": expect})

    return {
        "registry": f"0x{REGISTRY:040x}", "insurer": f"0x{INSURER:040x}", "chainid": CHAINID,
        "t0": T0, "period": PERIOD, "namespace": reg.subtree_id("")[:-1],
        "pk": _pt(pk), "E": {"R": _pt(E.R), "C": _pt(E.C)}, "M": _pt(M),
        "leaf": str(identity_leaf(M)),
        "kyc": {"key": hex(subtree_key("registry:ca-ab:kyc")), "slot": 0,
                "depth": KYC_SUBTREE_DEPTH},
        "subtrees": subtrees, "root": str(root), "paths": paths,
        "claim": {"faceBand": env.face_band,
                  "depTypes": sum(1 << d for d in env.dep_types),
                  "maxDepRate": env.max_dep_rate, "maxPremiumRate": env.max_premium_rate,
                  "general": True, "scopes": ["asset:bicycle"]},
        "opening": {"e": str(op.e), "s": str(op.s), "T1": _pt(op.T1), "T2": _pt(op.T2)},
        "cases": cases,
    }


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "test", "vectors", "insurer_gate.json")
    with open(out, "w") as f:
        json.dump(build(), f, indent=2)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
