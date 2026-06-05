// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityMembershipVerifier} from "../src/IdentityMembershipVerifier.sol";

/// @notice On-chain verification of the unified Notes membership circuit
///         (circuits/identity_membership.circom): the Groth16 proof of a genuine
///         registry-Identity member, generated from the Python reference
///         IdentityTree (scripts/snark/setup_identity_membership.sh ->
///         test/vectors/identity_membership.json), verifies on chain; a tampered
///         public root is rejected.  This is the native Poseidon-Merkle half of
///         the A2 deposit coupling / B1 depositor binding gate.  See
///         alberta-buck-notes-identity-axis.org.
contract IdentityMembershipTest is Test {
    IdentityMembershipVerifier internal verifier;
    string internal vj;

    function setUp() public {
        vj = vm.readFile("test/vectors/identity_membership.json");
        verifier = new IdentityMembershipVerifier();
    }

    function _arr(string memory key) internal view returns (uint256[] memory) {
        return vm.parseJsonUintArray(vj, key);
    }

    function _proof()
        internal
        view
        returns (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[1] memory pub)
    {
        uint256[] memory av = _arr(".a");
        uint256[] memory bv = _arr(".b");
        uint256[] memory cv = _arr(".c");
        uint256[] memory pv = _arr(".pub");
        a = [av[0], av[1]];
        // snarkjs generatecall already emits b in the verifier's [[b00,b01],[b10,b11]] order.
        b = [[bv[0], bv[1]], [bv[2], bv[3]]];
        c = [cv[0], cv[1]];
        pub = [pv[0]];
    }

    function test_member_proof_verifies() public view {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[1] memory pub) = _proof();
        assertTrue(verifier.verifyProof(a, b, c, pub),
                   "Groth16 membership proof of a Python-tree member must verify on-chain");
    }

    function test_tampered_root_rejected() public view {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[1] memory pub) = _proof();
        pub[0] = pub[0] ^ 1;     // a different public root must not verify this proof
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }
}
