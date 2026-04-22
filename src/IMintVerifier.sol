// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IMintVerifier -- Notes mint-circuit verifier interface (Phase 7-bis).
/// @notice The mint circuit pivot moves per-leaf Poseidon insertion *into* the
///         SNARK; the contract only verifies the proof and the stale-state
///         guards.  Public inputs are:
///           - oldRoot        : the live note root the prover read from chain
///           - newRoot        : the root after inserting cms[] starting at
///                              nextLeafIndex
///           - nextLeafIndex  : the live tree size the prover read from chain
///           - totalFace      : sum of v_i over all leaves in the batch (incl
///                              dummies, whose v_i = 0)
///           - commitments    : [cm_0, ..., cm_{N-1}] -- exposed directly as
///                              public inputs; verifier returns false if the
///                              registered Groth16 verifier for this N is not
///                              installed.
///
/// @dev    Notes dispatches to the per-N verifier via cms.length; each pinned
///         N requires its own MintBatchN${N}Groth16Verifier and an entry in
///         the adapter's verifiers mapping.
interface IMintVerifier {
    function verifyMint(
        bytes calldata proof,
        uint256 oldRoot,
        uint256 newRoot,
        uint256 nextLeafIndex,
        uint256 totalFace,
        uint256[] calldata commitments
    ) external view returns (bool);
}
