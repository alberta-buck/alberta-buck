"""The privacy paper's world: every value traces to its source, and the story runs on chain.

1. Offline: the fixture's values derive from what the paper says they derive from -- M from
   the core record, each account's envelope from its keys, the identity root from the
   citizens' wallet secrets, each note's commitment and nullifier from its opening -- and
   replaying the recorded draws rebuilds the exact notes the Groth16 proofs were made over.
2. Live EVM (anvil): Alberta's issuer certifies the cast; the accounts register with real
   proofs; Bob pays Carol directly, then by B1, A1 and A2; every spend pays out, and the
   observer decodes what an outsider sees.
"""

from __future__ import annotations

import random

import pytest

from alberta_buck.registry.merkle_service import rooted_registry
from alberta_buck.registry.tree import identity_leaf_salted, mailbox_leaf, receiving_leaf
from alberta_buck.sim.cast import ASPEN, BOB, CAROL
from alberta_buck.sim.privacy_world import PrivacyWorld
from alberta_buck.wallet.bn254 import G1, eq, mul, point_to_words, rand_scalar
from alberta_buck.wallet.elgamal import elgamal_encrypt
from alberta_buck.wallet.envelope import deserialize_core, serialize_core
from alberta_buck.wallet.identity import canonical_identity_data, identity_scalar
from alberta_buck.wallet.notes import note_commitment, nullifier_b
from alberta_buck.wallet.recvkey import receiving_key, verify_receiving_binding
from alberta_buck.wallet.salt import derive_salt
from alberta_buck.wallet.verify_receipt import verify_receipt


@pytest.fixture(scope="module")
def world():
    return PrivacyWorld.load()


def _rng(seed=0x9A1):
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


def test_identities_come_from_the_cast(world):
    for name, fields in (("bob", BOB), ("carol", CAROL), ("aspen", ASPEN)):
        p = world.people[name]
        assert p.identity == canonical_identity_data(fields)
        assert p.m == identity_scalar(p.identity) and eq(p.M, mul(G1, p.m))


def test_account_envelopes_come_from_their_keys(world):
    for a in world.accounts.values():
        assert eq(a.pk, mul(G1, a.sk))
        E = elgamal_encrypt(a.M, a.pk, a.r)
        assert eq(E.R, a.E.R) and eq(E.C, a.E.C), a.label
    assert world.accounts["carol"].address != world.accounts["carolSavings"].address


def test_identity_root_rebuilds_from_wallet_secrets(world):
    """The posted root is nothing but the citizens' own associations, each with its own salt."""
    tree = rooted_registry("registry:kyc")
    for name in ("bob", "carol"):
        p                       = world.people[name]
        k, pk                   = receiving_key(p.seed)
        assert k == p.k and eq(pk, p.pk_recv)
        for purpose, counter in (("receiving", 0), ("mailbox", 1), ("naming", 2)):
            assert p.salts[purpose] == derive_salt(p.seed, world.kyc, counter)
    bob, carol = world.people["bob"], world.people["carol"]
    for leaf in (receiving_leaf(bob.m, bob.k, bob.salts["receiving"]),
                 identity_leaf_salted(bob.M, bob.salts["naming"]),
                 receiving_leaf(carol.m, carol.k, carol.salts["receiving"]),
                 mailbox_leaf(carol.M, carol.pk_recv, carol.salts["mailbox"]),
                 identity_leaf_salted(carol.M, carol.salts["naming"])):
        tree.insert_leaf(leaf)
    assert tree.root() == world.identity_root
    assert verify_receiving_binding(carol.M, world.mailbox_binding(), world.identity_root)


def test_note_commitments_and_nullifiers(world):
    for note in world.notes.values():
        assert [note_commitment(o) for o in note.batch] == note.cms
        assert note_commitment(note.opening) == note.cm
        assert nullifier_b(note.opening.rho, note.opening.id_hash) == note.nullifier
        assert note.face == 100 * world.unit
    b1, a1, a2 = (world.notes[f] for f in ("b1", "a1", "a2"))
    assert (b1.raw["leafIndex"], a1.raw["leafIndex"], a2.raw["leafIndex"]) == (2, 6, 8)


def test_replayed_draws_rebuild_the_proven_notes(world):
    """The paper re-runs each wallet operation with its recorded draws and gets the same note."""
    from alberta_buck.wallet.b1_binding import b1_bind_prove
    from alberta_buck.wallet.unilateral_a1 import mint_unilateral_a1
    from alberta_buck.wallet.unilateral_a2 import mint_unilateral_a2
    from alberta_buck.wallet.bn254 import words_to_point
    A, P                        = world.accounts, world.people
    a2                          = world.notes["a2"]
    m2                          = mint_unilateral_a2(A["bob"].sk, A["bob"].E, P["carol"].pk_recv, v=a2.face,
                                                     rho=a2.opening.rho, issuer=A["bob"].addr, chainid=world.chainid,
                                                     salt_iss=P["bob"].salts["naming"], rng=world.replay("a2", "mint"))
    assert m2.cm == a2.cm
    a1                          = world.notes["a1"]
    s                           = a1.raw["sigma"]
    sigma_R                     = words_to_point(int(s["sigma_R"]["x"]), int(s["sigma_R"]["y"]))
    m1                          = mint_unilateral_a1(P["carol"].M, P["carol"].pk_recv, v=a1.face, rho=a1.opening.rho,
                                                     m_issuer=P["aspen"].m, sigma_R=sigma_R, sigma_s=int(s["sigma_s"]),
                                                     rng=world.replay("a1", "mint"))
    assert m1.cm == a1.cm
    b1                          = world.notes["b1"]
    rng                         = world.replay("b1", "depositorBinding")
    b                           = rand_scalar(rng)
    _binding, eDep              = b1_bind_prove(P["carol"].m, A["carol"].sk, A["carol"].E, A["aspen"].pk,
                                                account=A["carol"].addr, chainid=world.chainid, b=b, rng=rng)
    want = b1.raw["depositor"]["eDepForIss"]["C"]
    assert point_to_words(eDep.C) == (int(want["x"]), int(want["y"]))


@pytest.mark.parametrize("flavor", ["b1", "a1", "a2"])
@pytest.mark.parametrize("role", ["recipient", "issuer"])
def test_receipts_verify(world, flavor, role):
    fx                          = world.fixture(flavor)
    anchors                     = {"mint": {"txhash": "0x" + "11" * 32, "block": 100},
                                   "spend": {"txhash": "0x" + "22" * 32, "block": 101, "logindex": 3,
                                             "timestamp": 1780000000}}
    contracts                   = {"registry": "0x" + "aa" * 20, "buck": "0x" + "bb" * 20, "notes": "0x" + "cc" * 20}
    core                        = fx.build_receipt(role, contracts, rng=_rng(), **anchors)
    res                         = verify_receipt(deserialize_core(serialize_core(core)))
    assert res.ok and res.reason == "VALID", f"{flavor}/{role}: {res.reason}"
    assert res.value == 100 * world.unit


# ---- live EVM ------------------------------------------------------------------------------------

def _have_forge_artifacts() -> bool:
    try:
        from alberta_buck.sim.chain import repo_root
        return (repo_root() / "out" / "MintBatchN4Groth16Verifier.sol").exists()
    except FileNotFoundError:
        return False


@pytest.mark.skipif(not _have_forge_artifacts(), reason="needs forge artifacts (make nix-build)")
def test_the_story_runs_on_chain(world):
    from alberta_buck.sim.anvil import Anvil
    from alberta_buck.sim.privacy_world import PrivacyChain
    from alberta_buck.wallet.issuer import Issuer

    rng                         = _rng(0x5701)
    A, U                        = world.accounts, world.unit
    with Anvil(chain_id=1, auto_impersonate=True) as anvil:
        alberta                 = Issuer.setup("alberta-identity", 0xA1BE27A0000000000000000000000000000000A1, rng=rng)
        chain                   = PrivacyChain(anvil, world, alberta, rng=rng)
        creds                   = {who: alberta.issue(f, applicant_addr=0, rng=rng)
                                   for who, f in (("bob", BOB), ("carol", CAROL), ("aspen", ASPEN))}
        for label in ("bob", "carol", "carolSavings"):
            chain.register(chain.registration_package(A[label], creds[A[label].owner], rng))
        chain.bind_public(A["aspen"])
        chain.post_identity_root(world.identity_root)
        chain.fund(A["bob"], 1000 * U)
        chain.fund(A["aspen"], 2000 * U)

        chain.approve(chain.envelope(A["bob"], A["carol"].address, A["carol"].pk, rng))
        chain.approve(chain.envelope(A["carol"], A["bob"].address, A["bob"].pk, rng))
        chain.transfer(A["bob"], A["carol"].address, 100 * U)

        chain.approve(chain.envelope(A["bob"], A["aspen"].address, A["aspen"].pk, rng))
        chain.transfer(A["bob"], A["aspen"].address, 200 * U)
        chain.allow(A["aspen"], chain.notes.address, 10**15)
        chain.approve(chain.envelope(A["carol"], chain.notes.address, chain.pool_pk, rng))
        chain.approve(chain.envelope(A["carolSavings"], chain.notes.address, chain.pool_pk, rng))
        chain.approve(chain.envelope(A["bob"], chain.notes.address, chain.pool_pk, rng), 100 * U)
        spends = {}
        for flavor in ("b1", "a1", "a2"):
            chain.mint_note(world.notes[flavor])
            spends[flavor] = chain.spend_note(world.notes[flavor])
            assert chain.notes.functions.nullifiers(world.notes[flavor].nullifier).call()

        assert chain.balance(A["carol"].address) >= 299 * U
        assert chain.balance(A["carolSavings"].address) >= 99 * U
        seen = chain.observe(spends["b1"])
        assert seen.function == "spendCoupledB1"
        assert "SpentCoupledB1" in [e for e, _ in seen.events]
        assert any("Aspen Mutual" in v for _, v in seen.args)
