// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title ISpendVerifier -- Notes spend-circuit verifier interface.
/// @notice Public-input layout for the spend circuit, fixed so the Notes
///         contract surface is independent of which Groth16/PLONK verifier
///         binary backs it.
///
/// @dev    Public inputs (per `circuits/spend.circom`):
///           - noteRoot:   accumulator root the prover claims membership against
///           - nullifier:  Poseidon-3 spend tag, burned on first use
///           - face:       BUCK face value being released to `recipient`
///           - recipient:  address (uint160-packed) that receives the BUCK
///           - chainId:    block.chainid at proving time, replay protection
interface ISpendVerifier {
    function verifySpend(
        bytes calldata proof,
        uint256 noteRoot,
        uint256 nullifier,
        uint256 face,
        address recipient,
        uint256 chainId
    ) external view returns (bool);
}
