// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {INoteBindingVerifier} from "./INoteBindingVerifier.sol";
import {NoteBindingGroth16Verifier} from "./NoteBindingGroth16Verifier.sol";
import {NoteBindingA1Groth16Verifier} from "./NoteBindingA1Groth16Verifier.sol";

/// @title NoteBindingVerifierAdapter — wraps the generated Groth16 verifiers
///        for the note_binding (A2) and note_binding_a1 (A1) circuits behind
///        INoteBindingVerifier.
/// @notice The A2 circuit has 25 public inputs, in order:
///           pub[0]     = nullifier
///           pub[1..4]  = eEncRx[0..3]   (64-bit little-endian F_q limbs)
///           pub[5..8]  = eEncRy[0..3]
///           pub[9..12] = eEncCx[0..3]
///           pub[13..16]= eEncCy[0..3]
///           pub[17..20]= piX[0..3]
///           pub[21..24]= piY[0..3]
///         The A1 circuit has 26: the same layout with the note face `v`
///         inserted at pub[1] (and everything after shifted by one).
///         All public inputs are derived on-chain from the caller's
///         arguments; the `proof` bytes carry ONLY the Groth16 triple
///         (a, b, c).  This is the binding: the prover cannot choose the
///         public inputs, so an accept is necessarily about the caller's
///         supplied nullifier (+ face) + points.
contract NoteBindingVerifierAdapter is INoteBindingVerifier {
    NoteBindingGroth16Verifier   internal immutable _verifier;
    NoteBindingA1Groth16Verifier internal immutable _verifierA1;

    /// @dev Groth16 triple: a(2) + b(4) + c(2) = 8 words.
    uint256 internal constant PROOF_WORDS = 8;
    uint256 internal constant MASK64 = type(uint64).max;

    constructor() {
        _verifier   = new NoteBindingGroth16Verifier();
        _verifierA1 = new NoteBindingA1Groth16Verifier();
    }

    /// @inheritdoc INoteBindingVerifier
    /// @param proof abi-packed Groth16 triple: a[2], b[2][2], c[2] = 8 words = 256 bytes.
    function verifyNoteBinding(
        bytes calldata proof,
        uint256 nullifier,
        uint256 eEncRx,
        uint256 eEncRy,
        uint256 eEncCx,
        uint256 eEncCy,
        uint256 piX,
        uint256 piY
    ) external returns (bool) {
        (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c) =
            _triple(proof);

        // Public inputs derived from the caller's arguments — the binding.
        // Each point coordinate is decomposed into 4 64-bit little-endian
        // limbs, matching the circuit's public-input layout.
        uint256[25] memory pub;
        pub[0] = nullifier;
        _limbs(pub, 1, eEncRx, eEncRy, eEncCx, eEncCy, piX, piY);

        return _verifier.verifyProof(a, b, c, pub);
    }

    /// @inheritdoc INoteBindingVerifier
    /// @param proof abi-packed Groth16 triple: a[2], b[2][2], c[2] = 8 words = 256 bytes.
    function verifyNoteBindingA1(
        bytes calldata proof,
        uint256 nullifier,
        uint256 face,
        uint256 eEncRx,
        uint256 eEncRy,
        uint256 eEncCx,
        uint256 eEncCy,
        uint256 piX,
        uint256 piY
    ) external returns (bool) {
        (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c) =
            _triple(proof);

        uint256[26] memory pub;
        pub[0] = nullifier;
        pub[1] = face;
        _limbs(pub, 2, eEncRx, eEncRy, eEncCx, eEncCy, piX, piY);

        return _verifierA1.verifyProof(a, b, c, pub);
    }

    /// @dev Unpack the abi-packed Groth16 triple from `proof`.
    function _triple(bytes calldata proof)
        internal pure
        returns (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c)
    {
        require(proof.length == PROOF_WORDS * 32, "NoteBindAdapter: bad proof length");
        a[0]    = _word(proof, 0);
        a[1]    = _word(proof, 1);
        b[0][0] = _word(proof, 2);
        b[0][1] = _word(proof, 3);
        b[1][0] = _word(proof, 4);
        b[1][1] = _word(proof, 5);
        c[0]    = _word(proof, 6);
        c[1]    = _word(proof, 7);
    }

    /// @dev Write the six coordinates' 4x64-bit little-endian limb
    ///      decompositions into `pub[base..base+23]`.  `uint256[26]` is the
    ///      larger of the two layouts; the A2 path passes a 25-slot array via
    ///      its own copy below.
    function _limbs(
        uint256[26] memory pub,
        uint256 base,
        uint256 eEncRx,
        uint256 eEncRy,
        uint256 eEncCx,
        uint256 eEncCy,
        uint256 piX,
        uint256 piY
    ) internal pure {
        uint256[6] memory coords = [eEncRx, eEncRy, eEncCx, eEncCy, piX, piY];
        for (uint256 i = 0; i < 6; i++) {
            uint256 word = coords[i];
            for (uint256 j = 0; j < 4; j++) {
                pub[base + i * 4 + j] = (word >> (64 * j)) & MASK64;
            }
        }
    }

    /// @dev 25-slot overload for the A2 layout.
    function _limbs(
        uint256[25] memory pub,
        uint256 base,
        uint256 eEncRx,
        uint256 eEncRy,
        uint256 eEncCx,
        uint256 eEncCy,
        uint256 piX,
        uint256 piY
    ) internal pure {
        uint256[6] memory coords = [eEncRx, eEncRy, eEncCx, eEncCy, piX, piY];
        for (uint256 i = 0; i < 6; i++) {
            uint256 word = coords[i];
            for (uint256 j = 0; j < 4; j++) {
                pub[base + i * 4 + j] = (word >> (64 * j)) & MASK64;
            }
        }
    }

    /// @dev Read the `i`-th 32-byte word from a calldata bytes blob.
    function _word(bytes calldata data, uint256 i)
        internal pure returns (uint256 v)
    {
        assembly {
            v := calldataload(add(data.offset, mul(i, 32)))
        }
    }
}
