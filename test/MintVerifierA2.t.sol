// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {MintBatchA2N1Groth16Verifier} from "../src/MintBatchA2N1Groth16Verifier.sol";
import {MintBatchA2N2Groth16Verifier} from "../src/MintBatchA2N2Groth16Verifier.sol";
import {MintVerifierA2Adapter}        from "../src/MintVerifierA2Adapter.sol";

/// @title MintVerifierA2.t.sol -- cross-artifact parity for the private-issuer
///        (A2) mint circuit.  Loads a mint_batch_a2 proof fixture
///        (scripts/snark/prove_mint_batch_a2.js) and checks it verifies through
///        MintVerifierA2Adapter + the real per-N Groth16 verifier, and that a
///        tampered eIss public input fails the pairing check (the leaf-tie is
///        load-bearing -- you cannot pass a different eIss than the one the
///        prover committed).
contract MintVerifierA2Test is Test {

    MintBatchA2N1Groth16Verifier internal g1;
    MintBatchA2N2Groth16Verifier internal g2;
    MintVerifierA2Adapter        internal adapter;

    address internal constant GOV = address(0xA2);

    function setUp() public {
        g1      = new MintBatchA2N1Groth16Verifier();
        g2      = new MintBatchA2N2Groth16Verifier();
        adapter = new MintVerifierA2Adapter(GOV);
        vm.startPrank(GOV);
        adapter.registerVerifier(1, address(g1));
        adapter.registerVerifier(2, address(g2));
        vm.stopPrank();
    }

    struct Fx {
        uint256      n;
        uint256[4][] eIss;
        uint256[2][] T;
        uint256      oldRoot;
        uint256      newRoot;
        uint256      nextLeafIndex;
        uint256      totalFace;
        uint256[]    cms;
        bytes        proof;
    }

    function _load(string memory path) internal view returns (Fx memory fx) {
        string memory j = vm.readFile(path);
        fx.n             = vm.parseJsonUint(j, ".N");
        fx.oldRoot       = vm.parseJsonUint(j, ".public.oldRoot");
        fx.newRoot       = vm.parseJsonUint(j, ".public.newRoot");
        fx.nextLeafIndex = vm.parseJsonUint(j, ".public.nextLeafIndex");
        fx.totalFace     = vm.parseJsonUint(j, ".public.totalFace");
        fx.cms           = new uint256[](fx.n);
        fx.eIss          = new uint256[4][](fx.n);
        fx.T             = new uint256[2][](fx.n);
        for (uint256 i = 0; i < fx.n; i++) {
            fx.cms[i] = vm.parseJsonUint(j, string.concat(".public.cm[", vm.toString(i), "]"));
            for (uint256 k = 0; k < 4; k++) {
                fx.eIss[i][k] = vm.parseJsonUint(
                    j, string.concat(".public.eIss[", vm.toString(i), "][", vm.toString(k), "]"));
            }
            for (uint256 k = 0; k < 2; k++) {
                fx.T[i][k] = vm.parseJsonUint(
                    j, string.concat(".public.T[", vm.toString(i), "][", vm.toString(k), "]"));
            }
        }
        fx.proof = vm.parseJsonBytes(j, ".proofBytes");
    }

    function _verify(Fx memory fx) internal view returns (bool) {
        return adapter.verifyMint(
            fx.proof, fx.eIss, fx.T, fx.oldRoot, fx.newRoot, fx.nextLeafIndex, fx.totalFace, fx.cms);
    }

    function test_a2_basic_N1_verifies() public view {
        Fx memory fx = _load("build/snark/mint_batch_a2_n1/fixtures/basic.json");
        assertEq(fx.n, 1, "N=1");
        assertTrue(_verify(fx), "A2 N=1 proof verifies through adapter");
    }

    function test_a2_basic_N2_verifies() public view {
        Fx memory fx = _load("build/snark/mint_batch_a2_n2/fixtures/basic.json");
        assertEq(fx.n, 2, "N=2");
        assertTrue(_verify(fx), "A2 N=2 proof verifies through adapter");
    }

    function test_a2_tamperedEIss_fails() public view {
        Fx memory fx = _load("build/snark/mint_batch_a2_n2/fixtures/basic.json");
        assertTrue(_verify(fx), "baseline verifies");
        fx.eIss[0][0] ^= 1;              // flip one eIss word
        assertFalse(_verify(fx), "tampered eIss fails the pairing check");
    }

    function test_a2_tamperedT_fails() public view {
        Fx memory fx = _load("build/snark/mint_batch_a2_n2/fixtures/basic.json");
        fx.T[1][1] ^= 1;                 // the leaf commits T; a binding's other T fails
        assertFalse(_verify(fx), "tampered T fails the pairing check");
    }

    function test_a2_tamperedCm_fails() public view {
        Fx memory fx = _load("build/snark/mint_batch_a2_n2/fixtures/basic.json");
        fx.cms[1] ^= 1;
        assertFalse(_verify(fx), "tampered cm fails the pairing check");
    }

    function test_a2_unregisteredN_returnsFalse() public view {
        // No N=4 A2 verifier registered -> adapter returns false.
        Fx memory fx = _load("build/snark/mint_batch_a2_n2/fixtures/basic.json");
        uint256[4][] memory eIss4 = new uint256[4][](4);
        uint256[2][] memory T4    = new uint256[2][](4);
        uint256[]    memory cms4  = new uint256[](4);
        assertFalse(adapter.verifyMint(
            fx.proof, eIss4, T4, fx.oldRoot, fx.newRoot, fx.nextLeafIndex, fx.totalFace, cms4));
    }

    function test_a2_adapter_governance() public {
        MintVerifierA2Adapter a = new MintVerifierA2Adapter(GOV);
        vm.expectRevert(bytes("not governance"));
        a.registerVerifier(1, address(g1));
        vm.prank(GOV);
        a.registerVerifier(1, address(g1));
        assertEq(a.verifiers(1), address(g1));
    }
}
