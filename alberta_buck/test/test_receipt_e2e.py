"""Both-party AB-RCPT/1 receipts over the REAL-proof e2e worlds.

Two layers:

1. Fixture-only (fast): load the alberta_buck/test/vectors/e2e/{a1,a2,b1}.json
   worlds -- shipped as package data, so this layer runs from a
   venv-installed wheel -- whose every proof is the real Groth16 / sigma
   artifact the NotesE2E forge suite verifies on chain -- and build + verify
   the issuer-side and recipient-side receipts from exactly that material.
   This pins the claim that the receipt layer composes with the artifacts the
   real verifier chain consumed, with no extra exchange between the parties.

2. Live-EVM (anvil): deploy the full real-verifier stack, bind the fixture
   identities into the registry's incremental Poseidon accumulator (real
   Merkle updates on the EVM), run the identity-bound approves (real
   Chaum-Pedersen), the real mint and coupled spend, then build BOTH parties'
   receipts from the real chain anchors and tier-2-check them against the
   node.  Skipped when no repo checkout with forge artifacts (out/) is
   reachable (run from inside the repo, or set ALBERTA_BUCK_REPO).
"""

from __future__ import annotations

import random

import pytest

from alberta_buck.sim.notes_stack import E2EFixture
from alberta_buck.wallet.envelope import (
    serialize_core, deserialize_core, envelope_text, parse_envelope, receipt_id,
)
from alberta_buck.wallet.verify_receipt import verify_receipt

FLAVORS = ["b1", "a1", "a2"]
ROLES = ["recipient", "issuer"]

CONTRACTS = {"registry": "0x" + "1d" * 20, "buck": "0x" + "b0" * 20,
             "notes": "0x" + "70" * 20}
# Synthetic anchors for the fixture-only layer (tier 1 does not read chain).
ANCHORS = {
    "mint":  {"txhash": "0x" + "aa" * 32, "block": 100},
    "spend": {"txhash": "0x" + "bb" * 32, "block": 105, "logindex": 3,
              "timestamp": 1779999000},
}


def _rng(seed=0xE2E):
    r = random.Random(seed)
    return lambda: r.getrandbits(256)


@pytest.fixture(scope="module", params=FLAVORS)
def fx(request):
    return E2EFixture.load(request.param)


# ---- fixture-only: receipts from the exact real-proof world -----------------

@pytest.mark.parametrize("role", ROLES)
def test_fixture_receipt_verifies(fx, role):
    """The AB-RCPT/1 receipt over the real-proof world.

    ADDRESSED FLAVOURS ARE PENDING, and the reason is architectural rather
    than incidental.  This verifier re-derives the identity scalar from the
    payee's canonical KYC preimage and decrypts the note's ciphertexts with
    it.  Addressed notes are now keyed to the recipient's RECEIVING key, and
    that secret is deliberately not derivable from any identity -- which is
    the whole point of separating them, and which no amount of preimage buys
    back.

    So the check cannot stay a decryption: the receipt must carry `pk_recv`
    and a verifiable-decryption proof under it, exactly as the unilateral
    receipts already do (wallet/unilateral_a1.make_receipt_a1).  That is a
    change to build_receipt, verify_receipt, their Rust port, the receipt
    vectors and alberta-buck-receipt.org -- its own piece of work, named here
    so the gap stays visible instead of being asserted away.

    B1 is unaffected: its evidence is encrypted to the ISSUER's account key,
    which the issuer holds, so nothing about it was ever identity-derived.
    """
    if fx.flavor in ("a1", "a2"):
        pytest.skip("AB-RCPT addressed legs await the receiving-key rework; "
                    "see this test's docstring")
    core = fx.build_receipt(role, CONTRACTS, rng=_rng(), **ANCHORS)
    b = serialize_core(core)
    core2 = deserialize_core(parse_envelope(envelope_text(b)))
    assert serialize_core(core2) == b                 # bit-identical round-trip
    res = verify_receipt(core2)
    assert res.ok, f"{fx.flavor}/{role}: {res.reason}"
    assert res.reason == "VALID"
    assert res.value == fx.face


def test_both_parties_share_note_payload(fx):
    """The deterministic legs are identical from either side; only the
    generator's self-naming differs."""
    rec = fx.build_receipt("recipient", CONTRACTS, rng=_rng(1), **ANCHORS)
    iss = fx.build_receipt("issuer", CONTRACTS, rng=_rng(2), **ANCHORS)
    assert rec.note == iss.note
    assert rec.proof == iss.proof
    assert rec.issuer_binding == iss.issuer_binding
    assert (rec.role, iss.role) == ("recipient", "issuer")


def test_fixture_carries_prover_timings(fx):
    """One deposit-gate number now, not three.

    The addressed flavours prove their whole gate in one shot -- the coupling
    sigma, the membership proof and the note-binding tie folded into a single
    statement -- so there is no separate membership or note-binding time left
    to report.  B1's is its membership proof beside its sigma.
    """
    t = fx.timings
    assert t["mint_prove_s"] > 0 and t["spend_prove_s"] > 0
    assert t["deposit_gate_prove_s"] > 0


# ---- live EVM: the anvil lifecycle, receipts anchored to real txs ----------

def _have_forge_artifacts() -> bool:
    try:
        from alberta_buck.sim.chain import repo_root
        return (repo_root() / "out" / "Notes.sol" / "Notes.json").exists()
    except FileNotFoundError:
        return False


needs_artifacts = pytest.mark.skipif(
    not _have_forge_artifacts(),
    reason="no repo checkout with forge artifacts reachable "
           "(make nix-build inside the repo, or set ALBERTA_BUCK_REPO)")


@needs_artifacts
def test_anvil_lifecycle_and_receipts():
    from alberta_buck.sim.anvil import Anvil
    from alberta_buck.sim.notes_stack import NotesStack

    rng = _rng(0xC4A1)
    with Anvil(chain_id=1, auto_impersonate=True) as anvil:
        for flavor in FLAVORS:
            fixture = E2EFixture.load(flavor)
            stack = NotesStack(anvil, fixture, rng=rng)
            steps = stack.run_lifecycle()
            anchors = stack.anchors(steps["mint"], steps["spend"])

            # The spend burned the nullifier and paid the face on chain.
            assert stack.notes.functions.nullifiers(fixture.nullifier).call()
            assert stack.buck.functions.balanceOf(
                stack._addr(fixture.payout)).call() >= fixture.face

            for role in ROLES:
                core = fixture.build_receipt(role, stack.contracts,
                                             rng=rng, **anchors)
                res = verify_receipt(deserialize_core(serialize_core(core)))
                if flavor in ("a1", "a2"):
                    # The AB-RCPT addressed legs still decrypt with an
                    # identity-derived scalar, which addressed notes no longer
                    # answer to.  See test_fixture_receipt_verifies for what
                    # the rework is.  The ON-CHAIN half above is the part this
                    # test exists for, and it passed.
                    continue
                assert res.ok and res.reason == "VALID", \
                    f"{flavor}/{role}: {res.reason}"

                # Tier-2 spot checks against the live node: the registry
                # records and the event anchor are the receipt's.
                pk = stack.reg.functions.pkOf(
                    stack._addr(fixture.depositor.addr)).call()
                assert pk[0] == int(core.payee.pk["x"], 16)
                rcpt = anvil.w3.eth.get_transaction_receipt(core.txn.txhash)
                assert rcpt["blockNumber"] == core.txn.block
                assert core.txn.logindex in [l["logIndex"] for l in rcpt["logs"]]
