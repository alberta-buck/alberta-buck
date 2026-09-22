"""The delivery: what an addressed note carries, and who can read it.

Three parties touch a delivery -- the minter who writes it, the channel that carries it, and the
recipient who opens it -- and the tests are organised by what each may and may not learn.
"""

import pytest

from alberta_buck.wallet.bn254 import G1, add, eq, mul, neg
from alberta_buck.wallet.delivery import (
    DeliveryRefused, deliver_a1, deliver_a2, open_a1, open_a2,
)
from alberta_buck.wallet.notes import nullifier_b, note_commitment
from alberta_buck.wallet.recvkey import receiving_key
from alberta_buck.wallet.unilateral_a1 import mint_unilateral_a1
from alberta_buck.wallet.unilateral_a2 import mint_unilateral_a2
from alberta_buck.wallet.elgamal import elgamal_encrypt

CHAINID                         = 1
ISSUER                          = 0xC0FFEE
FACE                            = 250


def _rng(seed=0xDE11):
    state = {"x": seed}

    def rng():
        x = state["x"]
        x ^= (x << 13) & ((1 << 256) - 1)
        x ^= x >> 7
        x ^= (x << 17) & ((1 << 256) - 1)
        state["x"] = x
        return x
    return rng


@pytest.fixture
def world():
    rng = _rng()
    k, pk_recv = receiving_key(0x5EED_A11CE)
    m_rec, m_iss = 0x1234_5678, 0x9ABC_DEF0
    M_rec = mul(G1, m_rec)
    sk_iss = 0x51_55
    pk_iss = mul(G1, sk_iss)
    E_iss = elgamal_encrypt(mul(G1, m_iss), pk_iss, 0x77)
    sig_k = 0x5EED
    return dict(rng=rng, k=k, pk_recv=pk_recv, m_rec=m_rec, M_rec=M_rec, m_iss=m_iss,
                sk_iss=sk_iss, E_iss=E_iss, sigma_R=mul(G1, sig_k), sigma_s=0x5155)


def _a1(w):
    minted = mint_unilateral_a1(w["M_rec"], w["pk_recv"], v=FACE, rho=0xB0B0, m_issuer=w["m_iss"],
                                sigma_R=w["sigma_R"], sigma_s=w["sigma_s"], rng=w["rng"])
    return minted, deliver_a1(minted, w["pk_recv"], w["sigma_R"], w["sigma_s"])


def _a2(w):
    minted = mint_unilateral_a2(w["sk_iss"], w["E_iss"], w["pk_recv"], v=FACE, rho=0xB0B0,
                                issuer=ISSUER, chainid=CHAINID, salt_iss=0x5A17, rng=w["rng"])
    return minted, deliver_a2(minted, w["pk_recv"])


# ---- the recipient opens what the minter wrote ----------------------------------------------------

def test_a1_round_trip_recomputes_the_commitment(world):
    minted, d = _a1(world)
    opened = open_a1(d, world["k"], world["m_iss"])
    assert opened.cm == minted.cm == note_commitment(minted.opening)
    assert opened.opening.v == FACE and opened.r_note == minted.r_note


def test_a2_round_trip_recovers_the_spend_witness(world):
    minted, d = _a2(world)
    opened = open_a2(d, world["k"])
    assert opened.cm == minted.cm
    assert opened.r_prime == minted.r_prime and opened.salt_iss == 0x5A17
    assert eq(opened.M_I, minted.M_I)          # the issuer the recipient will name


# ---- the channel reads nothing --------------------------------------------------------------------

def test_the_channel_learns_neither_the_face_nor_the_spend(world):
    """rho with idHash IS the nullifier: in the clear, the channel would learn when the note is
    spent.  The face would be readable while the note is in flight."""
    minted, d = _a2(world)
    assert int(d["vWrapped"]) != FACE
    assert nullifier_b(int(d["rhoWrapped"]), minted.idHash) != nullifier_b(0xB0B0, minted.idHash)


def test_the_channel_cannot_read_the_issuer(world):
    """In the clear, r' is the issuer's Identity: M = C - r'*pk_recv."""
    minted, d = _a2(world)
    blob = int(d["rPrimeWrapped"])
    assert not eq(add(minted.eIss.C, neg(mul(world["pk_recv"], blob))), minted.M_I)


def test_each_field_wears_its_own_mask(world):
    """One pad over several scalars leaks their differences; each field is keyed by its name."""
    minted, d = _a2(world)
    diffs = {int(d["rhoWrapped"]) - 0xB0B0, int(d["vWrapped"]) - FACE,
             int(d["rPrimeWrapped"]) - minted.r_prime, int(d["saltIssWrapped"]) - 0x5A17}
    assert len(diffs) == 4


# ---- a delivery that is not this recipient's is refused, with a reason ---------------------------

def test_another_mailbox_is_refused(world):
    _, d = _a2(world)
    k_other, _ = receiving_key(0x0DD)
    with pytest.raises(DeliveryRefused, match="k does not open eNote"):
        open_a2(d, k_other)


def test_a_corrupted_randomness_is_refused(world):
    _, d = _a2(world)
    bad = dict(d, rPrimeWrapped=str(int(d["rPrimeWrapped"]) + 1))
    with pytest.raises(DeliveryRefused, match="r' is not eIss's randomness"):
        open_a2(bad, world["k"])


def test_a1_claiming_another_issuer_opens_to_another_note(world):
    """idHash commits the issuer's scalar, so the wrong issuer recomputes a different cm."""
    minted, d = _a1(world)
    assert open_a1(d, world["k"], world["m_iss"] + 1).cm != minted.cm


def test_flavours_do_not_cross(world):
    _, d1 = _a1(world)
    _, d2 = _a2(world)
    with pytest.raises(DeliveryRefused, match="not an A2"):
        open_a2(d1, world["k"])
    with pytest.raises(DeliveryRefused, match="not an A1"):
        open_a1(d2, world["k"], world["m_iss"])


def test_an_a2_delivery_needs_the_naming_salt(world):
    minted = mint_unilateral_a2(world["sk_iss"], world["E_iss"], world["pk_recv"], v=FACE,
                                rho=0xB0B0, issuer=ISSUER, chainid=CHAINID, rng=world["rng"])
    with pytest.raises(ValueError, match="naming salt"):
        deliver_a2(minted, world["pk_recv"])
