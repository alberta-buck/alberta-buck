"""Identity-targeted unilateral A2 Note -- end-to-end demonstration.

Exercises the full flow of alberta-buck-notes-unilateral.org:

  mint (issuer)  ->  off-chain delivery + verify  ->  deposit coupling (EVM gate)
                 ->  unilateral recipient receipt naming BOTH identities

and the security properties: collusion-resistance (a bogus eIss decrypts to a
non-member and the receipt is INVALID), any-account flexibility (the issuer knows
only M_rec, any of the recipient's accounts can deposit), soundness of the
deposit coupling sigma, and privacy (no identity appears in the public proof).
"""

import pytest

from alberta_buck.wallet.bn254 import G1, ORDER, mul, add, neg, eq, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.issuer_reenc import issuer_reenc_verify
from alberta_buck.wallet.unilateral_a2 import (
    IdentityTree, identity_leaf,
    mint_unilateral_a2,
    deposit_couple_prove, deposit_couple_verify,
    make_receipt, verify_receipt,
)

CHAINID = 1
ISSUER_ADDR = 0xA11CE
DEPOSIT_ADDR = 0xB0B


def _seeded_rng(seed=0x5EED):
    state = {"x": seed}

    def rng():
        # xorshift-ish deterministic stream for reproducible vectors
        x = state["x"]
        x ^= (x << 13) & ((1 << 256) - 1)
        x ^= (x >> 7)
        x ^= (x << 17) & ((1 << 256) - 1)
        state["x"] = x
        return x % ORDER

    return rng


class Account:
    """One Fountain-derived account bound to identity scalar ``m``."""
    def __init__(self, m, rng):
        self.m = m % ORDER
        self.M = mul(G1, self.m)                 # identity point M = m*G
        self.sk = rand_scalar(rng)               # account key
        self.pk = mul(G1, self.sk)
        r = rand_scalar(rng)
        self.E = elgamal_encrypt(self.M, self.pk, r)   # registered credential


@pytest.fixture
def world():
    """An issuer identity, a recipient identity with two accounts, and a
    registry-identity tree containing both (plus decoys)."""
    rng = _seeded_rng()
    m_iss = rand_scalar(rng)
    m_rec = rand_scalar(rng)
    issuer = Account(m_iss, rng)                  # issuer's registered account
    rec0 = Account(m_rec, rng)                    # recipient account #0
    rec1 = Account(m_rec, rng)                    # recipient account #1 (same identity)

    tree = IdentityTree(depth=10)
    # decoys + the two real identities
    for _ in range(3):
        tree.insert(mul(G1, rand_scalar(rng)))
    tree.insert(issuer.M)
    tree.insert(rec0.M)                           # one leaf per *identity* (rec0.M == rec1.M)
    for _ in range(2):
        tree.insert(mul(G1, rand_scalar(rng)))

    return dict(rng=rng, m_iss=m_iss, m_rec=m_rec,
                issuer=issuer, rec0=rec0, rec1=rec1, tree=tree)


# --------------------------------------------------------------------------- #
# Completeness: the happy path end to end.
# --------------------------------------------------------------------------- #

def test_mint_binding_verifies(world):
    """The mint anti-framing binding (issuer_reenc with pk_rec := M_rec) verifies."""
    iss, m_rec = world["issuer"], world["m_rec"]
    M_rec = mul(G1, m_rec)
    minted = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=1000, rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    assert issuer_reenc_verify(iss.pk, iss.E, minted.eIss, minted.binding,
                               ISSUER_ADDR, CHAINID)
    # The issuer encrypted its *own* registered identity.
    assert eq(minted.M_I, iss.M)


def test_delivery_recipient_decrypts_issuer(world):
    """At delivery the recipient decrypts eIss under m_rec and recovers the
    issuer's true identity, which is a registered member."""
    iss, m_rec, tree = world["issuer"], world["m_rec"], world["tree"]
    M_rec = mul(G1, m_rec)
    minted = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=1000, rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    M_I = elgamal_decrypt(minted.eIss, m_rec)
    assert eq(M_I, iss.M)                         # names the issuer
    assert tree.contains(M_I)                     # ... a registered identity


def test_deposit_coupling_verifies(world):
    """The depositor proves eligibility (any account bound to m_rec) and the
    coupling sigma verifies."""
    iss, m_rec, rec0 = world["issuer"], world["m_rec"], world["rec0"]
    M_rec = mul(G1, m_rec)
    minted = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=1000, rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    proof = deposit_couple_prove(m_rec, rec0.sk, rec0.E, minted.eIss,
                                 DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    assert deposit_couple_verify(rec0.pk, rec0.E, minted.eIss, proof,
                                 DEPOSIT_ADDR, CHAINID)


def test_unilateral_receipt_names_both(world):
    """The recipient *alone* produces a receipt naming both plaintext identities,
    third-party-checkable with no secret."""
    iss, m_rec, tree = world["issuer"], world["m_rec"], world["tree"]
    M_rec = mul(G1, m_rec)
    minted = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=2500, rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    receipt = make_receipt(m_rec, minted, ISSUER_ADDR, CHAINID, tree, rng=world["rng"])

    # A third party (no secret) verifies against the issuer's registry record + root.
    res = verify_receipt(receipt, iss.pk, iss.E, tree.root(), tree)
    assert res.valid, res.reason
    assert eq(res.issuer_M, iss.M)               # issuer named
    assert eq(res.recipient_M, M_rec)            # recipient named
    assert res.value == 2500


# --------------------------------------------------------------------------- #
# Any-account flexibility: the issuer knows only M_rec, any account deposits.
# --------------------------------------------------------------------------- #

def test_any_account_can_deposit(world):
    """The issuer addressed the identity, not an account: both of the
    recipient's accounts can deposit and receipt the same note."""
    iss, m_rec, tree = world["issuer"], world["m_rec"], world["tree"]
    rec0, rec1 = world["rec0"], world["rec1"]
    M_rec = mul(G1, m_rec)
    minted = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=10, rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    for acct in (rec0, rec1):
        p = deposit_couple_prove(m_rec, acct.sk, acct.E, minted.eIss,
                                 DEPOSIT_ADDR, CHAINID, rng=world["rng"])
        assert deposit_couple_verify(acct.pk, acct.E, minted.eIss, p,
                                     DEPOSIT_ADDR, CHAINID), "every account bound to M_rec deposits"


# --------------------------------------------------------------------------- #
# Collusion-resistance: a bogus eIss is un-nameable AND un-spendable.
# --------------------------------------------------------------------------- #

def test_collusion_bogus_eiss_unnameable(world):
    """A colluding issuer keys eIss to a throwaway point (not M_rec).  The mint
    binding still passes (it only forces eIss over the issuer's own M), but the
    recipient's decryption lands on a NON-member, so the receipt is INVALID and
    the spend membership gate would reject it."""
    iss, m_rec, tree = world["issuer"], world["m_rec"], world["tree"]
    rng = world["rng"]
    pk_bogus = mul(G1, rand_scalar(rng))         # a key the recipient does not hold

    # Mint a note keyed to pk_bogus instead of M_rec (collusion).
    r_prime = rand_scalar(rng)
    eIss_bogus = elgamal_encrypt(iss.M, pk_bogus, r_prime)
    from alberta_buck.wallet.issuer_reenc import issuer_reenc_prove
    binding = issuer_reenc_prove(iss.sk, r_prime, pk_bogus, iss.E, eIss_bogus,
                                 ISSUER_ADDR, CHAINID, rng=rng)
    # Anti-framing still accepts (eIss really re-encrypts the issuer's own M)...
    assert issuer_reenc_verify(iss.pk, iss.E, eIss_bogus, binding, ISSUER_ADDR, CHAINID)

    # ... but the recipient's decryption is garbage, not a registered identity.
    M_I_bogus = elgamal_decrypt(eIss_bogus, m_rec)
    assert not eq(M_I_bogus, iss.M)
    assert not tree.contains(M_I_bogus), "bogus issuer identity must not be a tree member"

    # Build a receipt around the bogus note; verify_receipt must reject at the
    # membership link (the coupling).
    from alberta_buck.wallet.unilateral_a2 import (
        MintedA2, a2_id_hash, make_receipt as mk, verify_receipt as vr,
    )
    from alberta_buck.wallet.notes import NoteOpening, note_commitment, FLAVOR_A2
    M_rec = mul(G1, m_rec)
    eNote_bogus = elgamal_encrypt(mul(G1, 1000), M_rec, rand_scalar(rng))
    idh = a2_id_hash(eNote_bogus, eIss_bogus)
    opening = NoteOpening(FLAVOR_A2, 1000, rand_scalar(rng), idh, 0)
    minted_bogus = MintedA2(eNote=eNote_bogus, eIss=eIss_bogus, M_I=iss.M, idHash=idh,
                            cm=note_commitment(opening), opening=opening,
                            binding=binding, r_prime=r_prime, r_note=rand_scalar(rng))
    receipt = mk(m_rec, minted_bogus, ISSUER_ADDR, CHAINID, tree, rng=rng)
    res = vr(receipt, iss.pk, iss.E, tree.root(), tree)
    assert not res.valid
    assert "not a registered identity" in res.reason


# --------------------------------------------------------------------------- #
# Soundness of the deposit coupling sigma.
# --------------------------------------------------------------------------- #

def test_deposit_coupling_tamper_rejected(world):
    iss, m_rec, rec0 = world["issuer"], world["m_rec"], world["rec0"]
    M_rec = mul(G1, m_rec)
    minted = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=1, rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    proof = deposit_couple_prove(m_rec, rec0.sk, rec0.E, minted.eIss,
                                 DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    import dataclasses
    bad = dataclasses.replace(proof, s_m=(proof.s_m + 1) % ORDER)
    assert not deposit_couple_verify(rec0.pk, rec0.E, minted.eIss, bad,
                                     DEPOSIT_ADDR, CHAINID)


def test_deposit_coupling_wrong_account_rejected(world):
    """A proof made for the recipient's account #0 must not verify against a
    different account's registry record."""
    iss, m_rec, rec0, rec1 = world["issuer"], world["m_rec"], world["rec0"], world["rec1"]
    M_rec = mul(G1, m_rec)
    minted = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=1, rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    proof = deposit_couple_prove(m_rec, rec0.sk, rec0.E, minted.eIss,
                                 DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    # Verify against rec1's (pk, E) -- different account, different randomness.
    assert not deposit_couple_verify(rec1.pk, rec1.E, minted.eIss, proof,
                                     DEPOSIT_ADDR, CHAINID)


def test_deposit_coupling_wrong_chainid_rejected(world):
    iss, m_rec, rec0 = world["issuer"], world["m_rec"], world["rec0"]
    M_rec = mul(G1, m_rec)
    minted = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=1, rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    proof = deposit_couple_prove(m_rec, rec0.sk, rec0.E, minted.eIss,
                                 DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    assert not deposit_couple_verify(rec0.pk, rec0.E, minted.eIss, proof,
                                     DEPOSIT_ADDR, CHAINID + 1)


# --------------------------------------------------------------------------- #
# Privacy: no identity appears in the public deposit proof; mints unlinkable.
# --------------------------------------------------------------------------- #

def test_deposit_proof_hides_identities(world):
    iss, m_rec, rec0 = world["issuer"], world["m_rec"], world["rec0"]
    M_rec = mul(G1, m_rec)
    minted = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=1, rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    M_I = elgamal_decrypt(minted.eIss, m_rec)
    proof = deposit_couple_prove(m_rec, rec0.sk, rec0.E, minted.eIss,
                                 DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    # P_I is the only identity-derived public point, and it is blinded (!= M_I).
    assert not eq(proof.P_I, M_I)
    assert not eq(proof.P_I, M_rec)


def test_two_mints_unlinkable(world):
    """Two mints from the same issuer to the same recipient yield distinct eIss
    (fresh r'), so Mallory cannot group them."""
    iss, m_rec = world["issuer"], world["m_rec"]
    M_rec = mul(G1, m_rec)
    a = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=1, rho=rand_scalar(world["rng"]),
                           issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    b = mint_unilateral_a2(iss.sk, iss.E, M_rec, v=1, rho=rand_scalar(world["rng"]),
                           issuer=ISSUER_ADDR, chainid=CHAINID, rng=world["rng"])
    assert not eq(a.eIss.R, b.eIss.R)
    assert not eq(a.eIss.C, b.eIss.C)
