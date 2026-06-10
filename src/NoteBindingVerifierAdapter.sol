// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {INoteBindingVerifier} from "./INoteBindingVerifier.sol";
import {NoteBindingGroth16Verifier} from "./NoteBindingGroth16Verifier.sol";

/// @title NoteBindingVerifierAdapter — wraps the generated Groth16 verifier
///        for the note_binding circuit behind INoteBindingVerifier.
/// @notice The circuit has 25 public inputs, in order:
///           pub[0]     = nullifier
///           pub[1..4]  = eEncRx[0..3]   (64-bit little-endian F_q limbs)
///           pub[5..8]  = eEncRy[0..3]
///           pub[9..12] = eEncCx[0..3]
///           pub[13..16]= eEncCy[0..3]
///           pub[17..20]= piX[0..3]
///           pub[21..24]= piY[0..3]
///         All 25 are derived on-chain from the (nullifier, eEncRx, eEncRy,
///         eEncCx, eEncCy, piX, piY) arguments; the `proof` bytes carry ONLY
///         the Groth16 triple (a, b, c).  This is the binding: the prover
///         cannot choose the public inputs, so an accept is necessarily about
///         the caller's supplied nullifier + points.
contract NoteBindingVerifierAdapter is INoteBindingVerifier {
    NoteBindingGroth16Verifier internal immutable _verifier;

    /// @dev Groth16 triple: a(2) + b(4) + c(2) = 8 words.
    uint256 internal constant PROOF_WORDS = 8;
    uint256 internal constant MASK64 = type(uint64).max;

    constructor() {
        _verifier = new NoteBindingGroth16Verifier();
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
        require(proof.length == PROOF_WORDS * 32, "NoteBindAdapter: bad proof length");

        uint256[2]   memory a;
        uint256[2][2] memory b;
        uint256[2]   memory c;

        a[0]    = _word(proof, 0);
        a[1]    = _word(proof, 1);
        b[0][0] = _word(proof, 2);
        b[0][1] = _word(proof, 3);
        b[1][0] = _word(proof, 4);
        b[1][1] = _word(proof, 5);
        c[0]    = _word(proof, 6);
        c[1]    = _word(proof, 7);

        // Public inputs derived from the caller's arguments — the binding.
        // Each point coordinate is decomposed into 4 64-bit little-endian
        // limbs, matching the circuit's public-input layout.
        uint256[25] memory pub;
        pub[0] = nullifier;

        pub[1]  =  eEncRx         & MASK64;
        pub[2]  = (eEncRx >> 64)  & MASK64;
        pub[3]  = (eEncRx >> 128) & MASK64;
        pub[4]  = (eEncRx >> 192) & MASK64;

        pub[5]  =  eEncRy         & MASK64;
        pub[6]  = (eEncRy >> 64)  & MASK64;
        pub[7]  = (eEncRy >> 128) & MASK64;
        pub[8]  = (eEncRy >> 192) & MASK64;

        pub[9]  =  eEncCx         & MASK64;
        pub[10] = (eEncCx >> 64)  & MASK64;
        pub[11] = (eEncCx >> 128) & MASK64;
        pub[12] = (eEncCx >> 192) & MASK64;

        pub[13] =  eEncCy         & MASK64;
        pub[14] = (eEncCy >> 64)  & MASK64;
        pub[15] = (eEncCy >> 128) & MASK64;
        pub[16] = (eEncCy >> 192) & MASK64;

        pub[17] =  piX            & MASK64;
        pub[18] = (piX >> 64)     & MASK64;
        pub[19] = (piX >> 128)    & MASK64;
        pub[20] = (piX >> 192)    & MASK64;

        pub[21] =  piY            & MASK64;
        pub[22] = (piY >> 64)     & MASK64;
        pub[23] = (piY >> 128)    & MASK64;
        pub[24] = (piY >> 192)    & MASK64;

        return _verifier.verifyProof(a, b, c, pub);
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
