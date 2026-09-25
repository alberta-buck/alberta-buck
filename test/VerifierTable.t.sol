// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {MintBatchA2N32Groth16Verifier} from "../src/MintBatchA2N32Groth16Verifier.sol";
import {MintBatchA2N32Groth16VerifierStock} from "./reference/MintBatchA2N32Groth16VerifierStock.sol";

/// @notice The table-driven rewrite of a Groth16 verifier gives the stock
///         snarkjs verifier's verdict, input for input.
///
///         WHY THERE IS A REWRITE.  snarkjs unrolls one inlined G1
///         multiply-accumulate per public input, ~166 bytes of runtime code
///         each, so mint_batch_a2 at N=32 (228 public inputs) compiles to
///         38,805 bytes -- past EIP-170's 24,576, undeployable wherever the
///         limit holds.  scripts/snark/table_verifier.py moves IC1..ICn into
///         one code-resident table and walks it in a loop.  The verification
///         key, the pairing check and every public input are unchanged, so the
///         two must agree on every proof: that is what this file holds them to,
///         on a real N=32 proof (test/vectors/mint_batch_a2_n32/proof.json) and
///         on everything reachable from it by one change.
///
///         Also here: every Groth16 verifier this repository ships fits
///         EIP-170, whatever threshold table_verifier.py was run with.
contract VerifierTableTest is Test {
    MintBatchA2N32Groth16Verifier internal table;
    MintBatchA2N32Groth16VerifierStock internal stock;

    uint256[2] internal pA;
    uint256[2][2] internal pB;
    uint256[2] internal pC;
    uint256[228] internal sig;

    uint256 internal constant N_PUB = 228;
    uint256 internal constant R =
        21888242871839275222246405745257275088548364400416034343698204186575808495617;
    uint256 internal constant EIP170 = 24_576;

    function setUp() public {
        table = new MintBatchA2N32Groth16Verifier();
        stock = new MintBatchA2N32Groth16VerifierStock();
        string memory j = vm.readFile("test/vectors/mint_batch_a2_n32/proof.json");
        uint256[] memory s = vm.parseJsonUintArray(j, ".publicSignals");
        assertEq(s.length, N_PUB, "vector has the wrong number of public signals");
        for (uint256 i = 0; i < N_PUB; i++) sig[i] = s[i];
        uint256[] memory a  = vm.parseJsonUintArray(j, ".pA");
        uint256[] memory b0 = vm.parseJsonUintArray(j, ".pB[0]");
        uint256[] memory b1 = vm.parseJsonUintArray(j, ".pB[1]");
        uint256[] memory c  = vm.parseJsonUintArray(j, ".pC");
        pA = [a[0], a[1]];
        pB = [[b0[0], b0[1]], [b1[0], b1[1]]];
        pC = [c[0], c[1]];
    }

    function _both(uint256[2] memory a, uint256[2][2] memory b, uint256[2] memory c, uint256[228] memory s)
        internal view returns (bool viaTable, bool viaStock)
    {
        viaTable = table.verifyProof(a, b, c, s);
        viaStock = stock.verifyProof(a, b, c, s);
    }

    function test_honestProof_bothAccept() public view {
        (bool t, bool s) = _both(pA, pB, pC, sig);
        assertTrue(s, "stock rejects the honest proof");
        assertTrue(t, "table rejects the honest proof");
    }

    /// Every public signal, perturbed by one: both reject.
    function test_everySignalPerturbed_bothReject() public view {
        for (uint256 i = 0; i < N_PUB; i++) {
            uint256[228] memory s = sig;
            s[i] = addmod(s[i], 1, R);
            (bool t, bool st) = _both(pA, pB, pC, s);
            assertEq(t, st, "verdicts differ");
            assertFalse(t, "a perturbed signal verified");
        }
    }

    /// The same value plus r -- equal mod r, but not a canonical field element:
    /// the field check refuses it in both, at either end of the table.
    function test_unreducedSignal_bothReject() public view {
        uint256[2] memory at = [uint256(0), N_PUB - 1];
        for (uint256 k = 0; k < 2; k++) {
            uint256[228] memory s = sig;
            s[at[k]] = s[at[k]] + R;
            (bool t, bool st) = _both(pA, pB, pC, s);
            assertEq(t, st, "verdicts differ");
            assertFalse(t, "an unreduced signal verified");
        }
    }

    function testFuzz_signal(uint256 i, uint256 v) public view {
        uint256[228] memory s = sig;
        s[i % N_PUB] = v;
        (bool t, bool st) = _both(pA, pB, pC, s);
        assertEq(t, st, "verdicts differ");
        if (v != sig[i % N_PUB]) assertFalse(t, "a changed signal verified");
    }

    function testFuzz_proof(uint256 which, uint256 v) public view {
        uint256[2] memory a = pA;
        uint256[2][2] memory b = pB;
        uint256[2] memory c = pC;
        which %= 8;
        if (which < 2) a[which] = v;
        else if (which < 6) b[(which - 2) / 2][which % 2] = v;
        else c[which - 6] = v;
        (bool t, bool st) = _both(a, b, c, sig);
        assertEq(t, st, "verdicts differ");
    }

    function test_rewriteFitsAndStockDoesNot() public view {
        assertLe(address(table).code.length, EIP170, "the table verifier exceeds EIP-170");
        assertGt(address(stock).code.length, EIP170, "the stock verifier fits: the rewrite is unneeded");
    }

    /// Every verifier the repository ships deploys under EIP-170.
    function test_everyShippedVerifierFitsEip170() public view {
        string[17] memory names = [
            "MintBatchN1Groth16Verifier", "MintBatchN2Groth16Verifier", "MintBatchN4Groth16Verifier",
            "MintBatchN8Groth16Verifier", "MintBatchN16Groth16Verifier", "MintBatchN32Groth16Verifier",
            "MintBatchA2N1Groth16Verifier", "MintBatchA2N2Groth16Verifier", "MintBatchA2N4Groth16Verifier",
            "MintBatchA2N8Groth16Verifier", "MintBatchA2N16Groth16Verifier", "MintBatchA2N32Groth16Verifier",
            "SpendGroth16Verifier", "DepositFoldA1Verifier", "DepositFoldA2Verifier",
            "IdentityMembershipB1Verifier", "Notes"
        ];
        for (uint256 i = 0; i < names.length; i++) {
            bytes memory code = vm.getDeployedCode(string.concat(names[i], ".sol:", names[i]));
            assertGt(code.length, 0, names[i]);
            assertLe(code.length, EIP170, names[i]);
        }
    }
}
