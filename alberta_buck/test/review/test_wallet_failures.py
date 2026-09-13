# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests PASS when the documented current failure is reproduced.

When production changes, intentionally revisit these assertions and the review;
do not weaken them to accept either outcome.
"""
from dataclasses import replace
import json

from alberta_buck.review.examples import (
    Account, seeded, harvested_registration, false_identity_approval,
    double_opening, mismatched_membership,
)
from alberta_buck.review.mitigations import (
    ApprovalContext, prove_approval, verify_approval, independent_generator,
    credential_leaf,
)
from alberta_buck.wallet.bn254 import G1, ORDER, add, mul, neg, eq
from alberta_buck.wallet.ps import ps_verify
from alberta_buck.wallet.nizk import registration_verify
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_verify
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.issuer_reenc import H_POINT, H_SCALAR
from alberta_buck.registry.tree import identity_leaf
import pytest


def test_01_published_signature_tests_identity_and_issuer_can_link(backend):
    issuer, owner, public, *_ = harvested_registration()
    assert ps_verify(issuer.pk_X, issuer.pk_Y, public, owner.m)
    assert not ps_verify(issuer.pk_X, issuer.pk_Y, public, owner.m+1)
    # Stronger issuer view needs only a G1 operation, not pairings.
    assert eq(public.sigma_2, mul(public.sigma_1,
              (issuer.sk_x+owner.m*issuer.sk_y) % ORDER))


def test_02_public_credential_and_disclosed_record_register_attacker(backend):
    issuer, owner, public, attacker, sigma, proof = harvested_registration()
    assert attacker.sk != owner.sk and not eq(public.sigma_1, sigma.sigma_1)
    assert registration_verify(sigma, attacker.E, attacker.pk, issuer.pk_X,
                               issuer.pk_Y, proof, 0xBAD)
    assert not registration_verify(sigma, attacker.E, attacker.pk, issuer.pk_X,
                                   issuer.pk_Y, proof, 0xBAE)


def test_03_fresh_false_identity_approval_and_compact_repair(backend):
    alice, bob, victim, fake_sk, rp, forged, old = false_identity_approval()
    assert not eq(mul(G1, fake_sk), alice.pk)
    assert chaum_pedersen_verify(alice.E, forged, alice.pk, bob.pk, old, 0xA, 0xB, 1)
    assert eq(elgamal_decrypt(forged, bob.sk), mul(G1, victim))
    ctx = ApprovalContext(0xA, 0xB, 1, 0xCAFE)
    bad = prove_approval(alice.E, forged, alice.pk, bob.pk, fake_sk, rp, ctx, seeded())
    assert not verify_approval(alice.E, forged, alice.pk, bob.pk, bad, ctx)
    honest_ct = elgamal_encrypt(alice.M, bob.pk, rp)
    good = prove_approval(alice.E, honest_ct, alice.pk, bob.pk, alice.sk, rp, ctx, seeded())
    assert verify_approval(alice.E, honest_ct, alice.pk, bob.pk, good, ctx)
    for field in ("sender", "spender", "chainid", "registry", "nonce"):
        changed = replace(ctx, **{field: getattr(ctx, field)+1})
        assert not verify_approval(alice.E, honest_ct, alice.pk, bob.pk, good, changed)
    for field in ("e", "u", "v"):
        changed = replace(good, **{field: getattr(good, field)+ORDER})
        assert not verify_approval(alice.E, honest_ct, alice.pk, bob.pk, changed, ctx)


def test_04_deterministic_leaf_links_accounts_salted_leaf_does_not_reuse(backend):
    a, b = Account(12345, 111, 222), Account(12345, 333, 444)
    assert a.pk != b.pk and a.E != b.E
    assert identity_leaf(a.M) == identity_leaf(b.M)
    leaf1 = credential_leaf(a.m, 555, 666)
    leaf2 = credential_leaf(a.m, 555, 777)
    assert leaf1 != leaf2
    # A known identity with a guessed holder secret/salt does not open this leaf.
    assert leaf1 != credential_leaf(a.m, 556, 666)
    # Distinct outputs alone do NOT prove unlinkability; see real showing tests.


def test_05_arbitrary_T_and_known_log_are_independent_gaps(backend):
    member, outsider, P, tree, witness = mismatched_membership()
    assert member != outsider and tree.path(0).leaf == identity_leaf(member)
    T = add(P, neg(member))
    assert eq(P, add(member, T))
    assert "b" not in witness
    m1, b1, m2, b2, P2 = double_opening()
    assert m1 != m2 and b1 != b2
    assert eq(P2, add(mul(G1, m2), mul(H_POINT, b2)))
    H = independent_generator()
    assert H != H_POINT and H != mul(G1, H_SCALAR)
    assert not eq(add(mul(G1, m1), mul(H, b1)), add(mul(G1, m2), mul(H, b2)))
    # Unknown log alone does not bind arbitrary POINT messages.
    delta = 9
    shifted_M = add(mul(G1, m1), mul(H, delta))
    assert eq(add(mul(G1, m1), mul(H, b1)), add(shifted_M, mul(H, b1-delta)))


def test_06_pool_and_previous_counterparty_have_real_decryption_capabilities(backend):
    a, pool = Account(12345, 111, 222), Account(67890, 333, 444)
    approval = elgamal_encrypt(a.M, pool.pk, 555)
    assert eq(elgamal_decrypt(approval, pool.sk), a.M)
    # A2's identity-derived receiving key lets a previous counterparty use m.
    issuer_M = mul(G1, 999)
    public_eiss = elgamal_encrypt(issuer_M, a.M, 666)
    assert eq(elgamal_decrypt(public_eiss, a.m), issuer_M)
    # Independent payment secret removes precisely this known-m decryption.
    note_secret = 777
    private_eiss = elgamal_encrypt(issuer_M, mul(G1, note_secret), 666)
    assert eq(elgamal_decrypt(private_eiss, note_secret), issuer_M)
    assert not eq(elgamal_decrypt(private_eiss, a.m), issuer_M)


def test_canonical_records_are_credential_instances_not_unique_people(backend):
    from alberta_buck.wallet.identity import canonical_json, identity_scalar
    a = {"name": "Synthetic Alice", "issuer": "Review", "issued_at": 1}
    b = dict(a, issued_at=2)
    assert identity_scalar(canonical_json(a)) != identity_scalar(canonical_json(b))
    archive = [canonical_json(a), canonical_json(b)]
    assert json.loads(archive[0])["issued_at"] == 1


def test_bearer_opening_copy_survives_handoff_but_outsider_key_fails(backend):
    issuer_M, bearer_secret = mul(G1, 12345), 67890
    evidence = elgamal_encrypt(issuer_M, mul(G1, bearer_secret), 555)
    prior_holder_copy = bearer_secret
    final_holder_copy = bearer_secret
    assert eq(elgamal_decrypt(evidence, prior_holder_copy), issuer_M)
    assert eq(elgamal_decrypt(evidence, final_holder_copy), issuer_M)
    assert not eq(elgamal_decrypt(evidence, 67891), issuer_M)
    # The mitigation is a NARROWER property: outsider privacy, not erasure.


@pytest.mark.parametrize("flavor", ["a1", "a2", "b1"])
def test_receipt_offline_success_does_not_authenticate_invented_chain_anchor(flavor):
    from alberta_buck.sim.notes_stack import E2EFixture
    from alberta_buck.wallet.envelope import serialize_core, deserialize_core
    from alberta_buck.wallet.verify_receipt import verify_receipt
    fx = E2EFixture.load(flavor)
    contracts = {k: "0x"+"11"*20 for k in ("registry", "buck", "notes")}
    anchor = dict(txhash="0x"+"22"*32, block=123, logindex=0, timestamp=123456)
    receipt = fx.build_receipt("recipient", contracts, anchor, anchor, rng=seeded())
    encoded = serialize_core(receipt)
    assert encoded.startswith(b"{") and json.loads(encoded)
    result = verify_receipt(deserialize_core(encoded))
    assert result.ok, result.reason
    # No local chain exists in this test. Offline consistency is a useful but
    # strictly smaller claim than inclusion/authentication on an actual chain.
