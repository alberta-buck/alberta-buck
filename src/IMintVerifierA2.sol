// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IMintVerifierA2 -- private-issuer (A2) mint-circuit verifier surface.
/// @notice The A2 mint circuit (circuits/mint_batch_a2.circom) is the
///         private-issuer variant of mint_batch: every leaf is constrained to
///         flavor == A2, the committed idHash opens to Poseidon-10(eNote, eIss,
///         T), and each leaf's E_iss-for-rec ciphertext and its binding's T are
///         exposed as PUBLIC OUTPUTS.  Notes.mint passes the eIss and T carried
///         by each A2 binding as those public inputs, so a Groth16 accept *is*
///         the leaf-tie: the binding's eIss and T provably equal the committed
///         leaf's.  The eIss match closes the floating- and missing-binding
///         collusion cases (alberta-buck-notes.org, "The Non-Deniable-Receipt
///         Invariant"); the T match is what lets the spend tie the binding's
///         key to the recipient's own (doc/review/notes-receiving-key.org 4.6).
///
///         Public signals (circom emits OUTPUTS first, in declaration order,
///         then the public inputs in declaration order):
///           pub[0..4N)       = eIss[0..N)[0..4)  (R.x, R.y, C.x, C.y per leaf)
///           pub[4N..6N)      = T[0..N)[0..2)     (x, y per leaf)
///           pub[6N]          = oldRoot
///           pub[6N+1]        = newRoot
///           pub[6N+2]        = nextLeafIndex
///           pub[6N+3]        = totalFace
///           pub[6N+4..7N+4)  = cm[0..N)
///         Arity 7N+4 (vs 2N+4 for the public/bearer mint_batch).
///
/// @dev    Dispatched to a per-N MintBatchA2N${N}Groth16Verifier by cms.length,
///         exactly like IMintVerifier.  `eIss.length` and `T.length` must equal
///         `commitments.length` (one of each per committed leaf).
interface IMintVerifierA2 {
    function verifyMint(
        bytes calldata proof,
        uint256[4][] calldata eIss,
        uint256[2][] calldata T,
        uint256 oldRoot,
        uint256 newRoot,
        uint256 nextLeafIndex,
        uint256 totalFace,
        uint256[] calldata commitments
    ) external view returns (bool);
}
