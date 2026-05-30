// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {Notes} from "../src/Notes.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BN254} from "../src/BN254.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";

contract MockBuckA2 {
    function transferFrom(address, address, uint256) external pure returns (bool) { return true; }
    function transfer(address, uint256) external pure returns (bool) { return true; }
}

/// @notice Phase 2: Notes.mint(..., A2Binding[]) anchors verified A2
///         (private-issuer) recipient-blinded re-encryption bindings on chain.
///         A valid binding (the canonical issuer_reenc vector) mints and emits
///         IssuerReencBound; a tampered binding reverts the whole mint.  Leaf
///         tie / per-leaf completeness await the mint SNARK's issuerMode (see
///         alberta-buck-notes-decryptability.org).
contract NotesA2BindingTest is Test {
    address constant GOV = address(0xB0);

    IdentityRegistry reg;
    Notes            notes;
    string           vj;
    address          issuer;   // Bob (private A2 issuer = msg.sender at mint)

    function setUp() public {
        vm.chainId(1);                       // issuer_reenc transcript uses chainid = 1
        vj  = vm.readFile("test/vectors/identity.json");
        reg = new IdentityRegistry(GOV);

        issuer = address(uint160(_u(".issuer_reenc.issuer")));
        vm.etch(issuer, hex"60006000fd");
        // A2 issuer is a registered *private* Identity with its real E_addr.
        reg.bindContract(issuer, _g1(".issuer_reenc.pk_iss"),
                         _ct(".issuer_reenc.E_reg"), false, false);

        StubMintVerifier  m = new StubMintVerifier(GOV);
        StubSpendVerifier s = new StubSpendVerifier(GOV);
        MockBuckA2        b = new MockBuckA2();
        notes = new Notes(address(b), address(m), address(s), GOV);
        vm.prank(GOV);
        notes.setIdentityRegistry(address(reg));
    }

    // ---- helpers -----------------------------------------------------------

    function _u(string memory k) internal view returns (uint256) { return vm.parseJsonUint(vj, k); }
    function _g1(string memory k) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(k, ".x")), _u(string.concat(k, ".y")));
    }
    function _ct(string memory k) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(k, ".R"));
        c.C = _g1(string.concat(k, ".C"));
    }
    function _proof() internal view returns (IdentityRegistry.IssuerReencProof memory p) {
        p.e   = _u(".issuer_reenc.proof.e");
        p.s_r = _u(".issuer_reenc.proof.s_r");
        p.s_b = _u(".issuer_reenc.proof.s_b");
        p.s_s = _u(".issuer_reenc.proof.s_s");
        p.s_g = _u(".issuer_reenc.proof.s_g");
        p.A1  = _g1(".issuer_reenc.proof.A1");
        p.A2  = _g1(".issuer_reenc.proof.A2");
        p.A3  = _g1(".issuer_reenc.proof.A3");
        p.A4  = _g1(".issuer_reenc.proof.A4");
        p.A5  = _g1(".issuer_reenc.proof.A5");
        p.Q   = _g1(".issuer_reenc.proof.Q");
        p.U   = _g1(".issuer_reenc.proof.U");
        p.T   = _g1(".issuer_reenc.proof.T");
    }
    function _bindings() internal view returns (Notes.A2Binding[] memory a) {
        a = new Notes.A2Binding[](1);
        a[0] = Notes.A2Binding({eIss: _ct(".issuer_reenc.E_iss"), proof: _proof()});
    }
    function _cms() internal pure returns (uint256[] memory cms) {
        cms = new uint256[](1);
        cms[0] = 0x1234;
    }

    // ---- tests -------------------------------------------------------------

    // Hoist all external-call args to locals: an external call in the mint
    // argument list (notes.noteRoot(), nextLeafIndex()) would otherwise consume
    // the vm.prank / trip vm.expectRevert before the mint itself runs.

    function test_mint_withValidA2Binding_succeeds() public {
        uint256 root = notes.noteRoot();
        uint32  idx  = notes.nextLeafIndex();
        uint256[] memory cms = _cms();
        Notes.A2Binding[] memory a = _bindings();
        vm.prank(issuer);    // msg.sender = the A2 issuer
        notes.mint(hex"00", root, 999, idx, 1000, cms, a);
        assertEq(notes.nextLeafIndex(), 1, "leaf appended");
        assertEq(notes.noteFaceSum(), 1000, "face accumulated");
    }

    function test_mint_emitsIssuerReencBound() public {
        uint256 root = notes.noteRoot();
        uint32  idx  = notes.nextLeafIndex();
        uint256[] memory cms = _cms();
        Notes.A2Binding[] memory a = _bindings();
        vm.recordLogs();
        vm.prank(issuer);
        notes.mint(hex"00", root, 999, idx, 1000, cms, a);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 topic = keccak256("IssuerReencBound(address,uint256,uint256)");
        bool found;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == topic) { found = true; break; }
        }
        assertTrue(found, "IssuerReencBound emitted");
    }

    function test_mint_badA2Binding_reverts() public {
        uint256 root = notes.noteRoot();
        uint32  idx  = notes.nextLeafIndex();
        uint256[] memory cms = _cms();
        Notes.A2Binding[] memory a = _bindings();
        a[0].proof.s_r = addmod(a[0].proof.s_r, 1, BN254.R);   // tamper
        vm.prank(issuer);
        vm.expectRevert("Notes: bad A2 binding");
        notes.mint(hex"00", root, 999, idx, 1000, cms, a);
    }

    function test_mint_emptyA2Bindings_reverts() public {
        uint256 root = notes.noteRoot();
        uint32  idx  = notes.nextLeafIndex();
        uint256[] memory cms = _cms();
        Notes.A2Binding[] memory a = new Notes.A2Binding[](0);
        vm.prank(issuer);
        vm.expectRevert("Notes: no A2 bindings");
        notes.mint(hex"00", root, 999, idx, 1000, cms, a);
    }
}
