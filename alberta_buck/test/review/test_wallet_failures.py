# SPDX-License-Identifier: GPL-3.0-or-later
"""Review tests: each names a finding of doc/review/identity-findings*.

Tests 1, 2, 3 and 9 are INVERTED: they now assert the production verifier
rejects the attack the review demonstrated (registration presentation A' on
branch feature/a-prime; approval and account-key repairs earlier).  The
remaining tests still PASS BY REPRODUCING an unresolved finding.  When
production changes again, revisit the assertions deliberately; never weaken
one to accept either outcome.
"""
from dataclasses import replace
import json

from alberta_buck.review.examples import (
    Account, seeded, harvested_registration, false_identity_approval,
    double_opening, mismatched_membership, aliased_g1tie_limbs,
    uncontrolled_registration,
)
from alberta_buck.review.mitigations import (
    ApprovalContext, prove_approval, verify_approval, independent_generator,
    credential_leaf, prove_key_ownership, verify_key_ownership,
    membership_proof_required, APPROVE_DOMAIN,
)
from alberta_buck.wallet.bn254 import G1, G2, ORDER, Z1, add, mul, neg, eq, pairing
from alberta_buck.wallet.ps import ps_verify, ps_keygen, ps_sign, ps_present, PSSignature
from alberta_buck.wallet.nizk import registration_prove, registration_verify, presentation_point
from alberta_buck.wallet.chaum_pedersen import chaum_pedersen_prove, chaum_pedersen_verify
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.issuer_reenc import H_POINT, H_SCALAR
from alberta_buck.registry.tree import identity_leaf
import pytest


def test_01_published_presentation_is_not_a_testable_signature(backend):
    """Findings 1 and R2 inverted under A': the published pair (A, B) and the
    proof fields give the issuer and any m-holder no candidate test."""
    issuer, owner, pres, proof, *_ = harvested_registration()
    assert registration_verify(pres, owner.E, owner.pk, issuer.pk_X, issuer.pk_Y,
                               proof, 0xA11CE)
    # R1, public key: the pair is not a signature on the true m (or any other).
    assert not ps_verify(issuer.pk_X, issuer.pk_Y, PSSignature(pres.A, pres.B), owner.m)
    # R1, issuer secret: one G1 multiplication no longer identifies it either.
    assert not eq(pres.B, mul(pres.A, (issuer.sk_x + owner.m * issuer.sk_y) % ORDER))
    # R2: the commitment C1 no longer exposes m*A up to the challenge.
    assert not eq(mul(pres.A, proof.s_m),
                  add(proof.C1, mul(pres.A, (proof.e * owner.m) % ORDER)))
    # The only derivable point is P = m*A + b*G; every candidate m_i implies its
    # own b_i*G = P - m_i*A, so the pairing relation holds for all of them.
    P = presentation_point(pres, proof)
    for cand in (owner.m, owner.m + 1):
        bG = add(P, neg(mul(pres.A, cand)))
        assert pairing(G2, pres.B) == pairing(issuer.pk_X, pres.A) * pairing(
            issuer.pk_Y, add(mul(pres.A, cand), bG))


def test_02_disclosed_record_and_public_presentation_do_not_register_attacker(backend):
    """Finding 2 inverted under A': m plus the public transcript is not a
    credential.  Re-presenting (A, B) with a fresh blinding cannot be proven
    without the original b; the legitimate holder still registers."""
    issuer, owner, pres, owner_proof, attacker, att_pres, att_proof = harvested_registration()
    assert attacker.sk != owner.sk and not eq(pres.A, att_pres.A)
    assert not registration_verify(att_pres, attacker.E, attacker.pk, issuer.pk_X,
                                   issuer.pk_Y, att_proof, 0xBAD)
    assert registration_verify(pres, owner.E, owner.pk, issuer.pk_X, issuer.pk_Y,
                               owner_proof, 0xA11CE)
    assert not registration_verify(pres, owner.E, owner.pk, issuer.pk_X, issuer.pk_Y,
                                   owner_proof, 0xA11CF)


def test_10_fresh_blinding_is_security_critical(backend):
    """Reusing b across two presentations of one credential is a candidate
    test: P_1 - P_2 = m*(A_1 - A_2).  Fresh b removes it."""
    issuer = ps_keygen(seeded(21))
    owner = Account(12345, 45678, 98765)
    sigma = ps_sign(issuer, owner.m, seeded(22))
    p1, _, b1 = ps_present(sigma, issuer.pk_Y1, seeded(23))
    p2, _, _ = ps_present(sigma, issuer.pk_Y1, b=b1, a=777)   # same b, fresh a
    k1 = registration_prove(p1, b1, owner.m, owner.r, owner.pk, owner.E, 1, owner.sk, 1, seeded(24))
    k2 = registration_prove(p2, b1, owner.m, owner.r, owner.pk, owner.E, 2, owner.sk, 1, seeded(25))
    P1, P2 = presentation_point(p1, k1), presentation_point(p2, k2)
    assert eq(add(P1, neg(P2)), mul(add(p1.A, neg(p2.A)), owner.m))          # linkable
    p3, _, b3 = ps_present(sigma, issuer.pk_Y1, seeded(26))                   # fresh b
    k3 = registration_prove(p3, b3, owner.m, owner.r, owner.pk, owner.E, 3, owner.sk, 1, seeded(27))
    P3 = presentation_point(p3, k3)
    assert not eq(add(P1, neg(P3)), mul(add(p1.A, neg(p3.A)), owner.m))


def test_11_infinity_presentation_and_inconsistent_key_are_rejected(backend):
    """A = O makes the credential term vanish, so the verifier must refuse it;
    a Y1 that does not match Y must be refused when the key is trusted."""
    from alberta_buck.wallet.ps import PSPresentation, ps_key_consistent
    issuer = ps_keygen(seeded(31))
    owner = Account(12345, 45678, 98765)
    pres, _, b = ps_present(ps_sign(issuer, owner.m, seeded(32)), issuer.pk_Y1, seeded(33))
    proof = registration_prove(pres, b, owner.m, owner.r, owner.pk, owner.E, 1, owner.sk, 1, seeded(34))
    assert registration_verify(pres, owner.E, owner.pk, issuer.pk_X, issuer.pk_Y, proof, 1)
    dead = PSPresentation(Z1, pres.B)
    bad = registration_prove(dead, b, 0, owner.r, owner.pk, owner.E, 1, owner.sk, 1, seeded(35))
    assert not registration_verify(dead, owner.E, owner.pk, issuer.pk_X, issuer.pk_Y, bad, 1)
    with pytest.raises(ValueError):
        ps_present(ps_sign(issuer, owner.m, seeded(36)), issuer.pk_Y1, a=0)
    assert ps_key_consistent(issuer.pk_X, issuer.pk_Y, issuer.pk_Y1)
    assert not ps_key_consistent(issuer.pk_X, issuer.pk_Y, mul(G1, issuer.sk_y + 1))
    assert not ps_key_consistent(issuer.pk_X, issuer.pk_Y, Z1)


def test_12_two_presentations_share_no_point_and_only_the_holder_can_extract(backend):
    """Cross-account: two showings of one credential share no public point.
    Stripping the blinding needs y*(b*G); subtracting b*G itself does not
    yield a signature."""
    issuer = ps_keygen(seeded(41))
    owner = Account(12345, 45678, 98765)
    sigma = ps_sign(issuer, owner.m, seeded(42))
    p1, _, b1 = ps_present(sigma, issuer.pk_Y1, seeded(43))
    p2, _, b2 = ps_present(sigma, issuer.pk_Y1, seeded(44))
    assert not eq(p1.A, p2.A) and not eq(p1.B, p2.B)
    naive = PSSignature(p1.A, add(p1.B, neg(mul(G1, b1))))
    assert not ps_verify(issuer.pk_X, issuer.pk_Y, naive, owner.m)
    stripped = PSSignature(p1.A, add(p1.B, neg(mul(issuer.pk_Y1, b1))))   # needs b (holder only)
    assert ps_verify(issuer.pk_X, issuer.pk_Y, stripped, owner.m)


def test_03_fresh_false_identity_approval_and_compact_repair(backend):
    alice, bob, victim, fake_sk, rp, forged, old = false_identity_approval()
    assert not eq(mul(G1, fake_sk), alice.pk)
    # Production verifier rejects the forged witness (finding 3 inverted).
    assert not chaum_pedersen_verify(alice.E, forged, alice.pk, bob.pk, old, 0xA, 0xB, 1)
    assert eq(elgamal_decrypt(forged, bob.sk), mul(G1, victim))
    ctx = ApprovalContext(0xA, 0xB, 1, 0xCAFE)
    bad = prove_approval(alice.E, forged, alice.pk, bob.pk, fake_sk, rp, ctx, seeded())
    assert not verify_approval(alice.E, forged, alice.pk, bob.pk, bad, ctx)
    honest_ct = elgamal_encrypt(alice.M, bob.pk, rp)
    honest = chaum_pedersen_prove(
        alice.E, honest_ct, alice.pk, bob.pk, alice.sk, rp, 0xA, 0xB, 1, seeded(),
    )
    assert chaum_pedersen_verify(alice.E, honest_ct, alice.pk, bob.pk, honest, 0xA, 0xB, 1)
    assert not chaum_pedersen_verify(alice.E, honest_ct, alice.pk, bob.pk, honest, 0xA, 0xB, 2)
    assert not chaum_pedersen_verify(alice.E, honest_ct, Z1, bob.pk, honest, 0xA, 0xB, 1)
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


def test_08_empty_membership_proof_is_not_a_proof(backend):
    # Models Notes._verifyIdentityMembership fail-closed gate: a spend is
    # accepted only with a nonempty proof AND a wired verifier (finding 8).
    assert not membership_proof_required(b"", True)
    assert not membership_proof_required(b"\x00" * 256, False)
    assert membership_proof_required(b"\x00" * 256, True)


def test_09_registration_accepts_a_public_key_with_no_known_secret(backend):
    issuer, owner, pk, E, pres, proof = uncontrolled_registration()
    # Production verifier rejects a NUMS pk (finding 9 inverted).
    assert not registration_verify(pres, E, pk, issuer.pk_X, issuer.pk_Y, proof, 0xBAD)
    # No scalar we have satisfies pk = sk*G.
    assert not eq(pk, mul(G1, owner.sk))
    domain = APPROVE_DOMAIN
    bogus = prove_key_ownership(pk, owner.sk, domain, seeded())
    assert not verify_key_ownership(pk, bogus, domain)
    honest_sk = 45678
    honest_pk = mul(G1, honest_sk)
    good = prove_key_ownership(honest_pk, honest_sk, domain, seeded())
    assert verify_key_ownership(honest_pk, good, domain)
    # Honest key still registers.
    honest = Account(12345, 45678, 98765)
    pres_h, _, b_h = ps_present(ps_sign(issuer, honest.m, seeded(11)), issuer.pk_Y1, seeded(12))
    pf = registration_prove(
        pres_h, b_h, honest.m, honest.r, honest.pk, honest.E, 0xA11C, honest.sk, 1, seeded(13),
    )
    assert registration_verify(
        pres_h, honest.E, honest.pk, issuer.pk_X, issuer.pk_Y, pf, 0xA11C, 1,
    )
    assert not registration_verify(
        pres_h, honest.E, honest.pk, issuer.pk_X, issuer.pk_Y, pf, 0xA11C, 2,
    )


def test_07_a1_spend_public_inputs_bind_flavor(backend):
    from pathlib import Path
    import json
    path = Path(__file__).resolve().parents[1] / "vectors" / "e2e" / "a1.json"
    if not path.is_file():
        pytest.skip("A1 e2e fixture not present")
    d = json.loads(path.read_text())
    assert int(d["opening"]["flavor"]) == 1
    pub = d["spend"]["public"]
    assert int(pub["flavor"]) == int(d["spend"]["witness"]["flavor"]) == 1
    assert int(pub["issuanceCommitment"]) == 0
    # Same opening, different payout account: the circuit still has a witness.
    w = d["spend"]["witness"]
    assert int(w["recipient"]) != 0xBAD
    # Nullifier is independent of recipient, so a fresh proof can redirect payout.
    assert "recipient" in pub


def test_05c_limb_carry_preserves_poseidon_leaf_not_ec_limbs(backend):
    member, outsider, P, tree, witness = mismatched_membership()
    aliased = aliased_g1tie_limbs(witness)
    assert aliased["Mx"] != witness["Mx"]
    rec = lambda ls: ls[0] + (ls[1] << 64) + (ls[2] << 128) + (ls[3] << 192)
    assert rec(aliased["Mx"]) == rec(witness["Mx"]) == witness["Mx_mod"]
    assert aliased["Mx"][0] != witness["Mx"][0]


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
    if flavor in ("a1", "a2"):
        import pytest as _pytest
        _pytest.skip(
            "AB-RCPT addressed legs await the receiving-key rework: the "
            "verifier decrypts with an identity-derived scalar, and addressed "
            "notes are keyed to a receiving key that no identity yields.  See "
            "test_receipt_e2e.test_fixture_receipt_verifies.")
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
