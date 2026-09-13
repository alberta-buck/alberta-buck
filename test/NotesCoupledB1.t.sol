// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {StubIdentityMembershipVerifier} from "../src/StubIdentityMembershipVerifier.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";
import {Notes} from "../src/Notes.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";

/// @notice End-to-end wiring of the identity-M-bound B1 deposit (bearer, public
///         issuer): Notes.spendCoupledB1 co-verifies the depositor-binding sigma
///         (IdentityRegistry.verifyDepositorBinding, extended with the P_dep
///         commitment) and the P_dep-bound membership proof, then pays out.  The
///         binding proof is the canonical Python-reference vector
///         (scripts/gen_b1_binding_vectors.py -> test/vectors/b1_binding.json);
///         the membership half uses the stub (the real G1-tie limb-binding is
///         proven in IdentityMembershipBinding.t.sol).  This is the
///         membership-bound B1 spender path that completes the Identity-M design
///         for bearer notes.  See alberta-buck-notes.org ("Mutual Decryptability", B1) and notes-flow "Identity-M Spend Path".
contract NotesCoupledB1Test is Test {
    Buck internal buck;
    BuckCreditHarness internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry internal reg;
    Notes internal notes;
    StubMintVerifier internal mintStub;
    StubSpendVerifier internal spendStub;
    StubIdentityMembershipVerifier internal idMemStub;

    address internal constant GOV  = address(0xA0);
    address internal constant POOL = address(0xBA51C);

    string  internal vj;
    address internal depositor;   // the payout account (= recipient), bound to M_dep
    address internal issuer;      // public bearer issuer
    address internal funder;
    uint256 internal constant SK_F = 0x5555555555555555555555555555555555555555555555555555555555555555;

    function setUp() public {
        vm.chainId(1);
        vj = vm.readFile("test/vectors/b1_binding.json");

        reg = new IdentityRegistryHarness(GOV);

        depositor = address(uint160(_u(".depositor.addr")));
        issuer    = address(uint160(_u(".issuer.addr")));
        vm.etch(depositor, hex"60006000fd");
        vm.etch(issuer,    hex"60006000fd");
        reg.bindContract(depositor, _g1(".depositor.pk"), _ct(".depositor.E"),  true, false);
        reg.bindContract(issuer,    _g1(".issuer.pk"),    _ct(".issuer.E_reg"), true, false);

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
    function _eDepForIss() internal view returns (IdentityRegistry.ElGamalCT memory) {
        return _ct(".eDepForIss");
    }
    function _b1() internal view returns (IdentityRegistry.DepositorBindingProof memory p) {
        p.e     = _u(".depositor_binding.e");
        p.s_m   = _u(".depositor_binding.s_m");
        p.s_s   = _u(".depositor_binding.s_s");
        p.s_r   = _u(".depositor_binding.s_r");
        p.s_b   = _u(".depositor_binding.s_b");
        p.A2    = _g1(".depositor_binding.A2");
        p.A4    = _g1(".depositor_binding.A4");
        p.B1    = _g1(".depositor_binding.B1");
        p.B2    = _g1(".depositor_binding.B2");
        p.A_p   = _g1(".depositor_binding.A_p");
        p.P_dep = _g1(".depositor_binding.P_dep");
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

    function test_coupledB1_spend_succeeds() public {
        uint256 root = notes.noteRoot();
        uint256 nf   = 0xB10;
        uint256 balBefore = buck.balanceOf(depositor);
        vm.prank(depositor);
        notes.spendCoupledB1(hex"00", root, nf, 100, depositor, issuer,
                             _eDepForIss(), _b1(), hex"cafe");
        assertTrue(notes.nullifiers(nf), "nullifier consumed");
        assertEq(buck.balanceOf(depositor), balBefore + 100, "payout delivered");
    }

    function test_coupledB1_emitsSpentCoupledB1() public {
        uint256 root = notes.noteRoot();
        vm.expectEmit(true, true, true, true, address(notes));
        emit Notes.SpentCoupledB1(0xB11, 100, depositor, issuer, _eDepForIss());
        vm.prank(depositor);
        notes.spendCoupledB1(hex"00", root, 0xB11, 100, depositor, issuer,
                             _eDepForIss(), _b1(), hex"cafe");
    }

    // ---- soundness ----------------------------------------------------------

    function test_coupledB1_badBinding_reverts() public {
        IdentityRegistry.DepositorBindingProof memory p = _b1();
        p.s_m = addmod(p.s_m, 1, BN254.R);
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad depositor binding"));
        notes.spendCoupledB1(hex"00", root, 0xB12, 100, depositor, issuer,
                             _eDepForIss(), p, hex"cafe");
    }

    function test_coupledB1_tamperedPdep_reverts() public {
        // Perturb P_dep: the P relation (s_m*G + s_b*H == A_p + e*P_dep) breaks.
        IdentityRegistry.DepositorBindingProof memory p = _b1();
        p.P_dep = BN254.add(p.P_dep, BN254.g1());
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad depositor binding"));
        notes.spendCoupledB1(hex"00", root, 0xB13, 100, depositor, issuer,
                             _eDepForIss(), p, hex"cafe");
    }

    function test_coupledB1_membershipRejected_reverts() public {
        idMemStub.setEnabled(false);
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        vm.expectRevert(bytes("Notes: bad identity membership proof"));
        notes.spendCoupledB1(hex"00", root, 0xB14, 100, depositor, issuer,
                             _eDepForIss(), _b1(), hex"cafe");
    }

    function test_coupledB1_doubleSpend_reverts() public {
        uint256 root = notes.noteRoot();
        vm.startPrank(depositor);
        notes.spendCoupledB1(hex"00", root, 0xB15, 100, depositor, issuer,
                             _eDepForIss(), _b1(), hex"cafe");
        vm.expectRevert(bytes("Notes: already spent"));
        notes.spendCoupledB1(hex"00", root, 0xB15, 100, depositor, issuer,
                             _eDepForIss(), _b1(), hex"cafe");
        vm.stopPrank();
    }
}
