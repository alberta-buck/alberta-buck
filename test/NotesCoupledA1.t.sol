// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {StubIdentityMembershipVerifier} from "../src/StubIdentityMembershipVerifier.sol";
import {StubNoteBindingVerifier} from "../src/StubNoteBindingVerifier.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {Notes} from "../src/Notes.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";

/// @notice End-to-end wiring of the identity-M-bound A1 deposit (addressed,
///         public issuer): Notes.spendCoupledA1 co-verifies the deposit-coupling
///         sigma (reused from A2; here over eRec = Enc(M_rec, M_rec)) and the
///         P_I-bound membership proof, then pays out.  A1 and A2 share the
///         on-chain coupled spend; only the committed point's meaning differs
///         (A1: the recipient identity; A2: the private issuer).  The coupling
///         proof is the canonical Python-reference vector
///         (scripts/gen_unilateral_a1_vectors.py -> test/vectors/unilateral_a1.json);
///         the membership half uses the stub (the real G1-tie limb-binding is
///         proven in IdentityMembershipBinding.t.sol).  See
///         alberta-buck-notes.org ("Mutual Decryptability", one-gadget / A1) and notes-flow "Identity-M Spend Path".
contract NotesCoupledA1Test is Test {
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
    address internal depositor;
    address internal funder;
    uint256 internal constant SK_F = 0x5555555555555555555555555555555555555555555555555555555555555555;

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/unilateral_a1.json");

        reg = new IdentityRegistryHarness(GOV);

        depositor = address(uint160(_u(".depositor.addr")));
        vm.etch(depositor, hex"60006000fd");
        reg.bindContract(depositor, _g1(".depositor.pk"), _ct(".depositor.E"), true, false);

        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        mintStub  = new StubMintVerifier(GOV);
        spendStub = new StubSpendVerifier(GOV);
        notes = new Notes(address(buck), address(mintStub), address(spendStub), GOV);
        vm.startPrank(GOV);
        notes.setIdentityRegistry(address(reg));
        vm.stopPrank();

        reg.bindContract(address(notes), BN254.g1(),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, true);

        vm.prank(GOV);
        reg.setIdentityRoot(0x2b1be837cccc27a8ab397ebd3818ffe3ae3f16fdda0bf9e62bde6d78a5336fa3);

        idMemStub = new StubIdentityMembershipVerifier();
        vm.prank(GOV);
        notes.setIdentityMembershipVerifier(address(idMemStub));

        bindStub = new StubNoteBindingVerifier();
        vm.prank(GOV);
        notes.setNoteBindingVerifier(address(bindStub));

        bytes32 fragSlot = keccak256(
            abi.encode(address(notes), keccak256(abi.encode(depositor, uint256(5))))
        );
        vm.store(address(buck), fragSlot, bytes32(uint256(1)));

        funder = address(0xF00D);
        vm.etch(funder, hex"60006000fd");
        reg.bindContract(funder, BN254.mul(BN254.g1(), SK_F),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, false);
        _grantCredit(funder, 1000e18);
        vm.startPrank(funder);
        buck.mint(500e18);
        buck.transfer(address(notes), 200e18);
        vm.stopPrank();
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
    function _eRec() internal view returns (IdentityRegistry.ElGamalCT memory) {
        return _ct(".eRec");
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

    function test_coupledA1_spend_succeeds() public {
        uint256 root = notes.noteRoot();
        uint256 nf   = 0xA10;
        uint256 balBefore = buck.balanceOf(depositor);
        vm.prank(depositor);
        notes.spendCoupledA1(hex"00", root, nf, 100, depositor,
                             _eRec(), _dc(), hex"cafe", hex"beef");
        assertTrue(notes.nullifiers(nf), "nullifier consumed");
        assertEq(buck.balanceOf(depositor), balBefore + 100, "payout delivered");
    }

    function test_coupledA1_emitsSpentCoupledA1() public {
        uint256 root = notes.noteRoot();
        IdentityRegistry.DepositCouplingProof memory p = _dc();
        vm.expectEmit(true, true, false, true, address(notes));
        emit Notes.SpentCoupledA1(0xA11, 100, depositor, p.P_I.X, p.P_I.Y);
        vm.prank(depositor);
        notes.spendCoupledA1(hex"00", root, 0xA11, 100, depositor, _eRec(), p, hex"cafe", hex"beef");
    }

    // ---- soundness ----------------------------------------------------------

    function test_coupledA1_badCoupling_reverts() public {
        IdentityRegistry.DepositCouplingProof memory p = _dc();
        p.s_m = addmod(p.s_m, 1, BN254.R);
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad deposit coupling"));
        notes.spendCoupledA1(hex"00", root, 0xA12, 100, depositor, _eRec(), p, hex"cafe", hex"beef");
    }

    function test_coupledA1_unregisteredDepositor_reverts() public {
        address other = address(uint160(0xBEEF));
        vm.etch(other, hex"60006000fd");
        uint256 root = notes.noteRoot();
        vm.prank(other);
        vm.expectRevert(bytes("Notes: bad deposit coupling"));
        notes.spendCoupledA1(hex"00", root, 0xA13, 100, other, _eRec(), _dc(), hex"cafe", hex"beef");
    }

    function test_coupledA1_membershipRejected_reverts() public {
        idMemStub.setEnabled(false);
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad identity membership proof"));
        notes.spendCoupledA1(hex"00", root, 0xA14, 100, depositor, _eRec(), _dc(), hex"cafe", hex"beef");
    }

    function test_coupledA1_doubleSpend_reverts() public {
        uint256 root = notes.noteRoot();
        vm.startPrank(depositor);
        notes.spendCoupledA1(hex"00", root, 0xA15, 100, depositor, _eRec(), _dc(), hex"cafe", hex"beef");
        vm.expectRevert(bytes("Notes: already spent"));
        notes.spendCoupledA1(hex"00", root, 0xA15, 100, depositor, _eRec(), _dc(), hex"cafe", hex"beef");
        vm.stopPrank();
    }

    // ---- soundness: the note<->eEnc tie (RESERVED stub) ---------------------

    function test_coupledA1_noteBindingRejected_reverts() public {
        bindStub.setEnabled(false);
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad note binding"));
        notes.spendCoupledA1(hex"00", root, 0xA16, 100, depositor, _eRec(), _dc(), hex"cafe", hex"beef");
    }

    function test_coupledA1_emptyNoteBinding_reverts() public {
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: empty note binding"));
        notes.spendCoupledA1(hex"00", root, 0xA17, 100, depositor, _eRec(), _dc(), hex"cafe", "");
    }

    function test_coupledA1_emptyMembershipProof_reverts() public {
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: empty identity membership proof"));
        notes.spendCoupledA1(hex"00", root, 0xA18, 100, depositor, _eRec(), _dc(), "", hex"beef");
    }

    function test_coupledA1_unsetMembershipVerifier_reverts() public {
        vm.prank(GOV);
        notes.setIdentityMembershipVerifier(address(0));
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: membership verifier not set"));
        notes.spendCoupledA1(hex"00", root, 0xA19, 100, depositor, _eRec(), _dc(), hex"cafe", hex"beef");
    }

    function test_coupledA1_unsetNoteBindingVerifier_reverts() public {
        vm.prank(GOV);
        notes.setNoteBindingVerifier(address(0));
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: note binding verifier not set"));
        notes.spendCoupledA1(hex"00", root, 0xA1A, 100, depositor, _eRec(), _dc(), hex"cafe", hex"beef");
    }
}
