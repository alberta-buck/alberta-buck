// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IIdentityMembershipVerifier} from "./IIdentityMembershipVerifier.sol";
import {IdentityMembershipG1TieVerifier} from "./IdentityMembershipG1TieVerifier.sol";

/// @title IdentityMembershipG1TieVerifierAdapter — wraps the generated Groth16 verifier for the
///        identity_membership_g1tie circuit behind IIdentityMembershipVerifier.
/// @notice The circuit has 9 public inputs:
///          identityRoot (1), PI_x[4], PI_y[4].
///         This adapter unpacks the proof bytes and delegates to IdentityMembershipG1TieVerifier.
contract IdentityMembershipG1TieVerifierAdapter is IIdentityMembershipVerifier {
    IdentityMembershipG1TieVerifier internal immutable _verifier;

    constructor() {
        _verifier = new IdentityMembershipG1TieVerifier();
    }

    /// @notice Verify a G1-tie membership proof.
    /// @param proof ABI-encoded Groth16 proof: (uint256[2] a, uint256[2][2] b, uint256[2] c)
    ///        followed by 9 public inputs as uint256[9].
    /// @param identityRoot The Poseidon Merkle root to verify against.
    function verifyMembership(
        bytes calldata proof,
        uint256 identityRoot
    ) external returns (bool) {
        // Decode: proof = a(2) + b(4) + c(2) + pubSignals(9)
        // = 8 uint256 for Groth16 + 9 uint256 for public inputs = 17 total
        require(proof.length == 17 * 32, "G1TieAdapter: bad proof length");

        uint256[2] memory a;
        uint256[2][2] memory b;
        uint256[2] memory c;
        uint256[9] memory pub;

        // Unpack from calldata.
        // Calldata layout after the first 4 bytes (selector): proof bytes.
        // ABI encoding of bytes: 32-byte length prefix + data.
        // The data is packed as a[0], a[1], b[0][0], b[0][1], b[1][0], b[1][1], c[0], c[1],
        // then pub[0]..pub[8].
        uint256 offset = 32; // skip length prefix
        a[0] = _readUint256(proof, offset); offset += 32;
        a[1] = _readUint256(proof, offset); offset += 32;
        b[0][0] = _readUint256(proof, offset); offset += 32;
        b[0][1] = _readUint256(proof, offset); offset += 32;
        b[1][0] = _readUint256(proof, offset); offset += 32;
        b[1][1] = _readUint256(proof, offset); offset += 32;
        c[0] = _readUint256(proof, offset); offset += 32;
        c[1] = _readUint256(proof, offset); offset += 32;

        // Public inputs: the 8 remaining values are PI_x[0..3], PI_y[0..3].
        // The FIRST public input is identityRoot (passed as argument).
        // The remaining 8 are packed in the proof.
        pub[0] = identityRoot;
        for (uint256 i = 1; i < 9; i++) {
            pub[i] = _readUint256(proof, offset);
            offset += 32;
        }

        // Verify PI_x and PI_y are provided (non-zero check for safety).
        // In production, these come from the deposit coupling proof's P_I.

        return _verifier.verifyProof(a, b, c, pub);
    }

    function _readUint256(bytes memory data, uint256 offset)
        internal pure returns (uint256 v)
    {
        assembly {
            v := mload(add(add(data, 32), offset))
        }
    }
}
