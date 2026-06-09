// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {StubIdentityMembershipVerifier} from "../src/StubIdentityMembershipVerifier.sol";
import {StubNoteBindingVerifier} from "../src/StubNoteBindingVerifier.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {Notes} from "../src/Notes.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";

/// @notice End-to-end wiring of the identity-M-bound (unilateral) A2 deposit:
///         Notes.spendCoupledA2 co-verifies the deposit-coupling sigma
///         (IdentityRegistry.verifyDepositCoupling) and the P_I-bound membership
///         proof, then pays the note out.  The deposit-coupling proof is the
///         canonical Python-reference vector (test/vectors/unilateral_a2.json,
///         the same one UnilateralA2.t.sol pins at the registry level); the
///         membership half uses StubIdentityMembershipVerifier so the full G1-tie
///         circuit is not required for the Notes-path validation (the real
///         limb-binding is proven in IdentityMembershipBinding.t.sol).
///
///         The decisive structural property: spendCoupledA2 reads `dc.P_I` ONCE
///         and feeds it to BOTH the coupling sigma (which constrains eIss to
///         decrypt to it) and the membership verifier — so the two halves are
///         bound to the same point by construction.  See
///         alberta-buck-notes-unilateral.org.
contract NotesCoupledA2Test is Test {
    Buck internal buck;
    BuckCreditHarness internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry internal reg;
    Notes internal notes;
    StubMintVerifier internal mintStub;
    StubSpendVerifier internal spendStub;
    StubIdentityMembershipVerifier internal idMemStub;
    StubNoteBindingVerifier internal bindStub;

    address internal constant GOV  = address(0xA0);
    address internal constant POOL = address(0xBA51C);

    string  internal vj;
    address internal depositor;   // the payout account, bound to identity m_rec
    address internal funder;      // funds the pool and seeds credit
    uint256 internal constant SK_F = 0x5555555555555555555555555555555555555555555555555555555555555555;

    function setUp() public {
        vm.chainId(1);                           // wallet transcripts use chainid = 1
        vj = vm.readFile("test/vectors/unilateral_a2.json");

        reg = new IdentityRegistry(GOV);

        // The depositor: any registered Fountain account bound to the recipient
        // identity m_rec.  verifyDepositCoupling reads its (pk, E_addr).
        depositor = address(uint160(_u(".depositor.addr")));
        vm.etch(depositor, hex"60006000fd");
        reg.bindContract(depositor, _g1(".depositor.pk"), _ct(".depositor.E"), true, false);

        // Buck stack.
        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        // Notes stack with stub mint/spend verifiers.
        mintStub  = new StubMintVerifier(GOV);
        spendStub = new StubSpendVerifier(GOV);
        notes = new Notes(address(buck), address(mintStub), address(spendStub), GOV);
        vm.startPrank(GOV);
        notes.setIdentityRegistry(address(reg));
        vm.stopPrank();

        // Bind Notes as a Public-Identity carrying contract.
        reg.bindContract(address(notes), BN254.g1(),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, true);

        // Seed a non-zero identity root so the membership gate fires.
        vm.prank(GOV);
        reg.setIdentityRoot(0x2b1be837cccc27a8ab397ebd3818ffe3ae3f16fdda0bf9e62bde6d78a5336fa3);

        // Membership verifier wired (stub) — exercises the bound-membership call.
        idMemStub = new StubIdentityMembershipVerifier();
        vm.prank(GOV);
        notes.setIdentityMembershipVerifier(address(idMemStub));

        // Note<->eEnc tie verifier wired (stub) — exercises the binding call.
        bindStub = new StubNoteBindingVerifier();
        vm.prank(GOV);
        notes.setNoteBindingVerifier(address(bindStub));

        // The pool->depositor payout requires the depositor's receipt fragment
        // for the notes sender (mutual-decryptability gate).  _receiptFragments
        // is slot 5: _receiptFragments[recipient][sender].
        bytes32 fragSlot = keccak256(
            abi.encode(address(notes), keccak256(abi.encode(depositor, uint256(5))))
        );
        vm.store(address(buck), fragSlot, bytes32(uint256(1)));

        // Fund the pool with BUCK from a registered (non-carrying) funder.
        funder = address(0xF00D);
        vm.etch(funder, hex"60006000fd");
        reg.bindContract(funder, BN254.mul(BN254.g1(), SK_F),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, false);
        _grantCredit(funder, 1000e18);
        vm.startPrank(funder);
        buck.mint(500e18);
        buck.transfer(address(notes), 200e18);   // fund pool
        vm.stopPrank();
        // Seed noteFaceSum so the spend path doesn't underflow.
        // noteFaceSum is storage slot 8 (see Notes layout comment).
        vm.store(address(notes), bytes32(uint256(8)), bytes32(uint256(1000e18)));
    }

    // ---- vector helpers -----------------------------------------------------

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }
    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }
    function _ct(string memory key) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(key, ".R"));
        c.C = _g1(string.concat(key, ".C"));
    }
    function _eIss() internal view returns (IdentityRegistry.ElGamalCT memory) {
        return _ct(".eIss");
    }
    function _dc() internal view returns (IdentityRegistry.DepositCouplingProof memory p) {
        p.e   = _u(".deposit_coupling.e");
        p.s_m = _u(".deposit_coupling.s_m");
        p.s_s = _u(".deposit_coupling.s_s");
        p.s_b = _u(".deposit_coupling.s_b");
        p.A2  = _g1(".deposit_coupling.A2");
        p.A3  = _g1(".deposit_coupling.A3");
        p.A4  = _g1(".deposit_coupling.A4");
        p.P_I = _g1(".deposit_coupling.P_I");
    }

    function _grantCredit(address who, uint256 faceValue) internal {
        uint256 tokenId = credit.createCredit(
            who, 0, faceValue, faceValue,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(who);
        credit.forceActivate(tokenId, faceValue);
    }

    // ---- completeness -------------------------------------------------------

    function test_coupledA2_spend_succeeds() public {
        uint256 root = notes.noteRoot();
        uint256 nf   = 0xC0;
        uint256 balBefore = buck.balanceOf(depositor);

        vm.prank(depositor);
        notes.spendCoupledA2(hex"00", root, nf, 100, depositor,
                             _eIss(), _dc(), hex"cafe", hex"beef");

        assertTrue(notes.nullifiers(nf), "nullifier consumed");
        assertEq(buck.balanceOf(depositor), balBefore + 100, "payout delivered");
    }

    function test_coupledA2_emitsSpentCoupledA2() public {
        uint256 root = notes.noteRoot();
        IdentityRegistry.DepositCouplingProof memory p = _dc();
        vm.expectEmit(true, true, false, true, address(notes));
        emit Notes.SpentCoupledA2(0xC1, 100, depositor, p.P_I.X, p.P_I.Y);
        vm.prank(depositor);
        notes.spendCoupledA2(hex"00", root, 0xC1, 100, depositor, _eIss(), p, hex"cafe", hex"beef");
    }

    // ---- soundness: the coupling half ---------------------------------------

    function test_coupledA2_badCoupling_reverts() public {
        // Perturb s_m: the deposit-coupling sigma no longer verifies.
        IdentityRegistry.DepositCouplingProof memory p = _dc();
        p.s_m = addmod(p.s_m, 1, BN254.R);

        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad deposit coupling"));
        notes.spendCoupledA2(hex"00", root, 0xC2, 100, depositor, _eIss(), p, hex"cafe", hex"beef");
    }

    function test_coupledA2_tamperedPI_reverts() public {
        // The collusion case at the coupling layer: a different P_I breaks E3.
        IdentityRegistry.DepositCouplingProof memory p = _dc();
        p.P_I = BN254.add(p.P_I, BN254.g1());

        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad deposit coupling"));
        notes.spendCoupledA2(hex"00", root, 0xC3, 100, depositor, _eIss(), p, hex"cafe", hex"beef");
    }

    function test_coupledA2_unregisteredDepositor_reverts() public {
        // msg.sender is not the registered account the coupling was bound to.
        address other = address(uint160(0xBEEF));
        vm.etch(other, hex"60006000fd");
        uint256 root = notes.noteRoot();
        vm.prank(other);
        vm.expectRevert(bytes("Notes: bad deposit coupling"));
        notes.spendCoupledA2(hex"00", root, 0xC4, 100, other, _eIss(), _dc(), hex"cafe", hex"beef");
    }

    // ---- soundness: the membership half -------------------------------------

    function test_coupledA2_membershipRejected_reverts() public {
        // Disable the membership stub: the bound membership check fails closed.
        idMemStub.setEnabled(false);
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad identity membership proof"));
        notes.spendCoupledA2(hex"00", root, 0xC5, 100, depositor, _eIss(), _dc(), hex"cafe", hex"beef");
    }

    function test_coupledA2_emptyMembershipProof_skips() public {
        // Empty membership proof: backward-compat skip (the coupling still gates).
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        notes.spendCoupledA2(hex"00", root, 0xC6, 100, depositor, _eIss(), _dc(), "", hex"beef");
        assertTrue(notes.nullifiers(0xC6), "nullifier consumed");
    }

    // ---- double-spend -------------------------------------------------------

    function test_coupledA2_doubleSpend_reverts() public {
        uint256 root = notes.noteRoot();
        vm.startPrank(depositor);
        notes.spendCoupledA2(hex"00", root, 0xC7, 100, depositor, _eIss(), _dc(), hex"cafe", hex"beef");
        vm.expectRevert(bytes("Notes: already spent"));
        notes.spendCoupledA2(hex"00", root, 0xC7, 100, depositor, _eIss(), _dc(), hex"cafe", hex"beef");
        vm.stopPrank();
    }

    // ---- soundness: the note<->eEnc tie (RESERVED stub) ---------------------

    function test_coupledA2_noteBindingRejected_reverts() public {
        // Disable the note-binding stub: the tie check fails closed.  (When the
        // real INoteBindingVerifier ships, this is the branch that rejects a
        // depositor-substituted eEnc.)
        bindStub.setEnabled(false);
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad note binding"));
        notes.spendCoupledA2(hex"00", root, 0xC8, 100, depositor, _eIss(), _dc(), hex"cafe", hex"beef");
    }

    function test_coupledA2_emptyNoteBinding_skips() public {
        // Empty note-binding proof: backward-compat skip (the coupling + membership
        // still gate).  Documents the RESERVED status: with no tie, the spend
        // proceeds even though eEnc is not bound to the note.
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        notes.spendCoupledA2(hex"00", root, 0xC9, 100, depositor, _eIss(), _dc(), hex"cafe", "");
        assertTrue(notes.nullifiers(0xC9), "nullifier consumed");
    }
}
