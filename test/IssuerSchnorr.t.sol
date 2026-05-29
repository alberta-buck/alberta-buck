// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BN254} from "../src/BN254.sol";

/// @notice Phase 1 of the Notes mutual-decryptability fix: the public-issuer
///         Schnorr binding (IdentityRegistry.verifyIssuerSchnorr).  Constructs
///         real Schnorr signatures over the BN254 G1 generator in-test, using
///         the same Fiat-Shamir encoding as the contract, and checks
///         completeness (honest sig verifies) and soundness (every tampered
///         input is rejected).  See alberta-buck-notes-decryptability.org.
contract IssuerSchnorrTest is Test {
    IdentityRegistry reg;

    address issuer = makeAddr("issuer");
    uint256 constant SK = 0x1111111111111111111111111111111111111111111111111111111111111111;
    uint256 constant K  = 0x2222222222222222222222222222222222222222222222222222222222222222;

    function setUp() public {
        reg = new IdentityRegistry(makeAddr("gov"));
        // Bind `issuer` as a registered PUBLIC identity with pk = SK*G.
        // bindContract requires the target to be a deployed contract.
        vm.etch(issuer, hex"60006000fd");
        BN254.G1Point memory pk = BN254.mul(BN254.g1(), SK);
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1());   // placeholder credential
        reg.bindContract(issuer, pk, E, true /*isPublicIdentity*/, false /*isCarrying*/);
    }

    // ---- in-test Schnorr signer (mirrors _fsIssuerSchnorr) -----------------

    function _challenge(BN254.G1Point memory pk, BN254.G1Point memory R, bytes32 hBatch, address iss)
        internal view returns (uint256)
    {
        BN254.G1Point[] memory pts = new BN254.G1Point[](2);
        pts[0] = pk;
        pts[1] = R;
        uint256[] memory scl = new uint256[](3);
        scl[0] = uint256(hBatch);
        scl[1] = uint256(uint160(iss));
        scl[2] = block.chainid;
        return BN254.fsChallenge(pts, scl);
    }

    /// @dev Sign hBatch under secret `sk` with nonce `k`, bound to `iss`.
    function _sign(uint256 sk, uint256 k, bytes32 hBatch, address iss)
        internal view returns (IdentityRegistry.SchnorrProof memory sig)
    {
        BN254.G1Point memory pk = BN254.mul(BN254.g1(), sk);
        BN254.G1Point memory R  = BN254.mul(BN254.g1(), k);
        uint256 e = _challenge(pk, R, hBatch, iss);
        uint256 s = addmod(k, mulmod(e, sk, BN254.R), BN254.R);
        sig = IdentityRegistry.SchnorrProof(e, s, R);
    }

    // ---- completeness ------------------------------------------------------

    function test_validSignature_verifies() public view {
        bytes32 hBatch = keccak256(abi.encodePacked(uint256(0xC0FFEE), uint256(0xBEEF)));
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, hBatch, issuer);
        assertTrue(reg.verifyIssuerSchnorr(issuer, hBatch, sig), "honest signature must verify");
    }

    // ---- soundness: every tampered input is rejected -----------------------

    function test_tamperedBatch_rejected() public view {
        bytes32 hBatch = keccak256(abi.encodePacked(uint256(0xC0FFEE)));
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, hBatch, issuer);
        bytes32 other = keccak256(abi.encodePacked(uint256(0xDECAF)));
        assertFalse(reg.verifyIssuerSchnorr(issuer, other, sig), "signature must not verify for a different batch");
    }

    function test_tamperedResponse_rejected() public view {
        bytes32 hBatch = keccak256(abi.encodePacked(uint256(1)));
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, hBatch, issuer);
        sig.s = addmod(sig.s, 1, BN254.R);          // perturb the response
        assertFalse(reg.verifyIssuerSchnorr(issuer, hBatch, sig), "perturbed s must fail Check 1");
    }

    function test_wrongKey_rejected() public view {
        // Sign with a different secret than the one bound to `issuer`.
        bytes32 hBatch = keccak256(abi.encodePacked(uint256(7)));
        uint256 wrongSk = SK + 1;
        IdentityRegistry.SchnorrProof memory sig = _sign(wrongSk, K, hBatch, issuer);
        assertFalse(reg.verifyIssuerSchnorr(issuer, hBatch, sig), "signature under the wrong key must fail");
    }

    function test_nonPublicIssuer_rejected() public {
        // An issuer bound NON-public cannot satisfy the binding even with a
        // valid signature: bearer/public-issuer notes require a public Identity.
        address priv = makeAddr("privIssuer");
        vm.etch(priv, hex"60006000fd");
        BN254.G1Point memory pk = BN254.mul(BN254.g1(), SK);
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1());
        reg.bindContract(priv, pk, E, false /*isPublicIdentity*/, false);

        bytes32 hBatch = keccak256(abi.encodePacked(uint256(9)));
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, hBatch, priv);
        assertFalse(reg.verifyIssuerSchnorr(priv, hBatch, sig), "non-public issuer must be rejected");
    }

    function test_unregisteredIssuer_rejected() public view {
        address ghost = address(0xDEAD);
        bytes32 hBatch = keccak256(abi.encodePacked(uint256(3)));
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, hBatch, ghost);
        assertFalse(reg.verifyIssuerSchnorr(ghost, hBatch, sig), "unregistered issuer must be rejected");
    }

    function test_replayUnderDifferentIssuer_rejected() public {
        // A signature bound to `issuer` must not verify when presented for a
        // different public identity holding the same key (chain/issuer binding).
        address issuer2 = makeAddr("issuer2");
        vm.etch(issuer2, hex"60006000fd");
        BN254.G1Point memory pk = BN254.mul(BN254.g1(), SK);
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1());
        reg.bindContract(issuer2, pk, E, true, false);

        bytes32 hBatch = keccak256(abi.encodePacked(uint256(5)));
        IdentityRegistry.SchnorrProof memory sig = _sign(SK, K, hBatch, issuer);   // bound to `issuer`
        assertFalse(reg.verifyIssuerSchnorr(issuer2, hBatch, sig), "issuer-bound proof must not replay to issuer2");
    }
}

/// @notice Cross-artifact parity: the on-chain verifyIssuerSchnorr accepts the
///         canonical proof emitted by alberta_buck.wallet.schnorr (via
///         `emit-vectors` -> test/vectors/identity.json), pinning the Solidity
///         and Python Fiat-Shamir encodings together byte-for-byte.
contract IssuerSchnorrVectorTest is Test {
    IdentityRegistry internal reg;
    address internal constant GOV = address(0xA0);
    string  internal vj;
    address internal issuer;

    function setUp() public {
        vm.chainId(1);                          // wallet transcripts use chainid = 1
        vj  = vm.readFile("test/vectors/identity.json");
        reg = new IdentityRegistry(GOV);

        issuer = address(uint160(_u(".issuer_schnorr.issuer")));
        vm.etch(issuer, hex"60006000fd");
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT(BN254.g1(), BN254.g1());
        reg.bindContract(issuer, _g1(".issuer_schnorr.pk"), E, true, false);
    }

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }
    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }
    function _sig() internal view returns (IdentityRegistry.SchnorrProof memory s) {
        s.e = _u(".issuer_schnorr.proof.e");
        s.s = _u(".issuer_schnorr.proof.s");
        s.R = _g1(".issuer_schnorr.proof.R");
    }
    function _hBatch() internal view returns (bytes32) {
        return bytes32(_u(".issuer_schnorr.hBatch"));
    }

    function test_vector_validProof_verifies() public view {
        assertTrue(reg.verifyIssuerSchnorr(issuer, _hBatch(), _sig()),
                   "python-reference proof must verify on-chain");
    }

    function test_vector_tamperedBatch_rejected() public view {
        bytes32 bad = bytes32(uint256(_hBatch()) ^ 1);
        assertFalse(reg.verifyIssuerSchnorr(issuer, bad, _sig()));
    }

    function test_vector_tamperedResponse_rejected() public view {
        IdentityRegistry.SchnorrProof memory s = _sig();
        s.s = addmod(s.s, 1, BN254.R);
        assertFalse(reg.verifyIssuerSchnorr(issuer, _hBatch(), s));
    }

    function test_vector_wrongChainid_rejected() public {
        vm.chainId(2);                          // FS rebinds chainid -> proof rejects
        assertFalse(reg.verifyIssuerSchnorr(issuer, _hBatch(), _sig()));
    }
}
