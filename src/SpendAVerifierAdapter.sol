// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {ISpendAVerifier} from "./ISpendAVerifier.sol";

interface ISpendAGroth16 {
    /// @notice snarkjs-generated Groth16 verifier entry point for the spend_a
    ///         V2 circuit.  Public-signal arity is exactly 9; changing the
    ///         circuit's public list requires regenerating the verifier and
    ///         updating this adapter's tuple.
    function verifyProof(
        uint256[2]    calldata a,
        uint256[2][2] calldata b,
        uint256[2]    calldata c,
        uint256[9]    calldata pubSignals
    ) external view returns (bool);
}

/// @title SpendAVerifierAdapter -- ISpendAVerifier over the Groth16 verifier.
/// @notice Decodes `proof` as `abi.encode(uint256[2], uint256[2][2], uint256[2])`
///         and forwards to the auto-generated verifier with public inputs
///         `[noteRoot, nullifier, face, recipient, chainId,
///           eNoteRx, eNoteRy, eNoteCx, eNoteCy]` -- the same order the
///         spend_a circuit declares them.
contract SpendAVerifierAdapter is ISpendAVerifier {

    ISpendAGroth16 public immutable verifier;

    constructor(address _verifier) {
        require(_verifier != address(0), "verifier=0");
        verifier = ISpendAGroth16(_verifier);
    }

    /// @inheritdoc ISpendAVerifier
    function verifySpendA(
        bytes calldata proof,
        uint256 noteRoot,
        uint256 nullifier,
        uint256 face,
        address recipient,
        uint256 chainId,
        uint256 eNoteRx,
        uint256 eNoteRy,
        uint256 eNoteCx,
        uint256 eNoteCy
    ) external view returns (bool) {
        (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c) =
            abi.decode(proof, (uint256[2], uint256[2][2], uint256[2]));

        uint256[9] memory pub;
        pub[0] = noteRoot;
        pub[1] = nullifier;
        pub[2] = face;
        pub[3] = uint256(uint160(recipient));
        pub[4] = chainId;
        pub[5] = eNoteRx;
        pub[6] = eNoteRy;
        pub[7] = eNoteCx;
        pub[8] = eNoteCy;

        return verifier.verifyProof(a, b, c, pub);
    }
}
