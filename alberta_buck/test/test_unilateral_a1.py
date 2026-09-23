"""Identity-targeted A1 Note (addressed, public issuer) -- end-to-end.

An A1 note NAMES the recipient Identity (eRec's plaintext is M_rec) and is
KEYED to that Identity's registered receiving key (eRec is encrypted to
pk_recv).  Those are two objects: an identity scalar is a read capability the
design discloses to every counterparty, so it cannot also be a decryption key.

The spend is therefore the folded gate rather than the legacy sigma -- reading
the note and being the Identity are facts about two different secrets now, and
a gate proving them side by side would state nothing about their owner.  See
alberta-buck-notes.org ("Mutual Decryptability", A1 row of the one-gadget),
notes-flow "Identity-M Spend Path", and
doc/review/notes-receiving-key.org section 3.3a.
"""

import pytest

from alberta_buck.registry.tree import IdentityMerkleTree, receiving_leaf
from alberta_buck.wallet.bn254 import G1, ORDER, mul, add, neg, eq, rand_scalar
from alberta_buck.wallet.deposit_fold import (
    DepositFoldRefused, deposit_fold_check, deposit_fold_witness,
)
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.recvkey import receiving_key
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.wallet.unilateral_a1 import (
    mint_unilateral_a1,
    make_receipt_a1, verify_receipt_a1,
)
from alberta_buck.wallet.unilateral_a2 import IdentityTree

KYC = "kyc:ca-ab-2026"

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

    # The receiving key belongs to the IDENTITY, not to an account: every
    # account bound to m_rec spends, and one mailbox key opens the mail.
    seed_rec = rand_scalar(rng)
    k_rec, pk_recv = receiving_key(seed_rec)
    salt_rec = derive_salt(seed_rec, KYC)

    # Two trees, and they answer different questions.  `tree` is the registry
    # accumulator of identity POINTS, which is what a receipt's membership
    # checks are about.  `priv` is the private identity-registry subtree whose
    # leaves commit (Identity, receiving key) pairs, which is what the spend's
    # tie relation is about.
    tree = IdentityTree(depth=10)
    for _ in range(2):
        tree.insert(mul(G1, rand_scalar(rng)))
    tree.insert(issuer.M)
    tree.insert(rec0.M)

    priv = IdentityMerkleTree(depth=10, private=True)
    priv.insert_receiving(m_rec, k_rec, salt_rec)

    return dict(rng=rng, m_iss=m_iss, m_rec=m_rec, M_rec=rec0.M,
                k_rec=k_rec, pk_recv=pk_recv, salt_rec=salt_rec,
                seed_rec=seed_rec, priv=priv,
                issuer=issuer, rec0=rec0, rec1=rec1, tree=tree,
                sigma_R=sigma_R, sigma_s=sigma_s)


def _mint(world, v=1, **kw):
    """Mint an A1 note naming M_rec and keyed to pk_recv."""
    return mint_unilateral_a1(world["M_rec"], world["pk_recv"], v=v,
                              rho=rand_scalar(world["rng"]),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=world["rng"], **kw)


# --------------------------------------------------------------------------- #
# Completeness.
# --------------------------------------------------------------------------- #

def test_mint_names_the_identity_and_keys_to_the_mailbox(world):
    """eRec's PLAINTEXT is the Identity and its KEY is the receiving key.  The
    receiving secret reads it; the identity scalar -- which every counterparty
    holds -- does not."""
    note = _mint(world, v=1000)
    assert eq(elgamal_decrypt(note.eRec, world["k_rec"]), world["M_rec"])
    assert not eq(elgamal_decrypt(note.eRec, world["m_rec"]), world["M_rec"])


def test_the_identity_scalar_no_longer_scans_calldata(world):
    """The defect the receiving key removes: under the collapsed design a
    candidate identity was testable with one scalar multiplication."""
    note = _mint(world, v=1000)
    m_rec = world["m_rec"]
    # The old test, C == m*(G+R), now finds nothing -- for the true identity.
    assert not eq(note.eRec.C, mul(add(G1, note.eRec.R), m_rec))


def test_folded_spend_commits_recipient_identity(world):
    """The gate proves k decrypts eRec to the point committed in P, the account
    credential holds M_rec, and a registered leaf commits the pair."""
    w = deposit_fold_witness(
        m_rec=world["m_rec"], k=world["k_rec"], sk_dep=world["rec0"].sk,
        salt=world["salt_rec"], E_dep=world["rec0"].E,
        note_ct=_mint(world).eRec, tree=world["priv"],
    )
    # For A1 the decrypted point IS the recipient identity.
    assert eq(w.M, world["M_rec"])


def test_any_account_of_the_identity_can_deposit(world):
    """The note named the Identity M_rec, so both of the recipient's accounts
    spend it -- authority is the Identity even though reading is a key."""
    note = _mint(world, v=5)
    for acct in (world["rec0"], world["rec1"]):
        w = deposit_fold_witness(
            m_rec=world["m_rec"], k=world["k_rec"], sk_dep=acct.sk,
            salt=world["salt_rec"], E_dep=acct.E, note_ct=note.eRec,
            tree=world["priv"],
        )
        assert deposit_fold_check(w, pk_dep=acct.pk, E_dep=acct.E,
                                  note_ct=note.eRec, root=world["priv"].root())


def test_unilateral_receipt_names_both(world):
    """The recipient alone names the public issuer and itself; third-party-checkable."""
    issuer, tree = world["issuer"], world["tree"]
    note = _mint(world, v=2500)
    receipt = make_receipt_a1(world["k_rec"], world["M_rec"], note, issuer.M,
                              ISSUER_ADDR, CHAINID, tree, rng=world["rng"])
    res = verify_receipt_a1(receipt, tree.root(), tree)
    assert res.valid, res.reason
    assert eq(res.issuer_M, issuer.M)
    assert eq(res.recipient_M, world["M_rec"])
    assert res.value == 2500


def test_a_rotated_receiving_key_still_spends(world):
    """Rotation is the accumulator's re-association: a fresh key, a fresh salt,
    a leaf that links to nothing, and the note still cashes."""
    k2, pk2 = receiving_key(world["seed_rec"], 1)
    salt2 = derive_salt(world["seed_rec"], KYC, 1)
    world["priv"].insert_receiving(world["m_rec"], k2, salt2)
    note = mint_unilateral_a1(world["M_rec"], pk2, v=7,
                              rho=rand_scalar(world["rng"]),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=world["rng"])
    w = deposit_fold_witness(
        m_rec=world["m_rec"], k=k2, sk_dep=world["rec0"].sk, salt=salt2,
        E_dep=world["rec0"].E, note_ct=note.eRec, tree=world["priv"],
    )
    assert deposit_fold_check(w, pk_dep=world["rec0"].pk, E_dep=world["rec0"].E,
                              note_ct=note.eRec, root=world["priv"].root())
    assert w.leaf != receiving_leaf(world["m_rec"], world["k_rec"],
                                   world["salt_rec"])


# --------------------------------------------------------------------------- #
# Soundness.
# --------------------------------------------------------------------------- #

def test_payload_thief_cannot_spend(world):
    """THE theft the fold exists to refuse.  A thief holding the whole stolen
    payload -- and so the receiving secret in it -- presents its OWN registered
    Identity and account.  Both halves of a split gate would be true.  The fold
    refuses it on the tie: no registered leaf pairs the thief's Identity with
    the key it stole."""
    note = _mint(world, v=1)
    rng = world["rng"]
    m_thief = rand_scalar(rng)
    thief = Account(m_thief, rng)
    seed_t = rand_scalar(rng)
    k_t, _ = receiving_key(seed_t)
    salt_t = derive_salt(seed_t, KYC)
    world["priv"].insert_receiving(m_thief, k_t, salt_t)   # genuinely registered

    with pytest.raises(DepositFoldRefused) as exc:
        deposit_fold_witness(
            m_rec=m_thief, k=world["k_rec"],          # its identity, the stolen key
            sk_dep=thief.sk, salt=salt_t, E_dep=thief.E,
            note_ct=note.eRec, tree=world["priv"],
        )
    assert exc.value.relation == 3

    # Both halves, shown individually true -- which is why the tie must be stated.
    assert eq(thief.E.C, add(thief.M, mul(thief.E.R, thief.sk)))
    assert eq(elgamal_decrypt(note.eRec, world["k_rec"]), world["M_rec"])


def test_spend_without_the_receiving_secret_is_refused(world):
    """Holding the Identity is not holding the mailbox key."""
    note = _mint(world, v=1)
    w = deposit_fold_witness(
        m_rec=world["m_rec"], k=world["k_rec"], sk_dep=world["rec0"].sk,
        salt=world["salt_rec"], E_dep=world["rec0"].E, note_ct=note.eRec,
        tree=world["priv"],
    )
    import dataclasses
    # Substituting the identity scalar for the receiving secret breaks (1).
    bad = dataclasses.replace(w, k=world["m_rec"])
    assert not deposit_fold_check(bad, pk_dep=world["rec0"].pk,
                                  E_dep=world["rec0"].E, note_ct=note.eRec,
                                  root=world["priv"].root())


def test_wrong_account_key_is_refused_on_the_credential_relation(world):
    with pytest.raises(DepositFoldRefused) as exc:
        deposit_fold_witness(
            m_rec=world["m_rec"], k=world["k_rec"],
            sk_dep=world["rec1"].sk,                 # wrong account for rec0's E
            salt=world["salt_rec"], E_dep=world["rec0"].E,
            note_ct=_mint(world).eRec, tree=world["priv"],
        )
    assert exc.value.relation == 2


def test_tampered_witness_is_refused(world):
    note = _mint(world, v=1)
    w = deposit_fold_witness(
        m_rec=world["m_rec"], k=world["k_rec"], sk_dep=world["rec0"].sk,
        salt=world["salt_rec"], E_dep=world["rec0"].E, note_ct=note.eRec,
        tree=world["priv"],
    )
    import dataclasses
    for bad in (dataclasses.replace(w, M=add(w.M, G1)),
                dataclasses.replace(w, salt=(w.salt + 1)),
                dataclasses.replace(w, leaf=w.leaf ^ 1)):
        assert not deposit_fold_check(bad, pk_dep=world["rec0"].pk,
                                      E_dep=world["rec0"].E, note_ct=note.eRec,
                                      root=world["priv"].root())


def test_unregistered_recipient_receipt_invalid(world):
    """A receipt for a recipient whose M_rec is absent from the tree is INVALID --
    the membership gate that the on-chain spend enforces."""
    issuer, tree, rng = world["issuer"], world["tree"], world["rng"]
    m_rec_unreg = rand_scalar(rng)             # never inserted
    M_unreg = mul(G1, m_rec_unreg)
    seed_u = rand_scalar(rng)
    k_u, pk_u = receiving_key(seed_u)
    note = mint_unilateral_a1(M_unreg, pk_u, v=1, rho=rand_scalar(rng),
                              m_issuer=world["m_iss"],
                              sigma_R=world["sigma_R"], sigma_s=world["sigma_s"],
                              rng=rng)
    receipt = make_receipt_a1(k_u, M_unreg, note, issuer.M, ISSUER_ADDR, CHAINID,
                              tree, rng=rng)
    res = verify_receipt_a1(receipt, tree.root(), tree)
    assert not res.valid
    assert "recipient M not a registered identity" in res.reason


def test_receipt_with_a_substituted_receiving_key_is_invalid(world):
    """The receipt's verifiable decryption is under pk_recv, so swapping it
    breaks the proof rather than silently renaming the reader."""
    import dataclasses
    issuer, tree = world["issuer"], world["tree"]
    note = _mint(world, v=3)
    receipt = make_receipt_a1(world["k_rec"], world["M_rec"], note, issuer.M,
                              ISSUER_ADDR, CHAINID, tree, rng=world["rng"])
    _, pk_other = receiving_key(rand_scalar(world["rng"]))
    bad = dataclasses.replace(receipt, pk_recv=pk_other)
    res = verify_receipt_a1(bad, tree.root(), tree)
    assert not res.valid
    assert "verifiable decryption invalid" in res.reason
