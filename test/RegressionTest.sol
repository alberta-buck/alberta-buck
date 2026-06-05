// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";

// Freshly-generated verifier (from this script run)
import {RegressVerifier} from "../build/snark/regression/RegressVerifier.sol";

// Pre-existing known-working verifier
import {IdentityMembershipVerifier} from "../src/IdentityMembershipVerifier.sol";

contract RegressionTest is Test {
    // ---- Test A: Freshly-generated verifier + proof ------------------------

    function test_freshVerifier_acceptsFreshProof() public {
        string memory vj = vm.readFile("test/vectors/regression/proof.json");
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");
        assertTrue(
            new RegressVerifier().verifyProof(
                [av[0], av[1]],
                [[bv[0], bv[1]], [bv[2], bv[3]]],
                [cv[0], cv[1]],
                [pv[0]]
            ),
            "fresh verifier must accept fresh proof"
        );
    }

    function test_freshVerifier_rejectsTamperedRoot() public {
        string memory vj = vm.readFile("test/vectors/regression/proof.json");
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");
        pv[0] ^= 1; // flip identityRoot
        assertFalse(
            new RegressVerifier().verifyProof(
                [av[0], av[1]],
                [[bv[0], bv[1]], [bv[2], bv[3]]],
                [cv[0], cv[1]],
                [pv[0]]
            ),
            "fresh verifier must reject tampered root"
        );
    }

    // ---- Test B: Pre-existing known-working verifier -----------------------

    function test_knownVerifier_acceptsKnownProof() public {
        string memory vj = vm.readFile("test/vectors/identity_membership.json");
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");
        assertTrue(
            new IdentityMembershipVerifier().verifyProof(
                [av[0], av[1]],
                [[bv[0], bv[1]], [bv[2], bv[3]]],
                [cv[0], cv[1]],
                [pv[0]]
            ),
            "known-working verifier must accept known proof"
        );
    }
}
