// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {DepositFoldA1Verifier} from "../src/DepositFoldA1Verifier.sol";

/// @notice On-chain verification of the folded A1 deposit gate
///         (circuits/deposit_fold_a1.circom): one proof over one witness,
///         carrying all four relations that let an addressed Note be spent.
///
///         WHY IT IS ONE PROOF.  An addressed Note is keyed to the recipient's
///         receiving key but its authority belongs to the recipient's Identity,
///         and those are two different secrets.  A gate proving "I can read
///         this note" and "I am this registered Identity" side by side would
///         state nothing about their owner: a thief holding a stolen payload
///         supplies the reading half with the stolen key and the Identity half
///         with its own registered Identity, both true, neither joining them.
///         Folding states the tie instead of inferring it.
///
///         The public inputs are the note ciphertext the spend supplies, the
///         deposit account's registered key and credential, the posted
///         identity root, the nullifier and the face.  Every identity stays
///         private.  See doc/review/notes-receiving-key.org section 3.3a.
contract DepositFoldA1VerifierTest is Test {
    DepositFoldA1Verifier internal verifier;
    string internal vj;

    uint256 internal constant N_PUB = 43;

    function setUp() public {
        vj = vm.readFile("test/vectors/deposit_fold_a1/proof.json");
        verifier = new DepositFoldA1Verifier();
    }

    function _proof()
        internal
        view
        returns (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[43] memory pub)
    {
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");
        a = [av[0], av[1]];
        b = [[bv[0], bv[1]], [bv[2], bv[3]]];
        c = [cv[0], cv[1]];
        for (uint256 i = 0; i < N_PUB; i++) {
            pub[i] = pv[i];
        }
    }

    function test_proof_verifies() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[43] memory pub) = _proof();
        assertTrue(verifier.verifyProof(a, b, c, pub),
                   "folded A1 deposit-gate proof must verify on-chain");
    }

    /// @notice Every public input is load-bearing: the gate's whole purpose is
    ///         to bind these together, so moving any one of them must break
    ///         the proof.  Nothing here should be free to vary.
    function test_every_public_input_is_bound() public {
        for (uint256 i = 0; i < N_PUB; i++) {
            (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[43] memory pub) = _proof();
            pub[i] = pub[i] ^ 1;
            assertFalse(verifier.verifyProof(a, b, c, pub),
                        "a tampered public input must not verify");
        }
    }

    function test_tampered_proof_point_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[43] memory pub) = _proof();
        c[0] = c[0] ^ 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }
}
