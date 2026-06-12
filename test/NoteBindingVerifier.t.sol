// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {NoteBindingVerifierAdapter} from "../src/NoteBindingVerifierAdapter.sol";
import {NoteBindingGroth16Verifier} from "../src/NoteBindingGroth16Verifier.sol";
import {NoteBindingA1Groth16Verifier} from "../src/NoteBindingA1Groth16Verifier.sol";

/// @title NoteBindingVerifierTest — on-chain verification of the note-binding
///        circuit (circuits/note_binding.circom).
/// @notice Vectors in test/vectors/note_binding/proof.json are produced by
///         scripts/snark/setup_note_binding.sh (make nix-snark-note-binding):
///         a Groth16 proof from a Python-generated witness must verify both
///         through the raw generated verifier and through the
///         INoteBindingVerifier adapter (which derives all 25 public inputs
///         on-chain from the caller's nullifier + point coordinates).
contract NoteBindingVerifierTest is Test {
    NoteBindingVerifierAdapter internal adapter;
    NoteBindingGroth16Verifier internal verifier;
    string internal vj;

    function setUp() public {
        vj = vm.readFile("test/vectors/note_binding/proof.json");
        adapter = new NoteBindingVerifierAdapter();
        verifier = new NoteBindingGroth16Verifier();
    }

    function _arr(string memory key) internal view returns (uint256[] memory) {
        return vm.parseJsonUintArray(vj, key);
    }

    function _proof()
        internal
        view
        returns (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[25] memory pub)
    {
        uint256[] memory av = _arr(".a");
        uint256[] memory bv = _arr(".b");
        uint256[] memory cv = _arr(".c");
        uint256[] memory pv = _arr(".pub");
        a = [av[0], av[1]];
        b = [[bv[0], bv[1]], [bv[2], bv[3]]];
        c = [cv[0], cv[1]];
        for (uint256 i = 0; i < 25; i++) {
            pub[i] = pv[i];
        }
    }

    /// @dev Recompose a full coordinate from 4 little-endian 64-bit limbs at
    ///      pub[off..off+3] (the inverse of the adapter's decomposition).
    function _coord(uint[25] memory pub, uint256 off) internal pure returns (uint256) {
        return pub[off] | (pub[off + 1] << 64) | (pub[off + 2] << 128) | (pub[off + 3] << 192);
    }

    /// @dev Pack the Groth16 triple into the adapter's 8-word proof blob.
    function _packed(uint[2] memory a, uint[2][2] memory b, uint[2] memory c)
        internal pure returns (bytes memory)
    {
        return abi.encodePacked(a[0], a[1], b[0][0], b[0][1], b[1][0], b[1][1], c[0], c[1]);
    }

    // ---- raw generated verifier ---------------------------------------------

    function test_proof_verifies_raw() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[25] memory pub) = _proof();
        assertTrue(verifier.verifyProof(a, b, c, pub),
                   "note-binding Groth16 proof must verify on-chain");
    }

    function test_tampered_nullifier_rejected_raw() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[25] memory pub) = _proof();
        pub[0] = pub[0] ^ 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }

    function test_tampered_eEnc_limb_rejected_raw() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[25] memory pub) = _proof();
        pub[1] = pub[1] ^ 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }

    // ---- adapter round-trip ---------------------------------------------------

    function test_proof_verifies_via_adapter() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[25] memory pub) = _proof();
        bool ok = adapter.verifyNoteBinding(
            _packed(a, b, c),
            pub[0],
            _coord(pub, 1),  _coord(pub, 5),   // eEncRx, eEncRy
            _coord(pub, 9),  _coord(pub, 13),  // eEncCx, eEncCy
            _coord(pub, 17), _coord(pub, 21)   // piX, piY
        );
        assertTrue(ok, "adapter must accept the proof for its derived publics");
    }

    function test_adapter_tampered_nullifier_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[25] memory pub) = _proof();
        bool ok = adapter.verifyNoteBinding(
            _packed(a, b, c),
            pub[0] ^ 1,
            _coord(pub, 1),  _coord(pub, 5),
            _coord(pub, 9),  _coord(pub, 13),
            _coord(pub, 17), _coord(pub, 21)
        );
        assertFalse(ok, "adapter must reject a foreign nullifier");
    }

    function test_adapter_tampered_point_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[25] memory pub) = _proof();
        bool ok = adapter.verifyNoteBinding(
            _packed(a, b, c),
            pub[0],
            _coord(pub, 1) ^ 1, _coord(pub, 5),
            _coord(pub, 9),     _coord(pub, 13),
            _coord(pub, 17),    _coord(pub, 21)
        );
        assertFalse(ok, "adapter must reject a substituted eEnc point");
    }

    // ---- proof-length gating ------------------------------------------------

    function test_emptyProof_reverts() public {
        vm.expectRevert(bytes("NoteBindAdapter: bad proof length"));
        adapter.verifyNoteBinding(
            hex"",                  // empty proof
            0xC1,                   // nullifier
            1, 2, 3, 4,             // eEncRx, eEncRy, eEncCx, eEncCy
            5, 6                    // piX, piY
        );
    }

    function test_shortProof_reverts() public {
        vm.expectRevert(bytes("NoteBindAdapter: bad proof length"));
        adapter.verifyNoteBinding(
            hex"deadbeef",          // 4 bytes — too short
            0xC1,
            1, 2, 3, 4,
            5, 6
        );
    }

    /// @dev A valid-length all-zero proof must be rejected (never accepted);
    ///      the generated verifier returns false for it.
    function test_zeroProof_rejected() public {
        bytes memory proof = new bytes(256);   // 8 words of zeros
        try adapter.verifyNoteBinding(proof, 0xC1, 1, 2, 3, 4, 5, 6)
            returns (bool ok)
        {
            assertFalse(ok, "zero proof must not verify");
        } catch {
            // Reverting on garbage input is equally fail-closed.
        }
    }
}

/// @title NoteBindingA1VerifierTest — on-chain verification of the A1-layout
///        note-binding circuit (circuits/note_binding_a1.circom).
/// @notice Vectors in test/vectors/note_binding_a1/proof.json are produced by
///         scripts/snark/setup_note_binding_a1.sh (make
///         nix-snark-note-binding-a1).  Same shape as the A2 suite, with the
///         note face `v` at pub[1] (and the coordinate limbs shifted by one).
contract NoteBindingA1VerifierTest is Test {
    NoteBindingVerifierAdapter   internal adapter;
    NoteBindingA1Groth16Verifier internal verifier;
    string internal vj;

    function setUp() public {
        vj = vm.readFile("test/vectors/note_binding_a1/proof.json");
        adapter = new NoteBindingVerifierAdapter();
        verifier = new NoteBindingA1Groth16Verifier();
    }

    function _arr(string memory key) internal view returns (uint256[] memory) {
        return vm.parseJsonUintArray(vj, key);
    }

    function _proof()
        internal
        view
        returns (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[26] memory pub)
    {
        uint256[] memory av = _arr(".a");
        uint256[] memory bv = _arr(".b");
        uint256[] memory cv = _arr(".c");
        uint256[] memory pv = _arr(".pub");
        a = [av[0], av[1]];
        b = [[bv[0], bv[1]], [bv[2], bv[3]]];
        c = [cv[0], cv[1]];
        for (uint256 i = 0; i < 26; i++) {
            pub[i] = pv[i];
        }
    }

    function _coord(uint[26] memory pub, uint256 off) internal pure returns (uint256) {
        return pub[off] | (pub[off + 1] << 64) | (pub[off + 2] << 128) | (pub[off + 3] << 192);
    }

    function _packed(uint[2] memory a, uint[2][2] memory b, uint[2] memory c)
        internal pure returns (bytes memory)
    {
        return abi.encodePacked(a[0], a[1], b[0][0], b[0][1], b[1][0], b[1][1], c[0], c[1]);
    }

    // ---- raw generated verifier ---------------------------------------------

    function test_proof_verifies_raw() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[26] memory pub) = _proof();
        assertTrue(verifier.verifyProof(a, b, c, pub),
                   "A1 note-binding Groth16 proof must verify on-chain");
    }

    function test_tampered_nullifier_rejected_raw() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[26] memory pub) = _proof();
        pub[0] = pub[0] ^ 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }

    function test_tampered_face_rejected_raw() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[26] memory pub) = _proof();
        pub[1] = pub[1] + 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }

    // ---- adapter round-trip ---------------------------------------------------

    function test_proof_verifies_via_adapter() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[26] memory pub) = _proof();
        bool ok = adapter.verifyNoteBindingA1(
            _packed(a, b, c),
            pub[0], pub[1],                    // nullifier, face
            _coord(pub, 2),  _coord(pub, 6),   // eEncRx, eEncRy
            _coord(pub, 10), _coord(pub, 14),  // eEncCx, eEncCy
            _coord(pub, 18), _coord(pub, 22)   // piX, piY
        );
        assertTrue(ok, "adapter must accept the proof for its derived publics");
    }

    /// @dev The face is the addressed-binding linchpin: the SAME proof against
    ///      any other face value must be rejected.
    function test_adapter_tampered_face_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[26] memory pub) = _proof();
        bool ok = adapter.verifyNoteBindingA1(
            _packed(a, b, c),
            pub[0], pub[1] + 1,
            _coord(pub, 2),  _coord(pub, 6),
            _coord(pub, 10), _coord(pub, 14),
            _coord(pub, 18), _coord(pub, 22)
        );
        assertFalse(ok, "adapter must reject a substituted face");
    }

    function test_adapter_tampered_nullifier_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[26] memory pub) = _proof();
        bool ok = adapter.verifyNoteBindingA1(
            _packed(a, b, c),
            pub[0] ^ 1, pub[1],
            _coord(pub, 2),  _coord(pub, 6),
            _coord(pub, 10), _coord(pub, 14),
            _coord(pub, 18), _coord(pub, 22)
        );
        assertFalse(ok, "adapter must reject a foreign nullifier");
    }

    function test_adapter_tampered_point_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[26] memory pub) = _proof();
        bool ok = adapter.verifyNoteBindingA1(
            _packed(a, b, c),
            pub[0], pub[1],
            _coord(pub, 2) ^ 1, _coord(pub, 6),
            _coord(pub, 10),    _coord(pub, 14),
            _coord(pub, 18),    _coord(pub, 22)
        );
        assertFalse(ok, "adapter must reject a substituted eEnc point");
    }

    // ---- proof-length gating ------------------------------------------------

    function test_emptyProof_reverts() public {
        vm.expectRevert(bytes("NoteBindAdapter: bad proof length"));
        adapter.verifyNoteBindingA1(
            hex"",                  // empty proof
            0xC1, 100,              // nullifier, face
            1, 2, 3, 4,             // eEncRx, eEncRy, eEncCx, eEncCy
            5, 6                    // piX, piY
        );
    }

    function test_zeroProof_rejected() public {
        bytes memory proof = new bytes(256);   // 8 words of zeros
        try adapter.verifyNoteBindingA1(proof, 0xC1, 100, 1, 2, 3, 4, 5, 6)
            returns (bool ok)
        {
            assertFalse(ok, "zero proof must not verify");
        } catch {
            // Reverting on garbage input is equally fail-closed.
        }
    }
}
