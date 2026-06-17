"""Identity-targeted A1 Note (addressed, public issuer) -- end-to-end.

A1 reuses the A2 deposit coupling verbatim: the note encrypts the recipient's
identity under itself (eRec = Enc(M_rec, M_rec)), so the same gadget proves the
spender is the addressed identity and the membership certifies M_rec is a
registered member.  The issuer is public (named at mint).  See
alberta-buck-notes.org ("Mutual Decryptability", A1 row of the one-gadget); see also notes-flow "Identity-M Spend Path".
"""

import pytest

from alberta_buck.wallet.bn254 import G1, ORDER, mul, add, neg, eq, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.unilateral_a1 import (
    mint_unilateral_a1,
    deposit_couple_prove, deposit_couple_verify,
    make_receipt_a1, verify_receipt_a1,
)
from alberta_buck.wallet.unilateral_a2 import IdentityTree
from alberta_buck.wallet.issuer_reenc import H_POINT

CHAINID = 1
ISSUER_ADDR = 0xA11CE
DEPOSIT_ADDR = 0xB0B


def _seeded_rng(seed=0x5EED):
    state = {"x": seed}

    def rng():
        x = state["x"]
        x ^= (x << 13) & ((1 << 256) - 1)
        x ^= (x >> 7)
        x ^= (x << 17) & ((1 << 256) - 1)
        state["x"] = x
        return x % ORDER

    return rng


class Account:
    def __init__(self, m, rng):
        self.m = m % ORDER
        self.M = mul(G1, self.m)
        self.sk = rand_scalar(rng)
        self.pk = mul(G1, self.sk)
        self.E = elgamal_encrypt(self.M, self.pk, rand_scalar(rng))


def _mock_schnorr(rng):
    """Return (sigma_R, sigma_s) for a synthetic Schnorr signature."""
    k = rand_scalar(rng)
    sigma_R = mul(G1, k)
    sigma_s = (k + rand_scalar(rng) * rand_scalar(rng)) % ORDER
    return sigma_R, sigma_s


@pytest.fixture
def world():
    rng = _seeded_rng()
    m_iss = rand_scalar(rng)
    m_rec = rand_scalar(rng)
    issuer = Account(m_iss, rng)        # public issuer, named at mint
    rec0 = Account(m_rec, rng)
    rec1 = Account(m_rec, rng)          # same identity, second account
    sigma_R, sigma_s = _mock_schnorr(rng)
    tree = IdentityTree(depth=10)
    for _ in range(2):
        tree.insert(mul(G1, rand_scalar(rng)))
    tree.insert(issuer.M)
    tree.insert(rec0.M)
    return dict(rng=rng, m_iss=m_iss, m_rec=m_rec,
                issuer=issuer, rec0=rec0, rec1=rec1, tree=tree,
                sigma_R=sigma_R, sigma_s=sigma_s)


# --------------------------------------------------------------------------- #
# Completeness.
# --------------------------------------------------------------------------- #

def test_mint_addresses_recipient_identity(world):
    """eRec encrypts the recipient identity under itself; it decrypts under m_rec
    back to M_rec -- so the note addresses the *identity*, not an account."""
    m_rec = world["m_rec"]
    M_rec = mul(G1, m_rec)
    note = mint_unilateral_a1(M_rec, v=1000, rho=rand_scalar(world["rng"]),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=world["rng"])
    assert eq(elgamal_decrypt(note.eRec, m_rec), M_rec)


def test_deposit_coupling_commits_recipient_identity(world):
    """The reused coupling proves account<->m_rec AND eRec decrypts under m_rec to
    P_I; here the committed point is M_rec (the recipient), not the issuer."""
    m_rec, rec0 = world["m_rec"], world["rec0"]
    M_rec = mul(G1, m_rec)
    note = mint_unilateral_a1(M_rec, v=1, rho=rand_scalar(world["rng"]),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=world["rng"])
    dc = deposit_couple_prove(m_rec, rec0.sk, rec0.E, note.eRec,
                              DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    assert deposit_couple_verify(rec0.pk, rec0.E, note.eRec, dc,
                                 DEPOSIT_ADDR, CHAINID)
    # P_I = M_rec + b*H, so P_I - b*H = M_rec is the recipient identity.  We cannot
    # see b, but the receipt below proves the underlying point is M_rec (a member).


def test_any_account_can_deposit(world):
    """The issuer addressed the identity M_rec: both of the recipient's accounts
    deposit the same A1 note."""
    m_rec, rec0, rec1 = world["m_rec"], world["rec0"], world["rec1"]
    M_rec = mul(G1, m_rec)
    note = mint_unilateral_a1(M_rec, v=5, rho=rand_scalar(world["rng"]),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=world["rng"])
    for acct in (rec0, rec1):
        dc = deposit_couple_prove(m_rec, acct.sk, acct.E, note.eRec,
                                  DEPOSIT_ADDR, CHAINID, rng=world["rng"])
        assert deposit_couple_verify(acct.pk, acct.E, note.eRec, dc,
                                     DEPOSIT_ADDR, CHAINID)


def test_unilateral_receipt_names_both(world):
    """The recipient alone names the public issuer and itself; third-party-checkable."""
    m_rec, issuer, tree = world["m_rec"], world["issuer"], world["tree"]
    M_rec = mul(G1, m_rec)
    note = mint_unilateral_a1(M_rec, v=2500, rho=rand_scalar(world["rng"]),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=world["rng"])
    receipt = make_receipt_a1(m_rec, note, issuer.M, ISSUER_ADDR, CHAINID, tree,
                              rng=world["rng"])
    res = verify_receipt_a1(receipt, tree.root(), tree)
    assert res.valid, res.reason
    assert eq(res.issuer_M, issuer.M)
    assert eq(res.recipient_M, M_rec)
    assert res.value == 2500


# --------------------------------------------------------------------------- #
# Soundness.
# --------------------------------------------------------------------------- #

def test_deposit_coupling_tamper_rejected(world):
    m_rec, rec0 = world["m_rec"], world["rec0"]
    M_rec = mul(G1, m_rec)
    note = mint_unilateral_a1(M_rec, v=1, rho=rand_scalar(world["rng"]),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=world["rng"])
    dc = deposit_couple_prove(m_rec, rec0.sk, rec0.E, note.eRec,
                              DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    import dataclasses
    bad = dataclasses.replace(dc, s_m=(dc.s_m + 1) % ORDER)
    assert not deposit_couple_verify(rec0.pk, rec0.E, note.eRec, bad,
                                     DEPOSIT_ADDR, CHAINID)


def test_deposit_coupling_wrong_account_rejected(world):
    m_rec, rec0, rec1 = world["m_rec"], world["rec0"], world["rec1"]
    M_rec = mul(G1, m_rec)
    note = mint_unilateral_a1(M_rec, v=1, rho=rand_scalar(world["rng"]),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=world["rng"])
    dc = deposit_couple_prove(m_rec, rec0.sk, rec0.E, note.eRec,
                              DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    assert not deposit_couple_verify(rec1.pk, rec1.E, note.eRec, dc,
                                     DEPOSIT_ADDR, CHAINID)


def test_unregistered_recipient_receipt_invalid(world):
    """A receipt for a recipient whose M_rec is absent from the tree is INVALID --
    the membership gate that the on-chain spend enforces."""
    issuer, tree, rng = world["issuer"], world["tree"], world["rng"]
    m_rec_unreg = rand_scalar(rng)             # never inserted
    M_rec = mul(G1, m_rec_unreg)
    note = mint_unilateral_a1(M_rec, v=1, rho=rand_scalar(rng),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=rng)
    receipt = make_receipt_a1(m_rec_unreg, note, issuer.M, ISSUER_ADDR, CHAINID,
                              tree, rng=rng)
    res = verify_receipt_a1(receipt, tree.root(), tree)
    assert not res.valid
    assert "recipient M not a registered identity" in res.reason
