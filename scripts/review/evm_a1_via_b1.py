# Run: PYTHONPATH=. python scripts/review/evm_a1_via_b1.py [scratch]
"""Review findings 7 and 8, executed on a real EVM with a real spend SNARK.

Mint the committed A1 e2e note (addressed, public issuer).  Then a third
party Mallory, who is given only the A1 opening, generates a FRESH spend
proof that pays HER address, produces a B1 depositor binding on HER keys,
and calls spendCoupledB1 with an EMPTY membership proof.

P0-0 inverted this assertion: empty membership now fails closed even
with the real G1-tie adapter wired, so the A1-via-B1 call MUST revert.
The attack is unchanged (same opening, same empty proof, same B1 entry
point); only the expected outcome flipped.  Finding 7 (flavor-agnostic
spend circuit) is still a circuit gap, but empty membership no longer
lets it move BUCK.

Honest controls: the same minted A1 note is then spent through
spendCoupledA1 with the fixture's real membership and note-binding
proofs; a separate B1 fixture then completes its lifecycle with a real
membership proof.  Both pay their intended recipients.
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
from alberta_buck.sim.notes_stack import E2EFixture, NotesStack, ACCOUNT_STUB
from alberta_buck.review.examples import Account, seeded
from alberta_buck.review.integration import spend_prove
from alberta_buck.wallet.b1_binding import b1_bind_prove
from alberta_buck.wallet.bn254 import point_to_words

g1 = lambda P: tuple(point_to_words(P))

SCRATCH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/alberta-buck-a1-via-b1")
SCRATCH.mkdir(parents=True, exist_ok=True)

MALLORY_ADDR_INT = 0xBAD
MALLORY = Web3.to_checksum_address(f"0x{MALLORY_ADDR_INT:040x}")


def db_tuple(proof):
    return (proof.e, proof.s_m, proof.s_s, proof.s_r, proof.s_b,
            g1(proof.A2), g1(proof.A4), g1(proof.B1), g1(proof.B2),
            g1(proof.A_p), g1(proof.P_dep))


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

    # Fresh spend SNARK: same A1 opening, recipient redirected to Mallory.
    w = dict(fx.raw["spend"]["witness"])
    w["recipient"] = MALLORY_ADDR_INT
    # Keep flavor = 1 (A1).  Public inputs still omit flavor.
    assert int(w["flavor"]) == 1
    print("proving spend with A1 flavor, recipient = Mallory ...")
    proved = spend_prove(SCRATCH / "mallory-spend", w)
    proof = proved["proofBytes"]
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

    fn = stack.notes.functions.spendCoupledB1(
        proof, root, nf, face, MALLORY, stack._addr(fx.issuer.addr),
        (g1(e_dep.R), g1(e_dep.C)), db_tuple(b1_proof),
        b"",   # empty membership: finding 8 -- must now revert
    )
    try:
        stack._send_from(fn, MALLORY, "Notes.spendCoupledB1(A1 opening)",
                         event="SpentCoupledB1", contract=stack.notes)
        raise AssertionError(
            "A1-via-B1 empty membership must revert (finding 8 inverted)")
    except RuntimeError as err:
        reason = str(err)
        print("A1-via-B1 empty membership REVERTED (finding 8 inverted)")
        print("   reason:", reason)
        print("   flavor in witness = 1 (A1); entry point = spendCoupledB1 (finding 7)")
        assert "empty identity membership" in reason, reason

    mal_bal = stack.buck.functions.balanceOf(MALLORY).call()
    dep_bal = stack.buck.functions.balanceOf(dep_addr).call()
    notes_bal = stack.buck.functions.balanceOf(stack.notes.address).call()
    spent = stack.notes.functions.nullifiers(nf).call()
    print("   nullifier consumed =", spent)
    print("   Mallory delta BUCK =", mal_bal - mal_bal_before)
    print("   addressee delta BUCK =", dep_bal - dep_bal_before)
    print("   pool delta BUCK =", notes_bal - notes_bal_before)
    assert not spent, "nullifier must not be consumed on revert"
    assert mal_bal == mal_bal_before, "Mallory must not receive BUCK"
    assert dep_bal == dep_bal_before, "addressee must not be paid by the attack"
    assert notes_bal == notes_bal_before, "pool must not move BUCK on revert"

    # Issuer substitution: a second B1-shaped call cannot reuse the nullifier
    # once spent, but the binding still accepts a different registered issuer
    # independently (finding 7 circuit gap, not closed by P0-0).
    fake_iss = Web3.to_checksum_address("0x00000000000000000000000000000000000015e1")
    anvil.set_code(fake_iss, ACCOUNT_STUB)
    fake_acct = Account(13579, 24680, 11111)
    stack.chain.send(
        bind5(fake_iss, g1(fake_acct.pk), (g1(fake_acct.E.R), g1(fake_acct.E.C)),
              True, False),
        sender=stack.gov)
    sub_proof, sub_e = b1_bind_prove(
        mallory.m, mallory.sk, mallory.E, fake_acct.pk,
        MALLORY_ADDR_INT, fx.chainid, rng=seeded(44))
    ok_sub = stack.reg.functions.verifyDepositorBinding(
        MALLORY, fake_iss, (g1(sub_e.R), g1(sub_e.C)), db_tuple(sub_proof)).call()
    print("depositor binding verifies against a SUBSTITUTE issuer =", ok_sub)
    assert ok_sub

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

    print("\nRESULT: finding 8 inverted -- empty membership no longer spends.")
    print("   A1-via-B1 with empty membership reverts; honest A1 with real")
    print("   membership+binding proofs still pays the addressee.")

# Required P0-0 control: fail-closed membership must not break an honest B1
# lifecycle carrying the committed real spend and G1-tie membership proofs.
b1_fx = E2EFixture.load("b1")
assert b1_fx.opening.flavor == 3, "fixture must be B1"

with PyrevmAnvil(chain_id=1, auto_impersonate=True, timestamp=1_700_000_000) as anvil:
    b1_stack = NotesStack(anvil, b1_fx, rng=seeded(45))
    b1_stack.bind_identities()
    for a in (b1_fx.issuer.addr, b1_fx.depositor.addr):
        anvil.set_code(b1_stack._addr(a), "0x")
    b1_stack.fund_issuer()
    b1_stack.approve_pool(b1_fx.issuer, 2 * b1_fx.face, "approve issuer->pool")
    b1_stack.approve_pool(b1_fx.depositor, 0, "approve depositor->pool")

    payout = b1_stack._addr(b1_fx.payout)
    payout_before = b1_stack.buck.functions.balanceOf(payout).call()
    pool_before = b1_stack.buck.functions.balanceOf(b1_stack.notes.address).call()
    b1_stack.mint()
    minted_pool = b1_stack.buck.functions.balanceOf(b1_stack.notes.address).call()
    honest_b1 = b1_stack.spend()
    payout_after = b1_stack.buck.functions.balanceOf(payout).call()
    pool_after = b1_stack.buck.functions.balanceOf(b1_stack.notes.address).call()

    assert minted_pool - pool_before == b1_fx.face, "B1 mint must escrow face"
    assert payout_after - payout_before == b1_fx.face, "honest B1 must pay recipient"
    assert minted_pool - pool_after == b1_fx.face, "honest B1 must debit the pool"
    assert b1_stack.notes.functions.nullifiers(b1_fx.nullifier).call(), \
        "honest B1 must consume nullifier"
    print("HONEST B1 spend with real membership proof succeeded; tx", honest_b1.txhash)
    print("   recipient delta BUCK =", payout_after - payout_before)
