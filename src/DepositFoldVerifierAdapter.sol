// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "./BN254.sol";
import {IDepositFoldVerifier} from "./IDepositFoldVerifier.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";
import {DepositFoldA1Verifier} from "./DepositFoldA1Verifier.sol";
import {DepositFoldA2Verifier} from "./DepositFoldA2Verifier.sol";

/// @title DepositFoldVerifierAdapter -- wraps the generated Groth16 verifiers
///        for circuits/deposit_fold_a1.circom and deposit_fold_a2.circom.
///
/// @notice EVERY public input is derived on chain; the `proof` bytes carry only
///         the Groth16 triple.  That is the binding, and it is what makes a
///         folded accept mean something: the prover chooses none of the values
///         the relations are about.  In particular the deposit account's
///         registered key and credential are READ FROM THE REGISTRY for the
///         declared depositor, so the account relation is necessarily about an
///         account that really is registered.
///
///         Public input order, matching the circuits' `component main`:
///
///           A1 (43): nullifier, v, identityRoot,
///                    eEncRx[4], eEncRy[4], eEncCx[4], eEncCy[4],
///                    pkDepX[4], pkDepY[4],
///                    eDepRx[4], eDepRy[4], eDepCx[4], eDepCy[4]
///           A2 (42): the same without `v` -- A2 publishes no face, because
///                    its ciphertext decrypts to the issuer's Identity rather
///                    than to a value the spend pins.
///
///         Points enter as 64-bit little-endian F_q limbs, matching
///         alberta_buck.wallet.deposit_fold._limbs and the circuits' Recompose4
///         (x = l0 + l1*2^64 + l2*2^128 + l3*2^192).
contract DepositFoldVerifierAdapter is IDepositFoldVerifier {
    DepositFoldA1Verifier internal immutable _a1;
    DepositFoldA2Verifier internal immutable _a2;
    IdentityRegistry      internal immutable _registry;

    uint256 internal constant PROOF_WORDS = 8;
    uint256 internal constant MASK64 = type(uint64).max;
    uint256 internal constant N_PUB_A1 = 43;
    uint256 internal constant N_PUB_A2 = 42;

    constructor(IdentityRegistry registry_) {
        require(address(registry_) != address(0), "FoldAdapter: zero registry");
        _registry = registry_;
        _a1 = new DepositFoldA1Verifier();
        _a2 = new DepositFoldA2Verifier();
    }

    /// @inheritdoc IDepositFoldVerifier
    function verifyFoldA1(
        bytes calldata proof,
        uint256 nullifier,
        uint256 face,
        uint256 identityRoot,
        IdentityRegistry.ElGamalCT calldata eEnc,
        address depositor
    ) external returns (bool) {
        uint256[] memory flat = _publics(nullifier, face, true, identityRoot, eEnc, depositor);
        require(flat.length == N_PUB_A1, "FoldAdapter: A1 public input count");

        uint256[43] memory pub;
        for (uint256 i = 0; i < N_PUB_A1; i++) {
            pub[i] = flat[i];
        }
        (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c) = _triple(proof);
        return _a1.verifyProof(a, b, c, pub);
    }

    /// @inheritdoc IDepositFoldVerifier
    function verifyFoldA2(
        bytes calldata proof,
        uint256 nullifier,
        uint256 identityRoot,
        IdentityRegistry.ElGamalCT calldata eEnc,
        address depositor
    ) external returns (bool) {
        uint256[] memory flat = _publics(nullifier, 0, false, identityRoot, eEnc, depositor);
        require(flat.length == N_PUB_A2, "FoldAdapter: A2 public input count");

        uint256[42] memory pub;
        for (uint256 i = 0; i < N_PUB_A2; i++) {
            pub[i] = flat[i];
        }
        (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c) = _triple(proof);
        return _a2.verifyProof(a, b, c, pub);
    }

    // ---- internals -------------------------------------------------------

    /// @dev Assemble the public inputs in circuit order.  `withFace` is the
    ///      only structural difference between the two layouts.
    function _publics(
        uint256 nullifier,
        uint256 face,
        bool    withFace,
        uint256 identityRoot,
        IdentityRegistry.ElGamalCT calldata eEnc,
        address depositor
    ) internal view returns (uint256[] memory flat) {
        flat = new uint256[](withFace ? N_PUB_A1 : N_PUB_A2);
        uint256 at;
        flat[at++] = nullifier;
        if (withFace) {
            flat[at++] = face;
        }
        flat[at++] = identityRoot;

        at = _limbs(flat, at, eEnc.R.X);
        at = _limbs(flat, at, eEnc.R.Y);
        at = _limbs(flat, at, eEnc.C.X);
        at = _limbs(flat, at, eEnc.C.Y);

        // The depositor's REGISTERED key and credential.  Reading these rather
        // than accepting them is the point: a prover free to supply them could
        // satisfy the account relation with an account nobody registered.
        BN254.G1Point memory pk = _registry.pkOf(depositor);
        IdentityRegistry.ElGamalCT memory E = _registry.ciphertextOf(depositor);
        at = _limbs(flat, at, pk.X);
        at = _limbs(flat, at, pk.Y);
        at = _limbs(flat, at, E.R.X);
        at = _limbs(flat, at, E.R.Y);
        at = _limbs(flat, at, E.C.X);
        at = _limbs(flat, at, E.C.Y);
        require(at == flat.length, "FoldAdapter: layout");
    }

    /// @dev Four 64-bit little-endian limbs of one F_q coordinate.
    function _limbs(uint256[] memory flat, uint256 at, uint256 v)
        internal pure returns (uint256)
    {
        flat[at]     =  v         & MASK64;
        flat[at + 1] = (v >> 64)  & MASK64;
        flat[at + 2] = (v >> 128) & MASK64;
        flat[at + 3] = (v >> 192) & MASK64;
        return at + 4;
    }

    function _triple(bytes calldata proof)
        internal pure
        returns (uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c)
    {
        require(proof.length == PROOF_WORDS * 32, "FoldAdapter: bad proof length");
        a[0]    = _word(proof, 0);
        a[1]    = _word(proof, 1);
        b[0][0] = _word(proof, 2);
        b[0][1] = _word(proof, 3);
        b[1][0] = _word(proof, 4);
        b[1][1] = _word(proof, 5);
        c[0]    = _word(proof, 6);
        c[1]    = _word(proof, 7);
    }

    function _word(bytes calldata data, uint256 i)
        internal pure returns (uint256 v)
    {
        assembly {
            v := calldataload(add(data.offset, mul(i, 32)))
        }
    }
}
