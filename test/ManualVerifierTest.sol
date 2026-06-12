// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;
import "forge-std/Test.sol";
import {ManualVerifier} from "../src/ManualVerifier.sol";

contract ManualVerifierTest is Test {
    function test_manual_verifies() public {
        string memory vj = vm.readFile("test/vectors/manual/proof.json");
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");
        assertTrue(new ManualVerifier().verifyProof(
            [av[0],av[1]], [[bv[0],bv[1]],[bv[2],bv[3]]], [cv[0],cv[1]], [pv[0]]));
    }
}
