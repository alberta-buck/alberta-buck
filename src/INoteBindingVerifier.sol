// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title INoteBindingVerifier — the note<->eEnc re-encryption tie.
/// @notice Verifies, in zero knowledge, that the deposit-coupling ciphertext
///         `eEnc` supplied at spend is a *re-encryption*, under the recipient
///         Identity point M_rec, of the issuer/recipient ciphertext that the
///         spent note committed in its `idHash` — without revealing `idHash`,
///         the committed ciphertext, or the re-randomization scalar.
///
///         WHY THIS EXISTS.  The flavor-agnostic spend proof (spend.circom)
///         proves only that *some* note opening is in the tree and that its
///         nullifier is well-formed; it does NOT bind the coupling's `eEnc` to
///         that note.  Without this tie, the addressed (A1/A2) guarantees the
///         docs claim are NOT enforced on-chain:
///           * "only the recipient Identity M_rec can spend an A1/A2 note" —
///             any registered account holding the opening can redeem it by
///             supplying its OWN self-addressed `eEnc`; and
///           * "an un-nameable A2 note is un-spendable" — a colluding pair can
///             spend a note whose committed `eIss` is un-nameable by attaching
///             a different, nameable `eEnc`.
///         This verifier closes both by binding `eEnc` to the specific spent
///         note.
///
///         WHY A HASH-EQUALITY CHECK IS NOT ENOUGH.  `Notes.mint` exposes each
///         leaf's `eIss` as a Groth16 public input (it is visible in the mint
///         calldata).  If the spend re-used the exact committed `eIss`, an
///         observer could match it to the mint tx and link spend->mint->issuer,
///         destroying A2's mint<->spend unlinkability.  The depositor must
///         therefore RE-RANDOMIZE `eEnc`, so the relation to prove is
///         re-encryption *equivalence*, not equality — hence a SNARK, not an
///         on-chain Poseidon compare (Poseidon-4/8 is not an EVM precompile).
///
///         THE RELATION (circuits/note_binding.circom, ~2.4M non-linear
///         constraints; all EC operations fixed-base via the ElGamal-structure
///         optimization).
///         Public:  nullifier, eEnc=(R,C), P_I=(piX,piY)  (25 signals: the six
///                  point coordinates as 4x64-bit limbs).
///         Private: rho, idHash, eNote, eIssCommitted=(R0,C0), s (re-rand
///                  scalar), r (ElGamal randomness), b (P_I blind), m_rec
///                  (recipient identity scalar), M_I (decrypted issuer point).
///         Constraints:
///           (1) nullifier      = Poseidon3(rho, idHash, TAG)        // ties to THE note
///           (2) idHash         = Poseidon8(eNote, eIssCommitted)     // opens idHash
///           (3) eEnc.R = R0 + s*G,  eEnc.C = C0 + (s*m_rec)*G        // re-encryption under M_rec
///           (4) R0 = r*G,  C0 = M_I + (r*m_rec)*G,  P_I = M_I + b*H  // ElGamal structure + committed point
///         (1) reuses the nullifier the spend SNARK already attests, so the tie
///         is to the SPECIFIC spent note without revealing `idHash`; (4) shares
///         m_rec / P_I with `IdentityRegistry.verifyDepositCoupling`, so the two
///         halves cannot be answered with different points.
///
///         IMPLEMENTATION.  The generated Groth16 verifier is
///         `NoteBindingGroth16Verifier` behind `NoteBindingVerifierAdapter`
///         (which derives all 25 public inputs on chain from the caller's
///         nullifier / eEnc / P_I; the proof bytes carry only the Groth16
///         triple).  `StubNoteBindingVerifier` remains for plumbing tests; a
///         deployment where governance has not wired the real verifier leaves
///         the addressed-binding and A2-collusion guarantees unenforced.  See
///         alberta-buck-notes.org ("Status"), alberta-buck-proofs.org
///         (Theorem 12), and alberta-buck-verifier-implementation.org
///         (toolchain).
///
/// @dev    Import-free (no BN254 / IdentityRegistry types) so the governance
///         slot in Notes.sol can swap a stub for the real verifier without
///         touching the spend-path logic.  Coordinates are passed as raw
///         uint256 base-field words, matching the on-chain ElGamal layout.
interface INoteBindingVerifier {
    /// @notice Verify the re-encryption tie binding `eEnc` to the spent note.
    /// @param proof   Groth16 proof bytes for the reserved tie circuit.
    /// @param nullifier The spent note's nullifier (Poseidon3(rho, idHash, tag));
    ///        the public handle the spend SNARK already bound to (rho, idHash),
    ///        so the proof is tied to THIS note without revealing idHash.
    /// @param eEncRx  eEnc.R.x  (the deposit-coupling ciphertext's R coords)
    /// @param eEncRy  eEnc.R.y
    /// @param eEncCx  eEnc.C.x
    /// @param eEncCy  eEnc.C.y
    /// @param piX     coupling commitment P_I.x (shares the m_rec witness)
    /// @param piY     coupling commitment P_I.y
    /// @return True iff `eEnc` re-encrypts the note's committed ciphertext under M_rec.
    function verifyNoteBinding(
        bytes calldata proof,
        uint256 nullifier,
        uint256 eEncRx,
        uint256 eEncRy,
        uint256 eEncCx,
        uint256 eEncCy,
        uint256 piX,
        uint256 piY
    ) external returns (bool);
}
