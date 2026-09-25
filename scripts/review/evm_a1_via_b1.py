# Run: PYTHONPATH=. python scripts/review/evm_a1_via_b1.py [scratch]
"""Review findings 7 and 8, executed on a real EVM with a real spend SNARK.

Mint the committed A1 e2e note (addressed, public issuer).  Then a third
party Mallory, who is given only the A1 opening, generates a FRESH spend
proof that pays HER address, produces a B1 depositor binding on HER keys,
and calls spendCoupledB1.

This PR makes **flavor** the rejecting check even when membership is
nonempty: the spend SNARK's public flavor is 1 (A1) and spendCoupledB1
supplies 3 (B1).  The empty-membership variant also rejects at that earlier
flavor check, without depending on the separate fail-closed PR.

Honest controls: the same minted A1 note is spent through spendCoupledA1
with the fixture's real membership and note-binding proofs; a separate B1
fixture spends through spendCoupledB1 with matching flavor.
"""
import os
import sys
from pathlib import Path

# Deploy THIS checkout's Notes artifact.  buck_core may be an editable
# install from another tree; repo_root() prefers that tree unless
# ALBERTA_BUCK_REPO is set.
_repo = Path(__file__).resolve().parents[2]
if "ALBERTA_BUCK_REPO" not in os.environ and (_repo / "foundry.toml").exists():
    os.environ["ALBERTA_BUCK_REPO"] = str(_repo)

from web3 import Web3

from alberta_buck.sim.pyrevm_backend import PyrevmAnvil
from alberta_buck.sim.notes_stack import (
    E2EFixture, NotesStack, ACCOUNT_STUB, _g1_tuple, _ct_tuple,
)
from alberta_buck.review.examples import Account, seeded
from alberta_buck.review.integration import spend_prove
from alberta_buck.wallet.b1_binding import b1_bind_prove
from alberta_buck.wallet.bn254 import point_to_words
from alberta_buck.wallet.notes import NoteOpening, note_commitment
from alberta_buck.wallet.poseidon import poseidon

g1 = lambda P: tuple(point_to_words(P))

SCRATCH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/alberta-buck-a1-via-b1")
SCRATCH.mkdir(parents=True, exist_ok=True)

MALLORY_ADDR_INT = 0xBAD
MALLORY = Web3.to_checksum_address(f"0x{MALLORY_ADDR_INT:040x}")


def db_tuple(proof):
    return (proof.e, proof.s_m, proof.s_s, proof.s_r, proof.s_b,
            g1(proof.A2), g1(proof.A4), g1(proof.B1), g1(proof.B2),
            g1(proof.A_p), g1(proof.P_dep))


def prove_mallory_spend(fx):
    w = dict(fx.raw["spend"]["witness"])
    w["recipient"] = MALLORY_ADDR_INT
    assert int(w["flavor"]) == 1
    print("proving spend with A1 flavor, recipient = Mallory ...")
    proved = spend_prove(SCRATCH / "mallory-spend", w)
    return proved["proofBytes"], w


def root_for_opening(witness, opening):
    """Recompute the supplied path root for a different leaf opening."""
    node = note_commitment(opening)
    for sibling, index in zip(witness["pathElements"], witness["pathIndices"]):
        sib = int(sibling)
        node = poseidon([sib, node] if int(index) else [node, sib])
    return node


fx = E2EFixture.load("a1")
assert fx.opening.flavor == 1, "fixture must be A1"
mallory = Account(77777, 88888, 99999)

with PyrevmAnvil(chain_id=1, auto_impersonate=True, timestamp=1_700_000_000) as anvil:
    stack = NotesStack(anvil, fx, rng=seeded(42))
    stack.bind_identities()
    # Pyrevm will not originate a tx from an account that still has code
    # (Anvil impersonation allows it).  Binding already required a stub;
    # clear it so issuer/depositor can fund, approve and mint.
    for a in (fx.issuer.addr, fx.depositor.addr):
        anvil.set_code(stack._addr(a), "0x")
    stack.fund_issuer()
    stack.approve_pool(fx.issuer, 2 * fx.face, "approve issuer->pool")
    # Notes is a public carrying contract: payout to a public Mallory
    # does not need a CP fragment.  Bind Mallory as public.
    anvil.set_code(MALLORY, ACCOUNT_STUB)
    anvil.set_balance(MALLORY, 10**18)
    bind5 = stack._bind5
    stack.chain.send(
        bind5(MALLORY, g1(mallory.pk), (g1(mallory.E.R), g1(mallory.E.C)),
              True, False),
        sender=stack.gov)
    anvil.set_code(MALLORY, "0x")
    assert stack.reg.functions.isVerified(MALLORY).call()
    print("bound Mallory as a public verified identity (no KYC credential)")

    mint = stack.mint()
    print("minted A1 note; face =", fx.face, "tx", mint.txhash)

    proof, w = prove_mallory_spend(fx)
    sp = fx.raw["spend"]["public"]
    root, nf, face = int(sp["noteRoot"]), int(sp["nullifier"]), int(sp["face"])

    # B1 depositor binding on Mallory's keys, naming the fixture issuer
    # (substitution of issuer is a separate assert below).
    b1_proof, e_dep = b1_bind_prove(
        mallory.m, mallory.sk, mallory.E, fx.issuer.pk,
        MALLORY_ADDR_INT, fx.chainid, rng=seeded(43))

    notes_bal_before = stack.buck.functions.balanceOf(stack.notes.address).call()
    mal_bal_before = stack.buck.functions.balanceOf(MALLORY).call()
    dep_addr = stack._addr(fx.depositor.addr)
    dep_bal_before = stack.buck.functions.balanceOf(dep_addr).call()

    def assert_unspent():
        mal_bal = stack.buck.functions.balanceOf(MALLORY).call()
        dep_bal = stack.buck.functions.balanceOf(dep_addr).call()
        notes_bal = stack.buck.functions.balanceOf(stack.notes.address).call()
        spent = stack.notes.functions.nullifiers(nf).call()
        print("   nullifier consumed =", spent)
        print("   Mallory delta BUCK =", mal_bal - mal_bal_before)
        assert not spent, "nullifier must not be consumed on revert"
        assert mal_bal == mal_bal_before, "Mallory must not receive BUCK"
        assert dep_bal == dep_bal_before, "addressee must not be paid by the attack"
        assert notes_bal == notes_bal_before, "pool must not move BUCK on revert"

    # Empty membership still reverts, but flavor is now the earlier check:
    # spendCoupledB1 supplies public flavor=3 against an A1 proof.
    fn_empty = stack.notes.functions.spendCoupledB1(
        proof, root, int(fx.raw["identityRoot"]), nf, face, MALLORY, int(fx.raw["opening"]["cm"]),
        stack._addr(fx.issuer.addr),
        (g1(e_dep.R), g1(e_dep.C)), db_tuple(b1_proof),
        b"",
    )
    try:
        stack._send_from(fn_empty, MALLORY, "Notes.spendCoupledB1(A1 opening, empty mem)",
                         event="SpentCoupledB1", contract=stack.notes)
        raise AssertionError("A1-via-B1 empty membership must revert")
    except RuntimeError as err:
        reason = str(err)
        print("A1-via-B1 empty membership REVERTED")
        print("   reason:", reason)
        print("   flavor in witness = 1 (A1); B1 entry point supplies flavor = 3")
        assert "bad spend proof" in reason, reason
        assert_unspent()

    # Finding 7: nonempty membership (fixture G1-tie bytes) must STILL revert
    # because the spend SNARK's public flavor (1) does not match B1 (3).
    # verifySpend runs before membership, so the rejecting check is flavor.
    mem = bytes.fromhex(fx.raw["membership"]["proofBytes"][2:])
    assert len(mem) > 0, "fixture membership must be nonempty"
    fn_mem = stack.notes.functions.spendCoupledB1(
        proof, root, int(fx.raw["identityRoot"]), nf, face, MALLORY, int(fx.raw["opening"]["cm"]),
        stack._addr(fx.issuer.addr),
        (g1(e_dep.R), g1(e_dep.C)), db_tuple(b1_proof),
        mem,
    )
    try:
        stack._send_from(fn_mem, MALLORY, "Notes.spendCoupledB1(A1 opening, nonempty mem)",
                         event="SpentCoupledB1", contract=stack.notes)
        raise AssertionError(
            "A1-via-B1 nonempty membership must revert on flavor mismatch")
    except RuntimeError as err:
        reason = str(err)
        print("A1-via-B1 nonempty membership REVERTED (finding 7 inverted)")
        print("   reason:", reason)
        print("   flavor in witness = 1 (A1); B1 entry point supplies flavor = 3")
        assert "bad spend proof" in reason, reason
        assert_unspent()

    # Proving the same A1 opening at public flavor=3 is unsatisfiable: the
    # committed flavor word would not match the minted leaf.
    w_b1 = dict(w)
    w_b1["flavor"] = 3
    try:
        spend_prove(SCRATCH / "mallory-spend-as-b1", w_b1)
        raise AssertionError("proving an A1 opening at B1 flavor must fail")
    except RuntimeError as err:
        print("cannot prove A1 opening at public flavor=3 (commitment mismatch)")
        print("   prover:", str(err).splitlines()[-1][:200])

    # Unsupported-predicate semantic test.  Recompute a matching commitment
    # and Merkle root for predicate=1, so witness generation can fail only on
    # the circuit's explicit predicate===0 policy rather than a stale path.
    w_pred = dict(w)
    unsupported = NoteOpening(
        flavor=int(w["flavor"]), v=int(w["v"]), rho=int(w["rho"]),
        id_hash=int(w["idHash"]), predicate=1,
    )
    w_pred["predicate"] = 1
    w_pred["noteRoot"] = root_for_opening(w, unsupported)
    try:
        spend_prove(SCRATCH / "unsupported-predicate", w_pred)
        raise AssertionError("unsupported nonzero predicate must be unsatisfiable")
    except RuntimeError as err:
        print("unsupported predicate=1 is UNSAT with a matching commitment/path")
        print("   prover:", str(err).splitlines()[-1][:200])

    # Honest control: real A1 coupled spend of the SAME note.
    stack.approve_pool(fx.depositor, 0, "approve depositor->pool")
    honest = stack.spend()
    dep_bal_after = stack.buck.functions.balanceOf(dep_addr).call()
    notes_bal_after = stack.buck.functions.balanceOf(stack.notes.address).call()
    assert stack.notes.functions.nullifiers(nf).call(), "honest A1 must consume nullifier"
    assert dep_bal_after - dep_bal_before == face, "honest A1 must pay addressee"
    assert notes_bal_before - notes_bal_after == face, "honest A1 must debit the pool"
    print("HONEST A1 spend succeeded; tx", honest.txhash)
    print("   addressee delta BUCK =", dep_bal_after - dep_bal_before)

    print("\nRESULT: finding 7 inverted -- flavor mismatch rejects A1-via-B1")
    print("   for both empty and nonempty attack calls;")
    print("   honest A1 with matching flavor still pays the addressee.")


# Honest B1 control: matching flavor through spendCoupledB1 still settles.
fx_b1 = E2EFixture.load("b1")
assert fx_b1.opening.flavor == 3, "b1 fixture must be B1"
with PyrevmAnvil(chain_id=1, auto_impersonate=True, timestamp=1_700_000_000) as anvil:
    stack = NotesStack(anvil, fx_b1, rng=seeded(42))
    stack.bind_identities()
    for a in (fx_b1.issuer.addr, fx_b1.depositor.addr):
        anvil.set_code(stack._addr(a), "0x")
    stack.fund_issuer()
    stack.approve_pool(fx_b1.issuer, 2 * fx_b1.face, "approve issuer->pool")
    stack.approve_pool(fx_b1.depositor, 0, "approve depositor->pool")
    stack.mint()

    d = fx_b1.raw
    dep = stack._addr(fx_b1.depositor.addr)
    sp = d["spend"]["public"]
    proof = bytes.fromhex(d["spend"]["proofBytes"][2:])
    root, nf = int(sp["noteRoot"]), int(sp["nullifier"])
    face, rec = int(sp["face"]), stack._addr(int(sp["recipient"], 16))
    db = d["sigma"]["db"]
    b1p = (int(db["e"]), int(db["s_m"]), int(db["s_s"]), int(db["s_r"]),
           int(db["s_b"]),
           _g1_tuple(db["A2"]), _g1_tuple(db["A4"]), _g1_tuple(db["B1"]),
           _g1_tuple(db["B2"]), _g1_tuple(db["A_p"]), _g1_tuple(db["P_dep"]))
    e_dep = d["sigma"]["eDepForIss"]
    # Issuer substitution is a semantic test: create a fresh, valid depositor
    # binding against a different registered issuer while retaining the honest
    # B1 opening and proof.  The exact commitment's mint attribution rejects
    # composition with that otherwise-valid sigma.
    fake_iss = Web3.to_checksum_address("0x00000000000000000000000000000000000015e1")
    fake_acct = Account(13579, 24680, 11111)
    anvil.set_code(fake_iss, ACCOUNT_STUB)
    stack.chain.send(
        stack._bind5(
            fake_iss, g1(fake_acct.pk), (g1(fake_acct.E.R), g1(fake_acct.E.C)),
            True, False,
        ),
        sender=stack.gov,
    )
    sub_proof, sub_e = b1_bind_prove(
        fx_b1.depositor.m, fx_b1.depositor.sk, fx_b1.depositor.E,
        fake_acct.pk, fx_b1.depositor.addr, fx_b1.chainid, rng=seeded(44),
    )
    assert stack.reg.functions.verifyDepositorBinding(
        rec, fake_iss, (g1(sub_e.R), g1(sub_e.C)), db_tuple(sub_proof)
    ).call(), "substitution control sigma must be independently valid"
    mem = bytes.fromhex(d["membership"]["proofBytes"][2:])
    fn_sub = stack.notes.functions.spendCoupledB1(
        proof, root, int(d["identityRoot"]), nf, face, rec, int(d["opening"]["cm"]), fake_iss,
        (g1(sub_e.R), g1(sub_e.C)), db_tuple(sub_proof), mem,
    )
    try:
        stack._send_from(fn_sub, dep, "Notes.spendCoupledB1(substitute issuer)",
                         event="SpentCoupledB1", contract=stack.notes)
        raise AssertionError("substitute issuer must not redeem a B1 note")
    except RuntimeError as err:
        reason = str(err)
        print("SUBSTITUTE issuer spend REVERTED")
        print("   reason:", reason)
        assert "wrong B1 issuer" in reason, reason
        assert not stack.notes.functions.nullifiers(nf).call()

    steps = {"spend": stack.spend()}
    rec_bal = stack.buck.functions.balanceOf(rec).call()
    assert stack.notes.functions.nullifiers(nf).call(), "honest B1 must consume nullifier"
    assert rec_bal == face, "honest B1 must pay the depositor"
    print("HONEST B1 spend succeeded; tx", steps["spend"].txhash)
    print("   depositor BUCK =", rec_bal)
