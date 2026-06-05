pragma solidity ^0.8.20;
import "forge-std/Test.sol";
import {TestV} from "../src/TestV.sol";
contract TestVTest is Test {
    function testVerify() public {
        string memory vj = vm.readFile("test/vectors/regen/proof.json");
        uint256[] memory av = vm.parseJsonUintArray(vj, ".a");
        uint256[] memory bv = vm.parseJsonUintArray(vj, ".b");
        uint256[] memory cv = vm.parseJsonUintArray(vj, ".c");
        uint256[] memory pv = vm.parseJsonUintArray(vj, ".pub");
        assertTrue(new TestV().verifyProof(
            [av[0],av[1]], [[bv[0],bv[1]],[bv[2],bv[3]]], [cv[0],cv[1]], [pv[0]]));
    }
}
