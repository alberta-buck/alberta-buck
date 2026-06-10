// SPDX-License-Identifier: GPL-3.0
/*
    PLACEHOLDER — auto-generated Groth16 verifier for note_binding.circom.

    This file will be replaced by the output of:
        snarkjs zkey export solidityverifier <zkey> NoteBindingGroth16Verifier.sol

    The real verifier has 25 public inputs (nullifier + 6 coords × 4 limbs).
    Until the trusted setup is run (scripts/snark/setup_note_binding.sh), this
    stub provides the expected interface so the adapter contract compiles.

    See: INoteBindingVerifier.sol, NoteBindingVerifierAdapter.sol
*/

pragma solidity >=0.7.0 <0.9.0;

contract NoteBindingGroth16Verifier {
    /// @notice Verify a Groth16 proof with 25 public inputs.
    /// @dev    The real verifier performs the pairing check; this stub always
    ///         returns false (fail-closed) so tests using the adapter reject by
    ///         default until a real trusted setup is run.
    function verifyProof(
        uint[2]        calldata,  // _pA
        uint[2][2]     calldata,  // _pB
        uint[2]        calldata,  // _pC
        uint[25]       calldata   // _pubSignals
    ) public pure returns (bool) {
        // Stub: fail-closed.  Replace with the real auto-generated verifier
        // after running scripts/snark/setup_note_binding.sh.
        return false;
    }
}
