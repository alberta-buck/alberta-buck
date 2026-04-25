// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title ISpendAVerifier -- Notes A-flavor spend-circuit verifier interface.
/// @notice Phase 8 V2 separates the A-flavor SNARK from the B-flavor SNARK in
///         public-input arity: A-spend's V2 circuit binds the publicly
///         revealed note ciphertext (E_n = (R_n, C_n)) into the leaf via
///         Poseidon-8(eNoteR, eNoteC, issuerData[4]) === idHash.  Those four
///         coordinates ride alongside the (root, nullifier, face, recipient,
///         chainId) tuple as additional public inputs so the on-chain
///         CP-DLEQ verifier and the SNARK agree on which ciphertext is in
///         scope.
///
/// @dev    Public inputs (per `circuits/spend_a.circom` V2):
///           - noteRoot:   accumulator root the prover claims membership against
///           - nullifier:  Poseidon-3 spend tag, A-domain (4243), burned on first use
///           - face:       BUCK face value being released to `recipient`
///           - recipient:  address (uint160-packed) that receives the BUCK
///           - chainId:    block.chainid at proving time, replay protection
///           - eNoteRx, eNoteRy, eNoteCx, eNoteCy: BN254 G1 coords of E_n,
///             bound to the leaf via the (I) Poseidon-8 idHash gate.
interface ISpendAVerifier {
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
    ) external view returns (bool);
}
