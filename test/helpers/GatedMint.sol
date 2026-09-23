// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../../src/BN254.sol";
import {IdentityRegistry} from "../../src/IdentityRegistry.sol";

/// @title GatedMint -- test helper for the gated-only Notes mint surface.
/// @notice Notes minting is gated-only: a PUBLIC (A1/B1) batch needs a
///         registered public issuer + a Schnorr over keccak256(cms); a PRIVATE
///         (A2) batch needs a private issuer + per-leaf re-encryption bindings.
///         These helpers build the PUBLIC-path arguments for tests whose mint is
///         scaffolding (seed a tree, exercise a guard) rather than the subject.
///         The Schnorr transcript mirrors IdentityRegistry._fsIssuerSchnorr.
library GatedMint {

    /// @dev all-MODE_PUBLIC issuerMode[] of length n.
    function allPublic(uint256 n) internal pure returns (uint256[] memory mm) {
        mm = new uint256[](n);
        for (uint256 i = 0; i < n; i++) mm[i] = 1; // Notes.MODE_PUBLIC
    }

    /// @dev Schnorr-sign keccak256(cms) under issuer key `sk` (nonce `k`),
    ///      Fiat-Shamir bound to (issuer, chainid) -- the public-issuer binding.
    function signPublic(
        uint256 sk,
        uint256 k,
        uint256[] memory cms,
        address issuer,
        uint256 chainid
    ) internal view returns (IdentityRegistry.SchnorrProof memory sig) {
        return signWith(BN254.mul(BN254.g1(), sk), BN254.mul(BN254.g1(), k),
                        sk, k, cms, issuer, chainid);
    }

    /// @dev Same as signPublic, but with `pk = sk*G` and `R = k*G` PRECOMPUTED.
    ///      This variant does NO ecMul (only keccak + mulmod), so it makes no
    ///      external precompile call -- safe to evaluate inside a mint argument
    ///      list after vm.prank / vm.expectRevert (which the ecMul staticcall
    ///      would otherwise consume / trip).  Callers precompute pk, R in setUp.
    function signWith(
        BN254.G1Point memory pk,
        BN254.G1Point memory R,
        uint256 sk,
        uint256 k,
        uint256[] memory cms,
        address issuer,
        uint256 chainid
    ) internal pure returns (IdentityRegistry.SchnorrProof memory sig) {
        BN254.G1Point[] memory pts = new BN254.G1Point[](2);
        pts[0] = pk;
        pts[1] = R;
        uint256[] memory scl = new uint256[](4);
        scl[0] = uint256(keccak256(abi.encodePacked(cms)));
        scl[1] = uint256(uint160(issuer));
        scl[2] = chainid;
        scl[3] = uint256(keccak256("AlbertaBuck/FiatShamir/IdentityRegistry/IssuerSchnorr/v2"));
        uint256 e = BN254.fsChallenge(pts, scl);
        uint256 s = addmod(k, mulmod(e, sk, BN254.R), BN254.R);
        sig = IdentityRegistry.SchnorrProof(e, s, R);
    }
}
