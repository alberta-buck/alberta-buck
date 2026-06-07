// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {SpendGroth16Verifier} from "../src/SpendGroth16Verifier.sol";
import {SpendVerifierAdapter} from "../src/SpendVerifierAdapter.sol";

/// @title SpendVerifier.t.sol -- cross-artifact parity for the generic spend SNARK.
/// @notice The spend.circom Groth16 proof (cm in noteRoot + a well-formed
///         nullifier) is the note-membership half that *every* Identity-M-bound
///         spend reuses: Notes.spendCoupledA1/A2/B1 all call
///         spendVerifier.verifySpend.  This suite pins the on-chain verifier to
///         the prover output -- a real proof verifies, and tampering any bound
///         public input (noteRoot, nullifier, face, recipient, chainId) is
///         rejected.  The end-to-end Notes behaviour (payout, nullifier burn,
///         double-spend) is covered by the NotesCoupled{A1,A2,B1} suites.
///
///         (Previously this drove the verifier through the generic Notes.spend /
///         spendACP entrypoints, removed in the Identity-M consolidation; it now
///         exercises the verifier directly, like G1TieVerifier.t.sol.)
contract SpendVerifierTest is Test {
    SpendVerifierAdapter internal spendAdapter;

    uint256 internal fxNoteRoot;
    uint256 internal fxNullifier;
    uint256 internal fxFace;
    address internal fxRecipient;
    uint256 internal fxChainId;
    bytes   internal fxProof;

    function setUp() public {
        spendAdapter = new SpendVerifierAdapter(address(new SpendGroth16Verifier()));

        string memory fx =
            vm.readFile("build/snark/spend/fixtures/spend_leaf0_to_bob.json");
        fxNoteRoot  = vm.parseJsonUint(fx, ".spend.public.noteRoot");
        fxNullifier = vm.parseJsonUint(fx, ".spend.public.nullifier");
        fxFace      = vm.parseJsonUint(fx, ".spend.public.face");
        fxRecipient = vm.parseJsonAddress(fx, ".spend.public.recipient");
        fxChainId   = vm.parseJsonUint(fx, ".spend.public.chainId");
        fxProof     = vm.parseJsonBytes(fx, ".spend.proofBytes");
    }

    function _verify(uint256 root, uint256 nf, uint256 face, address rec, uint256 cid)
        internal view returns (bool)
    {
        return spendAdapter.verifySpend(fxProof, root, nf, face, rec, cid);
    }

    function test_realProofVerifies() public {
        assertTrue(_verify(fxNoteRoot, fxNullifier, fxFace, fxRecipient, fxChainId),
            "real spend Groth16 proof must verify on-chain");
    }

    function test_tamperedRootRejected() public {
        assertFalse(_verify(fxNoteRoot ^ 1, fxNullifier, fxFace, fxRecipient, fxChainId));
    }

    function test_tamperedNullifierRejected() public {
        assertFalse(_verify(fxNoteRoot, fxNullifier ^ 1, fxFace, fxRecipient, fxChainId));
    }

    function test_tamperedFaceRejected() public {
        assertFalse(_verify(fxNoteRoot, fxNullifier, fxFace + 1, fxRecipient, fxChainId));
    }

    function test_tamperedRecipientRejected() public {
        assertFalse(_verify(fxNoteRoot, fxNullifier, fxFace, address(0xCAFE), fxChainId));
    }

    function test_chainIdMismatchRejected() public {
        assertFalse(_verify(fxNoteRoot, fxNullifier, fxFace, fxRecipient, fxChainId + 1));
    }
}
