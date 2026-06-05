pragma solidity ^0.8.20;
import "forge-std/Test.sol";
import {IdentityMembershipG1TieVerifier} from "../src/IdentityMembershipG1TieVerifier.sol";
contract TestRegen is Test {
    IdentityMembershipG1TieVerifier v;
    string vj;
    function setUp() public {
        vj = vm.readFile("test/vectors/g1tie/proof.json");
        v = new IdentityMembershipG1TieVerifier();
    }
    function _arr(string memory k) internal view returns (uint256[] memory) {
        return vm.parseJsonUintArray(vj, k);
    }
    function _proof() internal view returns (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub) {
        uint256[] memory av = _arr(".a");
        uint256[] memory bv = _arr(".b");
        uint256[] memory cv = _arr(".c");
        uint256[] memory pv = _arr(".pub");
        a = [av[0], av[1]];
        b = [[bv[0], bv[1]], [bv[2], bv[3]]];
        c = [cv[0], cv[1]];
        for (uint i = 0; i < 9; i++) pub[i] = pv[i];
    }
    function test_validProof_verifies() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub) = _proof();
        // Log the actual values being passed
        bool result = v.verifyProof(a, b, c, pub);
        if (!result) {
            emit log_string("Verification returned false");
            emit log_named_uint("pub[0]", pub[0]);
            emit log_named_uint("a[0]", a[0]);
        }
        assertTrue(result, "must verify");
    }
    function test_zeroProof_rejected() public {
        (uint[2] memory a, uint[2][2] memory b, uint[2] memory c, uint[9] memory pub) = _proof();
        a[0] = 0; a[1] = 0; b[0][0] = 0; b[0][1] = 0; b[1][0] = 0; b[1][1] = 0; c[0] = 0; c[1] = 0;
        assertFalse(v.verifyProof(a, b, c, pub), "zero proof must be rejected");
    }
}
