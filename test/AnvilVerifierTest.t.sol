pragma solidity ^0.8.20;
import "forge-std/Test.sol";
import {IdentityMembershipG1TieVerifier} from "../src/IdentityMembershipG1TieVerifier.sol";
contract AnvilVerifierTest is Test {
    function test_deployAndVerify() public {
        IdentityMembershipG1TieVerifier v = new IdentityMembershipG1TieVerifier();
        string memory vj = vm.readFile("test/vectors/g1tie/proof.json");
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");

        uint[2] memory a = [av[0], av[1]];
        uint[2][2] memory b = [[bv[0], bv[1]], [bv[2], bv[3]]];
        uint[2] memory c = [cv[0], cv[1]];
        uint[9] memory pub;
        for (uint i = 0; i < 9; i++) pub[i] = pv[i];

        // Call verifyProof - forge will show detailed gas/trace on failure
        bool result = v.verifyProof(a, b, c, pub);
        emit log_named_string("verifyProof", result ? "true" : "false");
        assertTrue(result, "valid proof must verify");
    }
}
