// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {ISpendVerifier} from "./ISpendVerifier.sol";

interface ISpendGroth16 {
    /// @notice snarkjs-generated Groth16 verifier entry point for the spend
    ///         circuit.  Public-signal arity is exactly 7; changing the
    ///         spend circuit's public list requires regenerating the
    ///         verifier and updating this adapter's tuple.
    function verifyProof(
        uint256[2]    calldata a,
        uint256[2][2] calldata b,
        uint256[2]    calldata c,
        uint256[7]    calldata pubSignals
    ) external view returns (bool);
}

/// @title SpendVerifierAdapter -- ISpendVerifier over the Groth16 verifier.
/// @notice Decodes `proof` as `abi.encode(uint256[2], uint256[2][2], uint256[2])`
///         and forwards to the auto-generated verifier with public inputs
///         `[noteRoot, nullifier, face, recipient, chainId, flavor,
///         issuanceCommitment]` -- the
///         same order the spend circuit declares them.
contract SpendVerifierAdapter is ISpendVerifier {

    ISpendGroth16 public immutable verifier;

    constructor(address _verifier) {
        require(_verifier != address(0), "verifier=0");
        verifier = ISpendGroth16(_verifier);
    }

    /// @inheritdoc ISpendVerifier
    function verifySpend(
        bytes calldata proof,
        uint256 noteRoot,
        uint256 nullifier,
        uint256 face,
        address recipient,
        uint256 chainId,
        uint256 flavor,
        uint256 issuanceCommitment
    ) external view returns (bool) {
        (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c) =
            abi.decode(proof, (uint256[2], uint256[2][2], uint256[2]));

        uint256[7] memory pub;
        pub[0] = noteRoot;
        pub[1] = nullifier;
        pub[2] = face;
        pub[3] = uint256(uint160(recipient));
        pub[4] = chainId;
        pub[5] = flavor;
        pub[6] = issuanceCommitment;

        return verifier.verifyProof(a, b, c, pub);
    }
}
