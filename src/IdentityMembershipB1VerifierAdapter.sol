// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IIdentityMembershipVerifier} from "./IIdentityMembershipVerifier.sol";
import {IdentityMembershipB1Verifier} from "./IdentityMembershipB1Verifier.sol";

/// @title IdentityMembershipB1VerifierAdapter — wraps the generated Groth16
///        verifier for circuits/identity_membership_b1.circom behind
///        IIdentityMembershipVerifier.
///
/// @notice A drop-in replacement for the G1-tie adapter: the same nine public
///         inputs in the same order, so governance migrates B1 by pointing
///         `Notes.identityMembershipVerifier` here.  What differs is entirely
///         inside the circuit, and all three of review finding 5's defects are
///         repaired there rather than inherited:
///
///           * the blind's point T is COMPUTED from the blind rather than
///             witnessed, so it is a multiple of H by construction, where the
///             G1-tie circuit let a prover choose it freely;
///           * the generator is H_PEDERSEN, hashed to the curve, so there is
///             no known logarithm to shift the blind along -- without which a
///             depositor holding any registered identity scalar could make
///             this proof speak about that identity while its sigma spoke
///             about its own, and an UNREGISTERED depositor would spend;
///           * the witnessed limbs are range-checked, and the one curve
///             addition asserts its addends' x-coordinates differ.
///
///         The leaf is the salted one, where G1-tie hashed coordinates alone.
///         See alberta_buck/wallet/nums.py and doc/review/notes-receiving-key.org.
/// @notice The circuit has 9 public inputs, in order:
///           pub[0]     = identityRoot
///           pub[1..4]  = PI_x[0..3]   (64-bit little-endian F_q limbs)
///           pub[5..8]  = PI_y[0..3]
///         All nine are derived on-chain from the (identityRoot, px, py) arguments;
///         the `proof` bytes carry ONLY the Groth16 triple (a, b, c).  This is the
///         binding: the prover cannot choose the public inputs, so a membership
///         accept is necessarily about the caller's committed point P_I = (px, py).
contract IdentityMembershipB1VerifierAdapter is IIdentityMembershipVerifier {
    IdentityMembershipB1Verifier internal immutable _verifier;

    /// @dev Groth16 triple: a(2) + b(4) + c(2) = 8 words.
    uint256 internal constant PROOF_WORDS = 8;
    uint256 internal constant MASK64 = type(uint64).max;

    constructor() {
        _verifier = new IdentityMembershipB1Verifier();
    }

    /// @inheritdoc IIdentityMembershipVerifier
    /// @param proof abi-packed Groth16 triple: a[2], b[2][2], c[2] = 8 words = 256 bytes.
    function verifyMembership(
        bytes calldata proof,
        uint256 identityRoot,
        uint256 px,
        uint256 py
    ) external returns (bool) {
        require(proof.length == PROOF_WORDS * 32, "B1Adapter: bad proof length");

        uint256[2] memory a;
        uint256[2][2] memory b;
        uint256[2] memory c;

        a[0]    = _word(proof, 0);
        a[1]    = _word(proof, 1);
        b[0][0] = _word(proof, 2);
        b[0][1] = _word(proof, 3);
        b[1][0] = _word(proof, 4);
        b[1][1] = _word(proof, 5);
        c[0]    = _word(proof, 6);
        c[1]    = _word(proof, 7);

        // Public inputs derived from the caller's committed point — the binding.
        // PI_x / PI_y are the 64-bit little-endian limbs of the F_q coordinates,
        // matching scripts/snark/gen_g1tie_input.py:to_limbs and the circuit's
        // PI_x = PI_x[0] + PI_x[1]·2^64 + PI_x[2]·2^128 + PI_x[3]·2^192.
        uint256[9] memory pub;
        pub[0] = identityRoot;
        pub[1] =  px            & MASK64;
        pub[2] = (px >> 64)     & MASK64;
        pub[3] = (px >> 128)    & MASK64;
        pub[4] = (px >> 192)    & MASK64;
        pub[5] =  py            & MASK64;
        pub[6] = (py >> 64)     & MASK64;
        pub[7] = (py >> 128)    & MASK64;
        pub[8] = (py >> 192)    & MASK64;

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
