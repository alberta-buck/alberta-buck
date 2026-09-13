// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {Notes} from "../src/Notes.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BN254} from "../src/BN254.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubMintVerifierA2} from "../src/StubMintVerifierA2.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";

contract MockBuckMode {
    function transferFrom(address, address, uint256) external pure returns (bool) { return true; }
    function transfer(address, uint256) external pure returns (bool) { return true; }
}

/// @notice Phase 2: Notes.mint per-leaf issuerMode gating (The Required Mint
///         SNARK Signal).  The circuit exposes issuerMode[i] in {PUBLIC=1,
///         PRIVATE=2} as the deterministic flavor projection (A2 -> PRIVATE,
///         {A1,B1} -> PUBLIC); the contract gates on it.
///
///         The headline property is "bearer => public issuer": a bearer (B1)
///         leaf projects to PUBLIC, so it can only be minted through the
///         PUBLIC-mode path, which rejects any non-public issuer.  A private
///         issuer is therefore unable to mint an (unnameable) bearer note.
///
///         issuerMode binding to the committed flavor lands with the per-N
///         mint-verifier regen; these tests exercise the contract gate against
///         StubMintVerifier (it stands in for a verifier that binds issuerMode).
contract NotesIssuerModeTest is Test {
    address constant GOV = address(0xB0);

    IdentityRegistry reg;
    Notes            notes;
    string           vj;

    // Public issuer: pk = SK*G, registered isPublicIdentity = true.
    address pubIssuer = makeAddr("pubIssuer");
    uint256 constant SK = 0x1111111111111111111111111111111111111111111111111111111111111111;
    uint256 constant K  = 0x2222222222222222222222222222222222222222222222222222222222222222;

    // Private A2 issuer: the canonical issuer_reenc vector's registered Identity.
    address a2Issuer;

    function setUp() public {
        vm.chainId(1);                          // issuer_reenc transcript chainid
        vj  = vm.readFile("test/vectors/identity.json");
        reg = new IdentityRegistryHarness(GOV);

        StubMintVerifier  m = new StubMintVerifier(GOV);
        StubSpendVerifier s = new StubSpendVerifier(GOV);
        MockBuckMode      b = new MockBuckMode();
        notes = new Notes(address(b), address(m), address(s), GOV);
        vm.prank(GOV);
        notes.setIdentityRegistry(address(reg));
        // PRIVATE-mode (A2) mints route through the A2 verifier; stub it.
        StubMintVerifierA2 m2 = new StubMintVerifierA2(GOV);
        vm.prank(GOV);
        notes.setA2MintVerifier(address(m2));

        // Public issuer (pk = SK*G).
        vm.etch(pubIssuer, hex"60006000fd");
        reg.bindContract(
            pubIssuer, BN254.mul(BN254.g1(), SK),
            IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1()),
            true /*isPublicIdentity*/, false);

        // Private A2 issuer (registered *private* Identity with its real E_addr).
        a2Issuer = address(uint160(_u(".issuer_reenc.issuer")));
        vm.etch(a2Issuer, hex"60006000fd");
        reg.bindContract(a2Issuer, _g1(".issuer_reenc.pk_iss"),
                         _ct(".issuer_reenc.E_reg"), false /*private*/, false);
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
    function _binding() internal view returns (Notes.A2Binding memory a) {
        a = Notes.A2Binding({eIss: _ct(".issuer_reenc.E_iss"), proof: _proof()});
    }

    function _cms(uint256 n) internal pure returns (uint256[] memory cms) {
        cms = new uint256[](n);
        for (uint256 i = 0; i < n; i++) cms[i] = 0x1234 + i;
    }
    function _modes(uint256 n, uint256 mode) internal pure returns (uint256[] memory mm) {
        mm = new uint256[](n);
        for (uint256 i = 0; i < n; i++) mm[i] = mode;
    }

    /// @dev Schnorr-sign keccak256(cms) under `sk`, bound to `iss` (mirrors
    ///      IdentityRegistry._fsIssuerSchnorr).
    function _sign(uint256 sk, uint256 k, uint256[] memory cms, address iss)
        internal view returns (IdentityRegistry.SchnorrProof memory sig)
    {
        BN254.G1Point[] memory pts = new BN254.G1Point[](2);
        pts[0] = BN254.mul(BN254.g1(), sk);
        pts[1] = BN254.mul(BN254.g1(), k);
        uint256[] memory scl = new uint256[](3);
        scl[0] = uint256(keccak256(abi.encodePacked(cms)));
        scl[1] = uint256(uint160(iss));
        scl[2] = block.chainid;
        uint256 e = BN254.fsChallenge(pts, scl);
        uint256 sresp = addmod(k, mulmod(e, sk, BN254.R), BN254.R);
        sig = IdentityRegistry.SchnorrProof(e, sresp, pts[1]);
    }

    // ==== PUBLIC-mode path ==================================================

    function test_publicBatch_allPublicMode_mints() public {
        uint256[] memory cms  = _cms(1);
        uint256[] memory mm   = _modes(1, notes.MODE_PUBLIC());
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, pubIssuer);
        uint256 root = notes.noteRoot();
        vm.prank(pubIssuer);
        notes.mint(hex"00", root, 12345, 0, 0, cms, mm, sig);
        assertEq(notes.nextLeafIndex(), 1, "public-mode mint appends");
    }

    /// @notice Headline: a registered PRIVATE issuer cannot mint a PUBLIC-mode
    ///         (e.g. bearer) leaf -- "bearer => public issuer".
    function test_bearerFromPrivateIssuer_reverts() public {
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, notes.MODE_PUBLIC());
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, a2Issuer);
        uint256 root = notes.noteRoot();
        vm.prank(a2Issuer);                     // a registered *private* Identity
        vm.expectRevert("Notes: public-mode leaf needs public issuer");
        notes.mint(hex"00", root, 12345, 0, 0, cms, mm, sig);
    }

    /// @notice ... and neither can an unregistered EOA.
    function test_bearerFromUnregisteredEOA_reverts() public {
        address eoa = makeAddr("eoa");
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, notes.MODE_PUBLIC());
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, eoa);
        uint256 root = notes.noteRoot();
        vm.prank(eoa);
        vm.expectRevert("Notes: public-mode leaf needs public issuer");
        notes.mint(hex"00", root, 12345, 0, 0, cms, mm, sig);
    }

    function test_publicBatch_badSchnorr_reverts() public {
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, notes.MODE_PUBLIC());
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, pubIssuer);
        sig.s = addmod(sig.s, 1, BN254.R);      // perturb the response
        uint256 root = notes.noteRoot();
        vm.prank(pubIssuer);
        vm.expectRevert("Notes: bad issuer binding");
        notes.mint(hex"00", root, 12345, 0, 0, cms, mm, sig);
    }

    function test_publicBatch_privateModeLeaf_reverts() public {
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, notes.MODE_PRIVATE());
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, pubIssuer);
        uint256 root = notes.noteRoot();
        vm.prank(pubIssuer);
        vm.expectRevert("Notes: private leaf needs A2 overload");
        notes.mint(hex"00", root, 12345, 0, 0, cms, mm, sig);
    }

    function test_publicBatch_badModeValue_reverts() public {
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, 3);    // neither PUBLIC nor PRIVATE
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, pubIssuer);
        uint256 root = notes.noteRoot();
        vm.prank(pubIssuer);
        vm.expectRevert("Notes: bad issuerMode");
        notes.mint(hex"00", root, 12345, 0, 0, cms, mm, sig);
    }

    function test_publicBatch_lengthMismatch_reverts() public {
        uint256[] memory cms = _cms(2);
        uint256[] memory mm  = _modes(1, notes.MODE_PUBLIC());   // too short
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, pubIssuer);
        uint256 root = notes.noteRoot();
        vm.prank(pubIssuer);
        vm.expectRevert("Notes: issuerMode/cms length");
        notes.mint(hex"00", root, 12345, 0, 0, cms, mm, sig);
    }

    function test_mixedModeBatch_reverts() public {
        uint256[] memory cms = _cms(2);
        uint256[] memory mm  = new uint256[](2);
        mm[0] = notes.MODE_PUBLIC();
        mm[1] = notes.MODE_PRIVATE();
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, pubIssuer);
        uint256 root = notes.noteRoot();
        vm.prank(pubIssuer);
        vm.expectRevert("Notes: mixed issuerMode batch");
        notes.mint(hex"00", root, 12345, 0, 0, cms, mm, sig);
    }

    // ==== PRIVATE-mode (A2) path ===========================================

    function test_privateBatch_allPrivateMode_mints() public {
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, notes.MODE_PRIVATE());
        Notes.A2Binding[] memory bindings = new Notes.A2Binding[](1);
        bindings[0] = _binding();
        uint256 root = notes.noteRoot();
        vm.prank(a2Issuer);
        notes.mint(hex"00", root, 999, 0, 1000, cms, mm, bindings);
        assertEq(notes.nextLeafIndex(), 1, "private-mode mint appends");
        assertEq(notes.noteFaceSum(), 1000, "face accumulated");
    }

    function test_privateBatch_emitsIssuerReencBound() public {
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, notes.MODE_PRIVATE());
        Notes.A2Binding[] memory bindings = new Notes.A2Binding[](1);
        bindings[0] = _binding();
        uint256 root = notes.noteRoot();
        vm.recordLogs();
        vm.prank(a2Issuer);
        notes.mint(hex"00", root, 999, 0, 1000, cms, mm, bindings);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 topic = keccak256("IssuerReencBound(address,uint256,uint256)");
        bool found;
        for (uint256 i = 0; i < logs.length; i++) {
            if (logs[i].topics[0] == topic) { found = true; break; }
        }
        assertTrue(found, "IssuerReencBound emitted");
    }

    function test_privateBatch_publicIssuer_reverts() public {
        // A registered PUBLIC issuer cannot mint a PRIVATE-mode (A2) leaf.
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, notes.MODE_PRIVATE());
        Notes.A2Binding[] memory bindings = new Notes.A2Binding[](1);
        bindings[0] = _binding();
        uint256 root = notes.noteRoot();
        vm.prank(pubIssuer);
        vm.expectRevert("Notes: private-mode leaf needs private issuer");
        notes.mint(hex"00", root, 999, 0, 1000, cms, mm, bindings);
    }

    function test_privateBatch_publicModeLeaf_reverts() public {
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, notes.MODE_PUBLIC());  // wrong overload
        Notes.A2Binding[] memory bindings = new Notes.A2Binding[](1);
        bindings[0] = _binding();
        uint256 root = notes.noteRoot();
        vm.prank(a2Issuer);
        vm.expectRevert("Notes: public leaf needs Schnorr overload");
        notes.mint(hex"00", root, 999, 0, 1000, cms, mm, bindings);
    }

    function test_privateBatch_bindingCountMismatch_reverts() public {
        // Two PRIVATE leaves but only one binding -> completeness fails.
        uint256[] memory cms = _cms(2);
        uint256[] memory mm  = _modes(2, notes.MODE_PRIVATE());
        Notes.A2Binding[] memory bindings = new Notes.A2Binding[](1);
        bindings[0] = _binding();
        uint256 root = notes.noteRoot();
        vm.prank(a2Issuer);
        vm.expectRevert("Notes: A2 binding count");
        notes.mint(hex"00", root, 999, 0, 1000, cms, mm, bindings);
    }

    function test_privateBatch_badBinding_reverts() public {
        uint256[] memory cms = _cms(1);
        uint256[] memory mm  = _modes(1, notes.MODE_PRIVATE());
        Notes.A2Binding[] memory bindings = new Notes.A2Binding[](1);
        bindings[0] = _binding();
        bindings[0].proof.s_r = addmod(bindings[0].proof.s_r, 1, BN254.R);  // tamper
        uint256 root = notes.noteRoot();
        vm.prank(a2Issuer);
        vm.expectRevert("Notes: bad A2 binding");
        notes.mint(hex"00", root, 999, 0, 1000, cms, mm, bindings);
    }
}
