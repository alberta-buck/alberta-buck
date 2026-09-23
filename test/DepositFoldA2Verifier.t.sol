// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {DepositFoldA2Verifier} from "../src/DepositFoldA2Verifier.sol";

/// @notice On-chain verification of the folded A2 deposit gate
///         (circuits/deposit_fold_a2.circom): one proof over one witness,
///         carrying FIVE relations.
///
///         Read the A1 sibling's header for why the gate is folded at all.
///         A2 carries one relation more, and every difference traces to a
///         single fact: its ciphertext decrypts to the ISSUER's Identity, a
///         point the spender holds no scalar for.
///
///         That fifth relation is the membership of the decrypted point.
///         Without it a colluding issuer keys the note to a throwaway instead
///         of the recipient's mailbox -- the anti-framing binding still
///         passes, since it only forces the ciphertext over the issuer's own
///         registered Identity -- and the recipient is left holding garbage
///         that no receipt can name.  A1 needs no such relation: its plaintext
///         is the recipient's own Identity, which the leaf already commits.
///
///         It also means A2 cannot fold its point sums into scalar sums, so it
///         performs curve addition where A1 performs none, and enforces the
///         incomplete gadget's precondition instead of avoiding the operation.
///
///         42 public inputs rather than A1's 43: A2 publishes no face.
///         See doc/review/notes-receiving-key.org sections 3.3a and 4.4.
contract DepositFoldA2VerifierTest is Test {
    DepositFoldA2Verifier internal verifier;
    string internal vj;

    uint256 internal constant N_PUB = 42;

    function setUp() public {
        vj = vm.readFile("test/vectors/deposit_fold_a2/proof.json");
        verifier = new DepositFoldA2Verifier();
    }

    function _proof()
        internal
        view
        returns (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[42] memory pub)
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
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[42] memory pub) = _proof();
        assertTrue(verifier.verifyProof(a, b, c, pub),
                   "folded A2 deposit-gate proof must verify on-chain");
    }

    /// @notice Every public input is load-bearing: the gate's whole purpose is
    ///         to bind these together, so moving any one of them must break
    ///         the proof.  Nothing here should be free to vary.
    function test_every_public_input_is_bound() public {
        for (uint256 i = 0; i < N_PUB; i++) {
            (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[42] memory pub) = _proof();
            pub[i] = pub[i] ^ 1;
            assertFalse(verifier.verifyProof(a, b, c, pub),
                        "a tampered public input must not verify");
        }
    }

    function test_tampered_proof_point_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[42] memory pub) = _proof();
        c[0] = c[0] ^ 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }
}
