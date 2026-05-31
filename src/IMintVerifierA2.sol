// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IMintVerifierA2 -- private-issuer (A2) mint-circuit verifier surface.
/// @notice The A2 mint circuit (circuits/mint_batch_a2.circom) is the
///         private-issuer variant of mint_batch: every leaf is constrained to
///         flavor == A2, the committed idHash opens to Poseidon-8(eNote, eIss),
///         and each leaf's E_iss-for-rec ciphertext is exposed as a PUBLIC
///         OUTPUT.  Notes.mint passes the eIss carried by each A2 binding as the
///         public input, so a Groth16 accept *is* the leaf-tie: the binding's
///         eIss provably equals the committed leaf's eIss (closing the floating-
///         /missing-binding collusion sub-cases -- see
///         alberta-buck-notes-decryptability.org, The Required Mint SNARK Signal).
///
///         Public signals (circom emits OUTPUTS first, then public inputs in
///         declaration order):
///           pub[0..4N)       = eIss[0..N)[0..4)  (R.x, R.y, C.x, C.y per leaf)
///           pub[4N]          = oldRoot
///           pub[4N+1]        = newRoot
///           pub[4N+2]        = nextLeafIndex
///           pub[4N+3]        = totalFace
///           pub[4N+4..5N+4)  = cm[0..N)
///         Arity 5N+4 (vs 2N+4 for the public/bearer mint_batch).
///
/// @dev    Dispatched to a per-N MintBatchA2N${N}Groth16Verifier by cms.length,
///         exactly like IMintVerifier.  `eIss.length` must equal
///         `commitments.length` (one ciphertext per committed leaf).
interface IMintVerifierA2 {
    function verifyMint(
        bytes calldata proof,
        uint256[4][] calldata eIss,
        uint256 oldRoot,
        uint256 newRoot,
        uint256 nextLeafIndex,
        uint256 totalFace,
        uint256[] calldata commitments
    ) external view returns (bool);
}
