// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IMintVerifier} from "./IMintVerifier.sol";

interface IMintGroth16 {
    /// @notice snarkjs-generated Groth16 verifier entry point.
    /// @dev    The third argument is fixed-length `uint256[3]` because the
    ///         mint circuit has exactly 3 public signals: `totalFace` + the
    ///         two commitments.  Changing `Mint(N)` in mint.circom requires
    ///         regenerating the verifier and updating this adapter's arity.
    function verifyProof(
        uint256[2]    calldata a,
        uint256[2][2] calldata b,
        uint256[2]    calldata c,
        uint256[3]    calldata pubSignals
    ) external view returns (bool);
}

/// @title MintVerifierAdapter — IMintVerifier over the Groth16 verifier.
/// @notice Decodes `proof` as `abi.encode(uint256[2], uint256[2][2], uint256[2])`
///         and forwards to the auto-generated verifier with public inputs
///         `[totalFace, commitments[0], commitments[1]]`.
///
/// @dev    Phase 2 mint circuit is pinned at N=2 commitments per batch.  A
///         variable-N deployment re-templates the circuit + verifier; the
///         Notes contract surface and this adapter grow a length parameter
///         at that point.
contract MintVerifierAdapter is IMintVerifier {

    uint256 public constant MINT_BATCH_SIZE = 2;

    IMintGroth16 public immutable verifier;

    constructor(address _verifier) {
        require(_verifier != address(0), "verifier=0");
        verifier = IMintGroth16(_verifier);
    }

    /// @inheritdoc IMintVerifier
    function verifyMint(
        bytes calldata proof,
        uint256 totalFace,
        uint256[] calldata commitments,
        address /*issuer*/
    ) external view returns (bool) {
        if (commitments.length != MINT_BATCH_SIZE) return false;

        (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c) =
            abi.decode(proof, (uint256[2], uint256[2][2], uint256[2]));

        uint256[3] memory pub;
        pub[0] = totalFace;
        pub[1] = commitments[0];
        pub[2] = commitments[1];

        return verifier.verifyProof(a, b, c, pub);
    }
}
