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
///           - flavor:     committed Poseidon-5 word (A1=1, A2=2, B1=3);
///                         each Notes.spendCoupled* entry point supplies its
///                         own constant so an A-opening cannot redeem via B1
///           - issuanceCommitment: the opened note commitment for B1, zero for
///                         A1/A2; binds a bearer spend to its authenticated mint
interface ISpendVerifier {
    function verifySpend(
        bytes calldata proof,
        uint256 noteRoot,
        uint256 nullifier,
        uint256 face,
        address recipient,
        uint256 chainId,
        uint256 flavor,
        uint256 issuanceCommitment
    ) external view returns (bool);
}
