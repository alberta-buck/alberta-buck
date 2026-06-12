// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityMembershipG1TieVerifierAdapter} from "../src/IdentityMembershipG1TieVerifierAdapter.sol";

/// @notice The P_I binding: the adapter derives the G1-tie circuit's 8 public
///         limb-inputs from the *caller-supplied* committed point (px, py) — the
///         deposit-coupling sigma's `dc.P_I` — instead of trusting the prover's
///         proof bytes.  So a Groth16 accept proves membership of exactly that
///         point; a colluding pair cannot answer the coupling with one P_I and
///         the membership with another.
///
///         The fixture (test/vectors/g1tie/proof.json) carries the canonical
///         proof and its 9 public signals [root, PI_x[0..3], PI_y[0..3]].  This
///         test reconstructs P_I from those limbs, passes it as (px, py), and
///         asserts: the matching point verifies; any perturbation of the point
///         or the root fails.  No new proving is needed — the binding is a pure
///         consequence of the on-chain limb decomposition.
contract IdentityMembershipBindingTest is Test {
    IdentityMembershipG1TieVerifierAdapter internal adapter;
    string internal vj;

    uint256 internal root;
    uint256 internal piX;
    uint256 internal piY;
    bytes   internal proofBytes; // abi-packed (a[2], b[2][2], c[2]) = 8 words

    function setUp() public {
        vj = vm.readFile("test/vectors/g1tie/proof.json");
        adapter = new IdentityMembershipG1TieVerifierAdapter();

        uint256[] memory a = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory b = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory c = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pub = vm.parseJsonUintArray(vj, ".pub");

        // proof bytes = a(2) + b(4) + c(2), matching the adapter's word order.
        proofBytes = abi.encode(a[0], a[1], b[0], b[1], b[2], b[3], c[0], c[1]);

        // pub = [root, PI_x[0..3], PI_y[0..3]] with 64-bit little-endian limbs.
        root = pub[0];
        piX = pub[1] + (pub[2] << 64) + (pub[3] << 128) + (pub[4] << 192);
        piY = pub[5] + (pub[6] << 64) + (pub[7] << 128) + (pub[8] << 192);
    }

    /// The committed point reconstructed from the vector's limbs verifies — i.e.
    /// the adapter's on-chain limb decomposition reproduces the circuit's inputs.
    function test_boundPoint_verifies() public {
        assertTrue(
            adapter.verifyMembership(proofBytes, root, piX, piY),
            "membership must verify for the point the proof was made over"
        );
    }

    /// Passing a *different* committed point (the collusion / mismatch case)
    /// fails: the prover cannot supply a P_I unrelated to the proof, because the
    /// adapter — not the prover — chooses the public inputs from (px, py).
    function test_mismatchedPI_x_rejected() public {
        assertFalse(
            adapter.verifyMembership(proofBytes, root, piX ^ 1, piY),
            "a different P_I.x must not verify against this proof"
        );
    }

    function test_mismatchedPI_y_rejected() public {
        assertFalse(
            adapter.verifyMembership(proofBytes, root, piX, piY ^ 1),
            "a different P_I.y must not verify against this proof"
        );
    }

    /// A different identity root (e.g. before the issuer registered) fails.
    function test_wrongRoot_rejected() public {
        assertFalse(
            adapter.verifyMembership(proofBytes, root ^ 1, piX, piY),
            "a different identityRoot must not verify"
        );
    }

    /// Malformed proof length is rejected explicitly.
    function test_badProofLength_reverts() public {
        bytes memory short = hex"deadbeef";
        vm.expectRevert(bytes("G1TieAdapter: bad proof length"));
        adapter.verifyMembership(short, root, piX, piY);
    }
}
