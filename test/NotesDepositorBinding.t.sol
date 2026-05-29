// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {Notes} from "../src/Notes.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BN254} from "../src/BN254.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";

contract MockBuckB {
    function transferFrom(address, address, uint256) external pure returns (bool) { return true; }
    function transfer(address, uint256) external pure returns (bool) { return true; }
}

/// @notice Phase 1 (b): the depositor->issuer half of mutual decryptability for
///         bearer notes.  verifyDepositorForIssuer reuses the verifyApprove
///         relation, so the canonical approve vector (Alice->Bob) is a valid
///         depositor(Alice)->issuer(Bob) re-encryption proof.  Notes.spend's
///         8-arg overload checks it and emits SpentB; bad proofs revert; the
///         5-arg overload stays unbound.  See alberta-buck-notes-decryptability.org.
contract NotesDepositorBindingTest is Test {
    address constant GOV = address(0xB0);

    IdentityRegistry reg;
    Notes            notes;
    string           vj;

    address depositor;   // Alice
    address issuer;      // Bob

    function setUp() public {
        vm.chainId(1);                       // approve transcript uses chainid = 1
        vj  = vm.readFile("test/vectors/identity.json");
        reg = new IdentityRegistry(GOV);

        depositor = address(uint160(_u(".approve.sender")));    // Alice
        issuer    = address(uint160(_u(".approve.spender")));   // Bob

        // Bind both with their vector keys.  verifyDepositorForIssuer needs
        // _E_addr[depositor], _pk[depositor], _pk[issuer]; bindContract sets
        // them just as register() would.  The depositor is encrypted-Identity
        // (isPublicIdentity = false) -- exactly the case where eDepForIss is
        // needed for the issuer to recover them.
        IdentityRegistry.ElGamalCT memory placeholder =
            IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1());
        vm.etch(depositor, hex"60006000fd");
        reg.bindContract(depositor, _g1(".alice.elgamal_kp.pk"), _ct(".alice.ciphertext"), false, false);
        vm.etch(issuer, hex"60006000fd");
        reg.bindContract(issuer, _g1(".bob.elgamal_kp.pk"), placeholder, true, false);

        StubMintVerifier  m = new StubMintVerifier(GOV);
        StubSpendVerifier s = new StubSpendVerifier(GOV);
        MockBuckB         b = new MockBuckB();
        notes = new Notes(address(b), address(m), address(s), GOV);
        vm.prank(GOV);
        notes.setIdentityRegistry(address(reg));

        // Seed noteFaceSum so the spend tests don't underflow it.  Mint from a
        // non-public minter (this test contract) -> the Schnorr binding is
        // skipped; MockBuckB.transferFrom is a no-op true.
        uint256[] memory seed = new uint256[](1);
        seed[0] = 0x1234;
        notes.mint(hex"00", notes.noteRoot(), 999, notes.nextLeafIndex(), 1000, seed);
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
    function _eDepForIss() internal view returns (IdentityRegistry.ElGamalCT memory) {
        return _ct(".approve.E_for_bob");
    }
    function _cp() internal view returns (IdentityRegistry.CPProof memory pi) {
        pi.e  = _u(".approve.cp_proof.e");
        pi.s1 = _u(".approve.cp_proof.s1");
        pi.s2 = _u(".approve.cp_proof.s2");
        pi.T1 = _g1(".approve.cp_proof.T1");
        pi.T2 = _g1(".approve.cp_proof.T2");
        pi.T3 = _g1(".approve.cp_proof.T3");
    }

    // ---- registry-level parity ---------------------------------------------

    function test_verifyDepositorForIssuer_acceptsApproveVector() public view {
        assertTrue(reg.verifyDepositorForIssuer(depositor, issuer, _eDepForIss(), _cp()),
                   "approve relation must verify as depositor->issuer");
        // Same relation as verifyApprove.
        assertTrue(reg.verifyApprove(depositor, issuer, _eDepForIss(), _cp()));
    }

    function test_verifyDepositorForIssuer_tamperedCP_rejected() public view {
        IdentityRegistry.CPProof memory pi = _cp();
        pi.s1 = addmod(pi.s1, 1, BN254.R);
        assertFalse(reg.verifyDepositorForIssuer(depositor, issuer, _eDepForIss(), pi));
    }

    // ---- Notes.spend wiring -------------------------------------------------

    function test_spend_withDepositorBinding_emitsSpentB() public {
        uint256 root = notes.noteRoot();     // EMPTY_ROOT, accepted at genesis
        vm.prank(depositor);
        notes.spend(hex"00", root, 0x111, 100, depositor, issuer, _eDepForIss(), _cp());
        assertTrue(notes.nullifiers(0x111), "bound bearer spend consumes the nullifier");
    }

    function test_spend_badDepositorBinding_reverts() public {
        uint256 root = notes.noteRoot();
        IdentityRegistry.CPProof memory pi = _cp();
        pi.s1 = addmod(pi.s1, 1, BN254.R);   // tamper
        vm.prank(depositor);
        vm.expectRevert("Notes: bad depositor binding");
        notes.spend(hex"00", root, 0x222, 100, depositor, issuer, _eDepForIss(), pi);
    }

    function test_spend_legacy5arg_unbound_succeeds() public {
        uint256 root = notes.noteRoot();
        vm.prank(depositor);
        notes.spend(hex"00", root, 0x333, 100, depositor);   // no depositor binding
        assertTrue(notes.nullifiers(0x333), "legacy spend still works");
    }
}
