// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IdentityRegistry} from "./IdentityRegistry.sol";

/// @title IDepositFoldVerifier -- the folded deposit gate for addressed Notes.
/// @notice One proof over one witness, replacing the three co-bound checks the
///         addressed spend used to make (the coupling sigma, the P-bound
///         membership proof, and the note<->eEnc tie).
///
///         WHY ONE PROOF.  An addressed Note is keyed to the recipient's
///         receiving key, while its authority belongs to the recipient's
///         Identity -- two different secrets.  Proving "I can read this note"
///         and "I am this registered Identity" as separate statements says
///         nothing about their owner: a thief holding a stolen payload
///         satisfies the reading half with the stolen key and the Identity
///         half with its own registered Identity, both true, neither joining
///         them.  The fold states the tie as a relation over shared private
///         witnesses instead of inferring it from a shared public point.
///
///         See doc/review/notes-receiving-key.org section 3.3a.
interface IDepositFoldVerifier {
    /// @notice Verify a folded A1 spend.  A1 publishes its face, which pins
    ///         the note's value ciphertext plaintext.
    /// @param proof   abi-packed Groth16 triple a[2], b[2][2], c[2] (8 words).
    /// @param nullifier  the spent note's nullifier, shared with the spend SNARK.
    /// @param face    the spend's public face.
    /// @param identityRoot  the posted aggregator root.
    /// @param eEnc    the ciphertext the spend supplies.
    /// @param depositor  the payout account; its registered key and credential
    ///        are read from the registry, never supplied by the prover.
    function verifyFoldA1(
        bytes calldata proof,
        uint256 nullifier,
        uint256 face,
        uint256 identityRoot,
        IdentityRegistry.ElGamalCT calldata eEnc,
        address depositor
    ) external returns (bool);

    /// @notice Verify a folded A2 spend.  A2 publishes no face: its ciphertext
    ///         decrypts to the ISSUER's Identity, and the circuit carries a
    ///         fifth relation proving that point is itself registered.
    function verifyFoldA2(
        bytes calldata proof,
        uint256 nullifier,
        uint256 identityRoot,
        IdentityRegistry.ElGamalCT calldata eEnc,
        address depositor
    ) external returns (bool);
}
