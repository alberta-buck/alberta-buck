// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {IIdentityMembershipVerifier} from "../src/IIdentityMembershipVerifier.sol";
import {StubIdentityMembershipVerifier} from "../src/StubIdentityMembershipVerifier.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckTypes} from "../src/BuckTypes.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {Notes} from "../src/Notes.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";
import {SpendGroth16Verifier} from "../src/SpendGroth16Verifier.sol";
import {SpendVerifierAdapter} from "../src/SpendVerifierAdapter.sol";

/// @notice Integration test: identity membership verifier wired into Notes spend paths.
///         Exercises the Phase 9 identity-axis plumbing: governance sets the verifier,
///         the registry posts an identity root, and the spend paths gate on the
///         membership proof.  Uses the StubIdentityMembershipVerifier so the full
///         G1-tie circuit is not required for plumbing validation.
contract NotesIdentityMembershipTest is Test {
    Buck internal buck;
    BuckCreditHarness internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry internal reg;
    Notes internal notes;
    StubMintVerifier internal mintStub;
    StubSpendVerifier internal spendStub;
    StubIdentityMembershipVerifier internal idMemStub;

    address internal constant GOV = address(0xA0);
    address internal constant POOL = address(0xBA51C);

    address internal alice;
    address internal bob;
    uint256 internal constant SK_A = 0x1111111111111111111111111111111111111111111111111111111111111111;
    uint256 internal constant SK_B = 0x3333333333333333333333333333333333333333333333333333333333333333;
    uint256 internal constant K_A  = 0x2222222222222222222222222222222222222222222222222222222222222222;
    uint256 internal constant K_B  = 0x4444444444444444444444444444444444444444444444444444444444444444;
    BN254.G1Point internal pkA; BN254.G1Point internal RA;
    BN254.G1Point internal pkB; BN254.G1Point internal RB;

    uint256 internal identityRoot;

    function setUp() public {
        vm.chainId(1);

        // Identity layer (simplified — bind alice/bob as PUBLIC issuers with known keys).
        reg = new IdentityRegistryHarness(GOV);
        alice = 0x0411ce00000000000000000000000000000411ce;
        bob   = 0x00B0B00000000000000000000000000000000b0b;
        vm.etch(alice, hex"60006000fd");
        vm.etch(bob,   hex"60006000fd");
        reg.bindContract(alice, BN254.mul(BN254.g1(), SK_A),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, false);
        reg.bindContract(bob, BN254.mul(BN254.g1(), SK_B),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()), true, false);
        pkA = BN254.mul(BN254.g1(), SK_A);  RA = BN254.mul(BN254.g1(), K_A);
        pkB = BN254.mul(BN254.g1(), SK_B);  RB = BN254.mul(BN254.g1(), K_B);

        // Buck stack.
        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        // Notes stack.
        mintStub  = new StubMintVerifier(GOV);
        spendStub = new StubSpendVerifier(GOV);
        notes = new Notes(address(buck), address(mintStub), address(spendStub), GOV);
        vm.startPrank(GOV);
        notes.setIdentityRegistry(address(reg));
        vm.stopPrank();

        // Bind Notes as a Public-Identity carrying contract.
        reg.bindContract(address(notes), BN254.g1(),
                         IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()),
                         true, true);

        // Seed identity root (non-zero, so the membership gate fires).
        identityRoot = 0x2b1be837cccc27a8ab397ebd3818ffe3ae3f16fdda0bf9e62bde6d78a5336fa3;
        vm.prank(GOV);
        reg.setIdentityRoot(identityRoot);

        // Mutual decryptability fragment hack (same as Notes.t.sol).
        {
            bytes32 fragSlot = keccak256(
                abi.encode(address(notes), keccak256(abi.encode(alice, uint256(5))))
            );
            vm.store(address(buck), fragSlot, bytes32(uint256(1)));
        }

        // Fund alice and seed Notes pool with BUCK for spend payouts.
        _grantCredit(alice, 1000e18);
        vm.startPrank(alice);
        buck.mint(500e18);
        buck.approve(address(notes), type(uint256).max);
        buck.transfer(address(notes), 200e18);  // fund pool
        vm.stopPrank();

        // Seed noteFaceSum so the spend path doesn't underflow.  Storage slot
        // ordering: governance(0), mintVerifier(1), a2MintVerifier(2),
        // spendVerifier(3), spendAVerifier(4), identityRegistry(5),
        // identityMembershipVerifier(6), nullifiers-mapping(7),
        // noteFaceSum(8), nextLeafIndex(9), roots[0..29](10..39),
        // currentRootIndex(40).
        vm.store(address(notes), bytes32(uint256(8)), bytes32(uint256(1000e18)));
    }

    // ---- helpers ----------------------------------------------------------

    function _grantCredit(address who, uint256 faceValue) internal {
        uint256 tokenId = credit.createCredit(
            who, 0, faceValue, faceValue,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(who);
        credit.forceActivate(tokenId, faceValue);
    }

    // ---- identity membership verifier governance --------------------------

    function test_setIdentityMembershipVerifier() public {
        idMemStub = new StubIdentityMembershipVerifier();
        vm.prank(GOV);
        notes.setIdentityMembershipVerifier(address(idMemStub));
        assertEq(address(notes.identityMembershipVerifier()), address(idMemStub));
    }

    function test_setIdentityMembershipVerifier_onlyGovernance() public {
        idMemStub = new StubIdentityMembershipVerifier();
        vm.expectRevert(bytes("not governance"));
        notes.setIdentityMembershipVerifier(address(idMemStub));
    }

    function test_canSetToZero_disables() public {
        vm.prank(GOV);
        notes.setIdentityMembershipVerifier(address(0));
        assertEq(address(notes.identityMembershipVerifier()), address(0));
    }

    // ---- the folded deposit gate is not optional -------------------------

    /// @notice The addressed gate cannot be cleared.  An earlier shape treated
    ///         the fold as an upgrade over a coupling sigma, which meant a
    ///         deployment could be configured into a gate a payload thief
    ///         walks through.  There is no such configuration now.
    function test_setDepositFoldVerifier_refusesZero() public {
        vm.prank(GOV);
        vm.expectRevert(bytes("depositFoldVerifier=0"));
        notes.setDepositFoldVerifier(address(0));
    }

    function test_setDepositFoldVerifier_onlyGovernance() public {
        vm.expectRevert(bytes("not governance"));
        notes.setDepositFoldVerifier(address(this));
    }

    /// @notice With the slot unset an addressed spend reverts -- BEFORE the
    ///         spend SNARK, so an unwired deployment cannot pay out even with a
    ///         stub spend verifier that accepts everything.
    function test_addressedSpend_revertsWithoutFold() public {
        assertEq(address(notes.depositFoldVerifier()), address(0));
        IdentityRegistry.ElGamalCT memory zct;
        vm.expectRevert(bytes("Notes: deposit fold verifier not set"));
        notes.spendCoupledA1(hex"00", 0, 1, 100, address(this), zct, hex"00");
        vm.expectRevert(bytes("Notes: deposit fold verifier not set"));
        notes.spendCoupledA2(hex"00", 0, 1, 100, address(this), zct, hex"00");
    }
}
