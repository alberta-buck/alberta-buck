// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IIdentityMembershipVerifier — identity Merkle membership proof interface.
/// @notice Verifies a zero-knowledge proof that the issuer identity point M
///         committed (blinded) in a deposit's `P_I = M + b*H` is a member of the
///         registry-Identity accumulator under a given root.  The proof is a
///         Groth16 SNARK (circuits/identity_membership_g1tie.circom); the verifier
///         is an adapter over the generated Groth16 verifier contract.
///
///         Public inputs of the G1-tie circuit (arity 9):
///           - identityRoot          : the Poseidon Merkle root of registered identities
///           - PI_x[4], PI_y[4]       : the committed point P_I as 64-bit F_q limbs,
///                                      little-endian (PI_x = Σ PI_x[k]·2^(64k)).
///
///         THE BINDING.  `P_I` is NOT read from the prover-supplied proof bytes;
///         the adapter derives the 8 limbs from the on-chain (px, py) the caller
///         passes — which the deposit path takes from the deposit-coupling sigma's
///         `DepositCouplingProof.P_I`.  So the Groth16 accept proves membership of
///         the SAME point the coupling sigma decrypted `eIss` to; a colluding pair
///         cannot answer the coupling with one P_I and the membership with another.
///         The proof bytes therefore carry only the Groth16 (a, b, c) = 8 words.
///
/// @dev    The interface is intentionally minimal and free of the BN254 import —
///         the governance slot in Notes.sol can be upgraded from a stub to the
///         real verifier without changing the spend-path logic.  See
///         alberta-buck-notes.org and alberta-buck-notes-flow.org (the unified one-gadget / Identity-M model; see also historical unilateral for original A2).
interface IIdentityMembershipVerifier {
    /// @notice Verify that the committed identity point P_I = (px, py) is a member
    ///         of the registry-Identity accumulator under `identityRoot`.
    /// @param proof The Groth16 proof bytes: abi-packed (uint256[2] a,
    ///        uint256[2][2] b, uint256[2] c) = 8 words = 256 bytes.  No public
    ///        inputs are carried in the proof — they are derived from the
    ///        arguments below, which is what binds the membership to the caller.
    /// @param identityRoot The Poseidon Merkle root to verify against.
    /// @param px The committed point's F_q x-coordinate (P_I.X from the coupling).
    /// @param py The committed point's F_q y-coordinate (P_I.Y from the coupling).
    /// @return True iff the proof is valid for (identityRoot, px, py).
    function verifyMembership(
        bytes calldata proof,
        uint256 identityRoot,
        uint256 px,
        uint256 py
    ) external returns (bool);
}
