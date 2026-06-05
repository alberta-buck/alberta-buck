// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IIdentityMembershipVerifier — identity Merkle membership proof interface.
/// @notice Verifies a zero-knowledge proof that an identity point M is a member
///         of the registry-Identity accumulator under a given root.  The proof
///         is a Groth16 SNARK (circuits/identity_membership.circom); the
///         verifier is an adapter over the generated Groth16 verifier contract.
///
///         Public inputs (current circuit, native half only):
///           - identityRoot: the Poseidon Merkle root of registered identities
///
///         Pending G1-tie extension (the "one remaining circuit"):
///           - commitmentData: the EC commitment point (P_I for A2, eDepForIss
///             coords for B1) that the circuit proves ties the Merkle leaf to
///             the on-chain sigma's committed point.
///
/// @dev    The interface is intentionally minimal — the governance slot in
///         Notes.sol can be upgraded from a stub to the real verifier without
///         changing the spend-path logic.  See alberta-buck-notes-identity-axis.org.
interface IIdentityMembershipVerifier {
    /// @notice Verify that a committed identity point is a member of the
    ///         registry-Identity accumulator.
    /// @param proof The Groth16 proof bytes (256 bytes).
    /// @param identityRoot The Poseidon Merkle root to verify against.
    /// @return True iff the proof is valid against the given root.
    function verifyMembership(
        bytes calldata proof,
        uint256 identityRoot
    ) external returns (bool);
}
