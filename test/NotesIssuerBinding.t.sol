// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {Notes} from "../src/Notes.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BN254} from "../src/BN254.sol";
import {StubMintVerifier} from "../src/StubMintVerifier.sol";
import {StubSpendVerifier} from "../src/StubSpendVerifier.sol";

/// @dev Minimal ERC-20 so the mint's buck.transferFrom succeeds, isolating the
///      issuer-binding check from Buck's identity-gated transfer machinery.
contract MockBuck {
    function transferFrom(address, address, uint256) external pure returns (bool) { return true; }
    function transfer(address, uint256) external pure returns (bool) { return true; }
}

/// @notice Phase 1 wiring: Notes.mint requires a registered PUBLIC issuer to
///         bind their Identity via the Schnorr overload (verifyIssuerSchnorr).
///         A public issuer must use the 7-arg overload with a valid signature;
///         the 6-arg overload and any bad signature revert.  Non-public minters
///         skip the binding (private A2 issuers are bound in-SNARK later).
///         See alberta-buck-notes-decryptability.org.
contract NotesIssuerBindingTest is Test {
    address constant GOV = address(0x6011);

    IdentityRegistry reg;
    Notes            notes;

    address issuer = makeAddr("pubIssuer");
    uint256 constant SK = 0x1111111111111111111111111111111111111111111111111111111111111111;
    uint256 constant K  = 0x2222222222222222222222222222222222222222222222222222222222222222;

    function setUp() public {
        reg = new IdentityRegistry(GOV);
        StubMintVerifier  mintStub  = new StubMintVerifier(GOV);   // accepts any proof
        StubSpendVerifier spendStub = new StubSpendVerifier(GOV);
        MockBuck          buck      = new MockBuck();
        notes = new Notes(address(buck), address(mintStub), address(spendStub), GOV);
        vm.prank(GOV);
        notes.setIdentityRegistry(address(reg));

        // Bind `issuer` as a registered PUBLIC identity with pk = SK*G.
        vm.etch(issuer, hex"60006000fd");
        BN254.G1Point memory pk = BN254.mul(BN254.g1(), SK);
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1());
        reg.bindContract(issuer, pk, E, true /*isPublicIdentity*/, false);
    }

    // ---- helpers -----------------------------------------------------------

    function _cms() internal pure returns (uint256[] memory cms) {
        cms = new uint256[](1);
        cms[0] = 0x1234;
    }

    /// @dev Schnorr-sign keccak256(cms) under `sk`, bound to `iss` -- mirrors
    ///      IdentityRegistry._fsIssuerSchnorr.
    function _sign(uint256 sk, uint256 k, uint256[] memory cms, address iss)
        internal view returns (IdentityRegistry.SchnorrProof memory sig)
    {
        bytes32 hBatch = keccak256(abi.encodePacked(cms));
        BN254.G1Point memory pk = BN254.mul(BN254.g1(), sk);
        BN254.G1Point memory R  = BN254.mul(BN254.g1(), k);
        BN254.G1Point[] memory pts = new BN254.G1Point[](2);
        pts[0] = pk;
        pts[1] = R;
        uint256[] memory scl = new uint256[](3);
        scl[0] = uint256(hBatch);
        scl[1] = uint256(uint160(iss));
        scl[2] = block.chainid;
        uint256 e = BN254.fsChallenge(pts, scl);
        uint256 s = addmod(k, mulmod(e, sk, BN254.R), BN254.R);
        sig = IdentityRegistry.SchnorrProof(e, s, R);
    }

    // ---- tests -------------------------------------------------------------

    function test_publicIssuer_validBinding_mints() public {
        uint256[] memory cms = _cms();
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, issuer);
        uint256 oldRoot = notes.noteRoot();
        vm.prank(issuer);
        notes.mint(hex"00", oldRoot, 12345, 0, 0, cms, sig);
        assertEq(notes.nextLeafIndex(), 1, "bound mint must append the leaf");
    }

    function test_publicIssuer_badBinding_reverts() public {
        uint256[] memory cms = _cms();
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, cms, issuer);
        sig.s = addmod(sig.s, 1, BN254.R);                  // perturb the response
        uint256 oldRoot = notes.noteRoot();
        vm.prank(issuer);
        vm.expectRevert("Notes: bad issuer binding");
        notes.mint(hex"00", oldRoot, 12345, 0, 0, cms, sig);
    }

    function test_publicIssuer_legacyOverload_reverts() public {
        // A public issuer cannot mint through the 6-arg overload: it passes a
        // zero signature, which fails verifyIssuerSchnorr.
        uint256[] memory cms = _cms();
        uint256 oldRoot = notes.noteRoot();
        vm.prank(issuer);
        vm.expectRevert("Notes: bad issuer binding");
        notes.mint(hex"00", oldRoot, 12345, 0, 0, cms);
    }

    function test_publicIssuer_signatureForOtherBatch_reverts() public {
        // Signature is valid but over a different batch -> hBatch mismatch.
        uint256[] memory cms = _cms();
        uint256[] memory other = new uint256[](1);
        other[0] = 0x9999;
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, other, issuer);
        uint256 oldRoot = notes.noteRoot();
        vm.prank(issuer);
        vm.expectRevert("Notes: bad issuer binding");
        notes.mint(hex"00", oldRoot, 12345, 0, 0, cms, sig);
    }

    function test_nonPublicIssuer_skipsBinding() public {
        // An unregistered / non-public minter mints via the 6-arg overload;
        // the binding is skipped (Phase 2 binds private issuers in-SNARK).
        address eoa = makeAddr("eoaIssuer");
        uint256[] memory cms = _cms();
        uint256 oldRoot = notes.noteRoot();
        vm.prank(eoa);
        notes.mint(hex"00", oldRoot, 12345, 0, 0, cms);
        assertEq(notes.nextLeafIndex(), 1, "unbound (non-public) mint appended");
    }
}
