"""Identity-targeted unilateral A2 Note -- end-to-end demonstration.

Exercises the full flow:

  mint (issuer)  ->  off-chain delivery + verify  ->  folded deposit gate
                 ->  unilateral recipient receipt naming BOTH identities

An A2 note is keyed to the recipient's registered *receiving key* rather than
to its Identity point, because an identity scalar is a read capability the
design discloses to every counterparty and so cannot also be a decryption key.
Authority stays with the Identity -- every account bound to m_rec spends -- but
reading is a separate secret, which is why the spend is the folded gate of
doc/review/notes-receiving-key.org section 3.3a rather than the legacy sigma.

Properties covered: collusion-resistance (a bogus eIss decrypts to a non-member
and the receipt is INVALID), any-account flexibility, soundness of the folded
gate including the payload theft a split gate admits, and privacy (no identity
appears in the public proof, and no certified scalar tests the calldata).
"""

import dataclasses

import pytest

from alberta_buck.registry.tree import IdentityMerkleTree, receiving_leaf
from alberta_buck.wallet.bn254 import G1, ORDER, mul, add, neg, eq, rand_scalar
from alberta_buck.wallet.deposit_fold import (
    DepositFoldRefused, deposit_fold_check, deposit_fold_witness,
)
from alberta_buck.wallet.elgamal import elgamal_encrypt, elgamal_decrypt
from alberta_buck.wallet.issuer_reenc import issuer_reenc_verify
from alberta_buck.wallet.recvkey import receiving_key
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.wallet.unilateral_a2 import (
    IdentityTree, identity_leaf,
    mint_unilateral_a2,
    make_receipt, verify_receipt,
)

CHAINID = 1
ISSUER_ADDR = 0xA11CE
DEPOSIT_ADDR = 0xB0B
KYC = "kyc:ca-ab-2026"


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
        self.r_E = rand_scalar(rng)
        self.E = elgamal_encrypt(self.M, self.pk, self.r_E)   # registered credential


@pytest.fixture
def world():
    """An issuer identity, a recipient identity with two accounts and one
    receiving key, and the two trees the flow needs."""
    rng = _seeded_rng()
    m_iss = rand_scalar(rng)
    m_rec = rand_scalar(rng)
    issuer = Account(m_iss, rng)                  # issuer's registered account
    rec0 = Account(m_rec, rng)                    # recipient account #0
    rec1 = Account(m_rec, rng)                    # recipient account #1 (same identity)

    # One mailbox per IDENTITY, not per account: reading is one secret even
    # though authority is spread across every account bound to m_rec.
    seed_rec = rand_scalar(rng)
    k_rec, pk_recv = receiving_key(seed_rec)
    salt_rec = derive_salt(seed_rec, KYC)

    # `tree` holds identity POINTS and answers the receipt's membership
    # questions.  `priv` is the private identity-registry subtree whose leaves
    # commit (Identity, receiving key) pairs, and answers the spend's tie.
    tree = IdentityTree(depth=10)
    for _ in range(3):
        tree.insert(mul(G1, rand_scalar(rng)))
    tree.insert(issuer.M)
    tree.insert(rec0.M)                           # one leaf per *identity*
    for _ in range(2):
        tree.insert(mul(G1, rand_scalar(rng)))

    priv = IdentityMerkleTree(depth=10, private=True)
    priv.insert_receiving(m_rec, k_rec, salt_rec)

    return dict(rng=rng, m_iss=m_iss, m_rec=m_rec, M_rec=rec0.M,
                k_rec=k_rec, pk_recv=pk_recv, salt_rec=salt_rec,
                seed_rec=seed_rec, priv=priv,
                issuer=issuer, rec0=rec0, rec1=rec1, tree=tree)


def _mint(world, v=1000, **kw):
    """Mint an A2 note keyed to the recipient's receiving key."""
    iss = world["issuer"]
    return mint_unilateral_a2(iss.sk, iss.E, world["pk_recv"], v=v,
                              rho=rand_scalar(world["rng"]),
                              issuer=ISSUER_ADDR, chainid=CHAINID,
                              rng=world["rng"], **kw)


def _witness(world, minted, acct=None, **kw):
    acct = acct or world["rec0"]
    args = dict(m_rec=world["m_rec"], k=world["k_rec"], sk_dep=acct.sk,
                salt=world["salt_rec"], E_dep=acct.E, note_ct=minted.eIss,
                tree=world["priv"])
    args.update(kw)
    return deposit_fold_witness(**args)


# --------------------------------------------------------------------------- #
# Completeness: the happy path end to end.
# --------------------------------------------------------------------------- #

def test_mint_binding_verifies(world):
    """The mint anti-framing binding (issuer_reenc with pk_rec := pk_recv)
    verifies, and the issuer encrypted its *own* registered identity."""
    iss = world["issuer"]
    minted = _mint(world)
    assert issuer_reenc_verify(iss.pk, iss.E, minted.eIss, minted.binding,
                               ISSUER_ADDR, CHAINID)
    assert eq(minted.M_I, iss.M)


def test_delivery_recipient_decrypts_issuer(world):
    """At delivery the recipient decrypts eIss under its RECEIVING secret and
    recovers the issuer's true identity, a registered member.  The identity
    scalar does not open it."""
    iss, tree = world["issuer"], world["tree"]
    minted = _mint(world)
    M_I = elgamal_decrypt(minted.eIss, world["k_rec"])
    assert eq(M_I, iss.M)
    assert tree.contains(M_I)
    assert not eq(elgamal_decrypt(minted.eIss, world["m_rec"]), iss.M)


def test_no_certified_scalar_scans_the_calldata(world):
    """The harvest the receiving key closes: a party holding every certified
    identity scalar decides nothing about who a note is addressed to."""
    minted = _mint(world)
    certified = [world["m_rec"], world["m_iss"]] + [
        rand_scalar(world["rng"]) for _ in range(8)]
    hits = [m for m in certified
            if eq(minted.eIss.C, mul(add(G1, minted.eIss.R), m))]
    assert hits == []


def test_folded_gate_verifies(world):
    """The depositor proves eligibility with one witness carrying all four
    relations, and the check accepts against the public inputs."""
    minted = _mint(world)
    w = _witness(world, minted)
    assert eq(w.M, world["issuer"].M)
    assert deposit_fold_check(w, pk_dep=world["rec0"].pk, E_dep=world["rec0"].E,
                              note_ct=minted.eIss, root=world["priv"].root())


def test_unilateral_receipt_names_both(world):
    """The recipient *alone* produces a receipt naming both plaintext identities,
    third-party-checkable with no secret."""
    iss, tree = world["issuer"], world["tree"]
    minted = _mint(world, v=2500)
    receipt = make_receipt(world["k_rec"], world["M_rec"], minted,
                           ISSUER_ADDR, CHAINID, tree, rng=world["rng"])

    res = verify_receipt(receipt, iss.pk, iss.E, tree.root(), tree)
    assert res.valid, res.reason
    assert eq(res.issuer_M, iss.M)               # issuer named
    assert eq(res.recipient_M, world["M_rec"])   # recipient named
    assert res.value == 2500


# --------------------------------------------------------------------------- #
# Any-account flexibility: authority is the Identity, reading is a key.
# --------------------------------------------------------------------------- #

def test_any_account_can_deposit(world):
    """The issuer addressed the identity, not an account: both of the
    recipient's accounts spend the same note, under one mailbox key."""
    minted = _mint(world, v=10)
    for acct in (world["rec0"], world["rec1"]):
        w = _witness(world, minted, acct=acct)
        assert deposit_fold_check(w, pk_dep=acct.pk, E_dep=acct.E,
                                  note_ct=minted.eIss,
                                  root=world["priv"].root()), \
            "every account bound to M_rec deposits"


def test_a_lost_account_key_does_not_strand_the_note(world):
    """Recovery: register a fresh account on the same Identity and deposit.
    Authority never lived in an account key."""
    minted = _mint(world, v=42)
    fresh = Account(world["m_rec"], world["rng"])      # a new account, same Identity
    w = _witness(world, minted, acct=fresh)
    assert deposit_fold_check(w, pk_dep=fresh.pk, E_dep=fresh.E,
                              note_ct=minted.eIss, root=world["priv"].root())


def test_a_rotated_receiving_key_still_spends(world):
    """Rotation is the accumulator's re-association, and the new leaf links to
    nothing."""
    k2, pk2 = receiving_key(world["seed_rec"], 1)
    salt2 = derive_salt(world["seed_rec"], KYC, 1)
    world["priv"].insert_receiving(world["m_rec"], k2, salt2)
    iss = world["issuer"]
    minted = mint_unilateral_a2(iss.sk, iss.E, pk2, v=7,
                                rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID,
                                rng=world["rng"])
    w = _witness(world, minted, k=k2, salt=salt2)
    assert deposit_fold_check(w, pk_dep=world["rec0"].pk, E_dep=world["rec0"].E,
                              note_ct=minted.eIss, root=world["priv"].root())
    assert w.leaf != receiving_leaf(world["m_rec"], world["k_rec"],
                                   world["salt_rec"])


# --------------------------------------------------------------------------- #
# Collusion-resistance: a bogus eIss is un-nameable AND un-spendable.
# --------------------------------------------------------------------------- #

def _split_mint(world, M_puppet, salt_puppet, rng):
    """The A2 key split (doc/review/notes-receiving-key.org, section 4.6), minted by hand: ONE
    eIss that the binding opens to the minter's own Identity under a key of its choosing, and that
    the recipient's k opens to ``M_puppet``.  mint_unilateral_a2 cannot produce it -- it keys both
    to one point -- which is the point."""
    from alberta_buck.wallet.issuer_reenc import issuer_reenc_prove
    from alberta_buck.wallet.notes import FLAVOR_A2, NoteOpening, note_commitment
    from alberta_buck.wallet.unilateral_a2 import MintedA2, a2_id_hash
    iss                         = world["issuer"]
    r_prime                     = rand_scalar(rng)
    pk_mint                     = add(world["pk_recv"],
                                      mul(add(M_puppet, neg(iss.M)), pow(r_prime, -1, ORDER)))
    eIss                        = elgamal_encrypt(iss.M, pk_mint, r_prime)
    gamma                       = rand_scalar(rng)
    binding                     = issuer_reenc_prove(iss.sk, r_prime, pk_mint, iss.E, eIss,
                                                     ISSUER_ADDR, CHAINID, gamma=gamma, rng=rng)
    r_note                      = rand_scalar(rng)
    eNote                       = elgamal_encrypt(mul(G1, 1000), world["pk_recv"], r_note)
    idh                         = a2_id_hash(eNote, eIss, binding.T)
    opening                     = NoteOpening(FLAVOR_A2, 1000, rand_scalar(rng), idh, 0)
    return MintedA2(eNote=eNote, eIss=eIss, M_I=iss.M, idHash=idh, cm=note_commitment(opening),
                    opening=opening, binding=binding, r_prime=r_prime, r_note=r_note,
                    gamma=gamma, salt_iss=salt_puppet)


def test_split_key_sock_puppet_is_refused(world):
    """The A2 key split.  The minter's binding is true of its own registered Identity; the
    recipient's key opens the same eIss to a REGISTERED sock puppet, so relation (5) holds too.
    What refuses it is the committed T: it opens under the minter's chosen key, not the
    recipient's -- at delivery, at the spend gate, and in the receipt."""
    from alberta_buck.wallet.delivery import DeliveryRefused, deliver_a2, open_a2
    from alberta_buck.wallet.deposit_fold import deposit_fold_a2_witness
    from alberta_buck.registry.tree import identity_leaf_salted
    rng, iss                    = world["rng"], world["issuer"]
    M_puppet                    = mul(G1, rand_scalar(rng))
    salt_puppet                 = derive_salt(rand_scalar(rng), KYC, 1)
    world["priv"].insert_identity_salted(M_puppet, salt_puppet)
    world["tree"].insert(M_puppet)
    minted                      = _split_mint(world, M_puppet, salt_puppet, rng)

    # The split is real: the binding verifies, and k opens eIss to the registered puppet.
    assert issuer_reenc_verify(iss.pk, iss.E, minted.eIss, minted.binding, ISSUER_ADDR, CHAINID)
    assert eq(elgamal_decrypt(minted.eIss, world["k_rec"]), M_puppet)
    assert not eq(M_puppet, iss.M)

    # (1) The recipient's wallet refuses the delivery.
    with pytest.raises(DeliveryRefused, match="T does not open"):
        open_a2(deliver_a2(minted, world["pk_recv"]), world["k_rec"])

    # (2) The spend gate refuses it: relation (5) holds, the key tie does not.
    acct                        = world["rec0"]
    t                           = (minted.r_prime + rand_scalar(rng)) % ORDER
    eEnc                        = elgamal_encrypt(M_puppet, world["pk_recv"], t)
    w                           = deposit_fold_witness(
        m_rec=world["m_rec"], k=world["k_rec"], sk_dep=acct.sk, salt=world["salt_rec"],
        E_dep=acct.E, note_ct=eEnc, tree=world["priv"])
    priv                        = world["priv"]
    iss_path                    = priv.path(priv.leaves.index(identity_leaf_salted(M_puppet, salt_puppet)))
    with pytest.raises(AssertionError, match="keyed to another mailbox"):
        deposit_fold_a2_witness(
            witness=w, rho=minted.opening.rho, id_hash=minted.idHash, e_note=minted.eNote,
            e_iss=minted.eIss, r_prime=minted.r_prime, t=t, r_E=acct.r_E, e_dep=acct.E,
            pk_dep=acct.pk, e_enc=eEnc, salt_iss=salt_puppet, iss_path=iss_path,
            T=minted.binding.T, gamma=minted.gamma, identity_root=priv.root())

    # (3) The receipt refuses to name the puppet.
    receipt                     = make_receipt(world["k_rec"], world["M_rec"], minted, ISSUER_ADDR,
                                               CHAINID, world["tree"], rng=rng)
    res                         = verify_receipt(receipt, iss.pk, iss.E, world["tree"].root(),
                                                 world["tree"])
    assert not res.valid
    assert "not the one decrypted" in res.reason


def test_collusion_bogus_eiss_unnameable(world):
    """A colluding issuer keys eIss to a throwaway point (not pk_recv).  The
    mint binding still passes -- it only forces eIss over the issuer's own M --
    but the recipient's decryption lands on a NON-member, and on a point the
    binding's T does not open to, so the receipt is INVALID and the spend's
    membership gate and key tie would each reject it."""
    iss, tree = world["issuer"], world["tree"]
    rng = world["rng"]
    pk_bogus = mul(G1, rand_scalar(rng))         # a key the recipient does not hold

    r_prime = rand_scalar(rng)
    eIss_bogus = elgamal_encrypt(iss.M, pk_bogus, r_prime)
    from alberta_buck.wallet.issuer_reenc import issuer_reenc_prove
    gamma = rand_scalar(rng)
    binding = issuer_reenc_prove(iss.sk, r_prime, pk_bogus, iss.E, eIss_bogus,
                                 ISSUER_ADDR, CHAINID, gamma=gamma, rng=rng)
    assert issuer_reenc_verify(iss.pk, iss.E, eIss_bogus, binding, ISSUER_ADDR, CHAINID)

    # The recipient's decryption is garbage, not a registered identity.
    M_I_bogus = elgamal_decrypt(eIss_bogus, world["k_rec"])
    assert not eq(M_I_bogus, iss.M)
    assert not tree.contains(M_I_bogus), "bogus issuer identity must not be a member"

    from alberta_buck.wallet.unilateral_a2 import (
        MintedA2, a2_id_hash, make_receipt as mk, verify_receipt as vr,
    )
    from alberta_buck.wallet.notes import NoteOpening, note_commitment, FLAVOR_A2
    eNote_bogus = elgamal_encrypt(mul(G1, 1000), pk_bogus, rand_scalar(rng))
    idh = a2_id_hash(eNote_bogus, eIss_bogus, binding.T)
    opening = NoteOpening(FLAVOR_A2, 1000, rand_scalar(rng), idh, 0)
    minted_bogus = MintedA2(eNote=eNote_bogus, eIss=eIss_bogus, M_I=iss.M, idHash=idh,
                            cm=note_commitment(opening), opening=opening,
                            binding=binding, r_prime=r_prime, r_note=rand_scalar(rng),
                            gamma=gamma)
    receipt = mk(world["k_rec"], world["M_rec"], minted_bogus, ISSUER_ADDR,
                 CHAINID, tree, rng=rng)
    res = vr(receipt, iss.pk, iss.E, tree.root(), tree)
    assert not res.valid
    assert "not the one decrypted" in res.reason


# --------------------------------------------------------------------------- #
# Soundness of the folded gate.
# --------------------------------------------------------------------------- #

def test_payload_thief_cannot_spend(world):
    """THE theft the fold exists to refuse.  The thief holds the stolen payload
    -- and so the receiving secret inside it -- and presents its OWN registered
    Identity and account.  Both halves of a split gate would be true; neither
    joins them.  The fold refuses on the tie."""
    minted = _mint(world, v=1)
    rng = world["rng"]
    thief = Account(rand_scalar(rng), rng)
    seed_t = rand_scalar(rng)
    k_t, _ = receiving_key(seed_t)
    salt_t = derive_salt(seed_t, KYC)
    world["priv"].insert_receiving(thief.m, k_t, salt_t)   # genuinely registered

    with pytest.raises(DepositFoldRefused) as exc:
        deposit_fold_witness(
            m_rec=thief.m, k=world["k_rec"],       # its identity, the stolen key
            sk_dep=thief.sk, salt=salt_t, E_dep=thief.E,
            note_ct=minted.eIss, tree=world["priv"],
        )
    assert exc.value.relation == 3

    # Both halves, individually true -- which is why the tie must be stated.
    assert eq(thief.E.C, add(thief.M, mul(thief.E.R, thief.sk)))
    assert eq(elgamal_decrypt(minted.eIss, world["k_rec"]), world["issuer"].M)


def test_an_unregistered_receiving_key_cannot_spend(world):
    """A holder claiming a mailbox it never registered has no witness, even for
    its own Identity."""
    k2, pk2 = receiving_key(world["seed_rec"], 1)      # never admitted
    salt2 = derive_salt(world["seed_rec"], KYC, 1)
    iss = world["issuer"]
    minted = mint_unilateral_a2(iss.sk, iss.E, pk2, v=1,
                                rho=rand_scalar(world["rng"]),
                                issuer=ISSUER_ADDR, chainid=CHAINID,
                                rng=world["rng"])
    with pytest.raises(DepositFoldRefused) as exc:
        _witness(world, minted, k=k2, salt=salt2)
    assert exc.value.relation == 3


def test_wrong_account_is_refused_on_the_credential_relation(world):
    minted = _mint(world, v=1)
    with pytest.raises(DepositFoldRefused) as exc:
        _witness(world, minted, sk_dep=world["rec1"].sk)   # rec1's key, rec0's E
    assert exc.value.relation == 2


def test_tampered_witness_rejected(world):
    minted = _mint(world, v=1)
    w = _witness(world, minted)
    for bad in (dataclasses.replace(w, M=add(w.M, G1)),
                dataclasses.replace(w, k=(w.k + 1) % ORDER),
                dataclasses.replace(w, salt=w.salt + 1),
                dataclasses.replace(w, leaf=w.leaf ^ 1)):
        assert not deposit_fold_check(bad, pk_dep=world["rec0"].pk,
                                      E_dep=world["rec0"].E,
                                      note_ct=minted.eIss,
                                      root=world["priv"].root())


def test_stale_root_rejected(world):
    minted = _mint(world, v=1)
    w = _witness(world, minted)
    assert not deposit_fold_check(w, pk_dep=world["rec0"].pk,
                                  E_dep=world["rec0"].E, note_ct=minted.eIss,
                                  root=world["priv"].root() ^ 1)


def test_unregistered_account_rejected_by_the_check(world):
    """Relation (2) alone would hold for an account nobody registered; pinning
    sk_dep to the REGISTERED pk_dep rules that out."""
    minted = _mint(world, v=1)
    w = _witness(world, minted)
    assert not deposit_fold_check(w, pk_dep=mul(G1, world["rec0"].sk + 1),
                                  E_dep=world["rec0"].E, note_ct=minted.eIss,
                                  root=world["priv"].root())


# --------------------------------------------------------------------------- #
# Privacy: no identity appears in the public gate; mints unlinkable.
# --------------------------------------------------------------------------- #

def test_two_mints_unlinkable(world):
    """Two mints from the same issuer to the same recipient yield distinct eIss
    (fresh r'), so Mallory cannot group them."""
    a = _mint(world, v=1)
    b = _mint(world, v=1)
    assert not eq(a.eIss.R, b.eIss.R)
    assert not eq(a.eIss.C, b.eIss.C)
