"""B1 depositor binding -- the dual of the A2 issuer binding.

A public issuer mints a bearer (B1) note; an unknown depositor cashes it and, at
spend, re-encrypts its own registered Identity M_dep under the issuer's public key
and proves -- hiding every Identity -- that the ciphertext encrypts a registered
Identity bound to its payout account.  The issuer then scans the event, decrypts
with sk_iss, and produces an issuer-unilateral receipt naming the depositor.

Mirrors test_unilateral_a2.py with the roles swapped (issuer decrypts the
depositor's Identity, not the other way round).
"""

from alberta_buck.wallet.bn254 import G1, ORDER, mul, add, neg, eq, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.unilateral_a2 import IdentityTree
from alberta_buck.wallet.b1_binding import (
    b1_bind_prove, b1_bind_verify,
    make_issuer_receipt, verify_issuer_receipt,
)

CHAINID = 1
ISSUER_ADDR = 0xC0FFEE
DEPOSIT_ADDR = 0xB0B


def _seeded_rng(seed=0xB1):
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


import pytest


@pytest.fixture
def world():
    rng = _seeded_rng()
    issuer = Account(rand_scalar(rng), rng)     # public issuer: M_iss, sk_iss/pk_iss
    dep = Account(rand_scalar(rng), rng)         # depositor: m_dep, payout account
    tree = IdentityTree(depth=10)
    for _ in range(2):
        tree.insert(mul(G1, rand_scalar(rng)))
    tree.insert(issuer.M)
    tree.insert(dep.M)
    return dict(rng=rng, issuer=issuer, dep=dep, tree=tree)


def test_binding_verifies(world):
    dep, issuer = world["dep"], world["issuer"]
    proof, eDepForIss = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                                      DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    assert b1_bind_verify(dep.pk, dep.E, issuer.pk, eDepForIss, proof,
                          DEPOSIT_ADDR, CHAINID)


def test_issuer_receipt_names_depositor(world):
    """The issuer ALONE decrypts the depositor's Identity from the event and
    produces a third-party-checkable receipt naming both parties."""
    dep, issuer, tree = world["dep"], world["issuer"], world["tree"]
    proof, eDepForIss = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                                      DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    assert b1_bind_verify(dep.pk, dep.E, issuer.pk, eDepForIss, proof,
                          DEPOSIT_ADDR, CHAINID)

    receipt = make_issuer_receipt(issuer.sk, issuer.M, eDepForIss, value=750,
                                  issuer=ISSUER_ADDR, chainid=CHAINID, tree=tree,
                                  rng=world["rng"])
    res = verify_issuer_receipt(receipt, tree.root(), tree)
    assert res.valid, res.reason
    assert eq(res.issuer_M, issuer.M)            # issuer named (public)
    assert eq(res.recipient_M, dep.M)            # depositor named (decrypted)
    assert res.value == 750


def test_mallory_cannot_decrypt(world):
    """Only the issuer (sk_iss) recovers M_dep; Mallory sees an opaque ciphertext."""
    dep, issuer = world["dep"], world["issuer"]
    _, eDepForIss = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                                  DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    assert eq(elgamal_decrypt(eDepForIss, issuer.sk), dep.M)        # issuer recovers it
    sk_mallory = rand_scalar(world["rng"])
    assert not eq(elgamal_decrypt(eDepForIss, sk_mallory), dep.M)   # nobody else


def test_collusion_substituted_ciphertext_rejected(world):
    """The depositor cannot substitute a ciphertext encrypting a *different*
    Identity (to hide/frame): F2 is coupled to the account-bound m_dep via E2."""
    dep, issuer = world["dep"], world["issuer"]
    proof, _ = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                             DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    # A ciphertext over some OTHER identity under the same issuer key.
    M_other = mul(G1, rand_scalar(world["rng"]))
    bogus = elgamal_encrypt(M_other, issuer.pk, rand_scalar(world["rng"]))
    assert not b1_bind_verify(dep.pk, dep.E, issuer.pk, bogus, proof,
                              DEPOSIT_ADDR, CHAINID)


def test_binding_tamper_rejected(world):
    dep, issuer = world["dep"], world["issuer"]
    proof, eDepForIss = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                                      DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    import dataclasses
    bad = dataclasses.replace(proof, s_m=(proof.s_m + 1) % ORDER)
    assert not b1_bind_verify(dep.pk, dep.E, issuer.pk, eDepForIss, bad,
                              DEPOSIT_ADDR, CHAINID)


def test_binding_wrong_issuer_key_rejected(world):
    dep, issuer = world["dep"], world["issuer"]
    proof, eDepForIss = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                                      DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    pk_other = mul(G1, rand_scalar(world["rng"]))
    assert not b1_bind_verify(dep.pk, dep.E, pk_other, eDepForIss, proof,
                              DEPOSIT_ADDR, CHAINID)


def test_binding_wrong_chainid_rejected(world):
    dep, issuer = world["dep"], world["issuer"]
    proof, eDepForIss = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                                      DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    assert not b1_bind_verify(dep.pk, dep.E, issuer.pk, eDepForIss, proof,
                              DEPOSIT_ADDR, CHAINID + 1)


def test_two_deposits_unlinkable(world):
    """Two B1 deposits by the same depositor to the same issuer yield distinct
    event ciphertexts (fresh r), so Mallory cannot group them."""
    dep, issuer = world["dep"], world["issuer"]
    _, a = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk, DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    _, b = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk, DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    assert not eq(a.R, b.R)
    assert not eq(a.C, b.C)


# --------------------------------------------------------------------------- #
# P_dep: the membership commitment that binds M_dep to the spend's membership.
# --------------------------------------------------------------------------- #

from alberta_buck.wallet.nums import H_PEDERSEN


def test_p_dep_commits_m_dep_and_is_member(world):
    """The binding publishes P_dep = M_dep + b*H; subtracting the (known to the
    depositor) blind recovers M_dep, a tree member.  This is the point the spend's
    G1-tie membership proof certifies -- bound to the same m_dep as F2."""
    dep, issuer, tree = world["dep"], world["issuer"], world["tree"]
    b = rand_scalar(world["rng"])
    proof, eDepForIss = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                                      DEPOSIT_ADDR, CHAINID, b=b, rng=world["rng"])
    assert b1_bind_verify(dep.pk, dep.E, issuer.pk, eDepForIss, proof,
                          DEPOSIT_ADDR, CHAINID)
    # P_dep - b*H == M_dep, and M_dep is a registered member.
    M_dep_recovered = add(proof.P_dep, neg(mul(H_PEDERSEN, b)))
    assert eq(M_dep_recovered, dep.M)
    assert tree.contains(dep.M)


def test_tampered_p_dep_rejected(world):
    """Perturbing P_dep breaks the P relation (s_m*G + s_b*H == A_p + e*P_dep)."""
    dep, issuer = world["dep"], world["issuer"]
    proof, eDepForIss = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                                      DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    import dataclasses
    bad = dataclasses.replace(proof, P_dep=add(proof.P_dep, G1))
    assert not b1_bind_verify(dep.pk, dep.E, issuer.pk, eDepForIss, bad,
                              DEPOSIT_ADDR, CHAINID)


def test_tampered_s_b_rejected(world):
    dep, issuer = world["dep"], world["issuer"]
    proof, eDepForIss = b1_bind_prove(dep.m, dep.sk, dep.E, issuer.pk,
                                      DEPOSIT_ADDR, CHAINID, rng=world["rng"])
    import dataclasses
    bad = dataclasses.replace(proof, s_b=(proof.s_b + 1) % ORDER)
    assert not b1_bind_verify(dep.pk, dep.E, issuer.pk, eDepForIss, bad,
                              DEPOSIT_ADDR, CHAINID)
