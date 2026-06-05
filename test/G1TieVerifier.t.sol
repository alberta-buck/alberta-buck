// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityMembershipG1TieVerifier} from "../src/IdentityMembershipG1TieVerifier.sol";

/// @notice On-chain verification of the G1-tie membership circuit
///         (circuits/identity_membership_g1tie.circom): a Groth16 proof
///         generated from a Python-generated witness verifies on chain;
///         a tampered public root is rejected.
///         See alberta-buck-notes-identity-axis.org.
contract IdentityMembershipG1TieVerifierTest is Test {
    IdentityMembershipG1TieVerifier internal verifier;
    string internal vj;

    function setUp() public {
        vj = vm.readFile("test/vectors/g1tie/proof.json");
        verifier = new IdentityMembershipG1TieVerifier();
    }

    function _arr(string memory key) internal view returns (uint256[] memory) {
        return vm.parseJsonUintArray(vj, key);
    }

    function _proof()
        internal
        view
        returns (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub)
    {
        uint256[] memory av = _arr(".a");
        uint256[] memory bv = _arr(".b");
        uint256[] memory cv = _arr(".c");
        uint256[] memory pv = _arr(".pub");
        a = [av[0], av[1]];
        b = [[bv[0], bv[1]], [bv[2], bv[3]]];
        c = [cv[0], cv[1]];
        for (uint256 i = 0; i < 9; i++) {
            pub[i] = pv[i];
        }
    }

    function test_proof_verifies() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub) = _proof();
        assertTrue(verifier.verifyProof(a, b, c, pub),
                   "G1-tie Groth16 proof must verify on-chain");
    }

    function test_tampered_root_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub) = _proof();
        pub[0] = pub[0] ^ 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }

    function test_tampered_PI_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub) = _proof();
        pub[1] = pub[1] ^ 1;
        assertFalse(verifier.verifyProof(a, b, c, pub));
    }
}
