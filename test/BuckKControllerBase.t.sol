// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckKControllerBaseHarness} from "./harness/BuckKControllerBaseHarness.sol";

/// @title BuckKControllerBaseTest -- Layer 1 unit tests for the abstract
///        PID core.  Covers sign convention, anti-windup, dTMax clamp,
///        constructor prime, and reprime semantics independently of any
///        concrete subclass's oracle wiring.
contract BuckKControllerBaseTest is Test {

    address GOV = makeAddr("governance");

    BuckKControllerBaseHarness internal ctrl;

    function setUp() public {
        ctrl = new BuckKControllerBaseHarness(
            0.1e18, 0.01e18, 0.00001e18,
            3600,
            0.50e18, 1.50e18,
            1.0e18,
            GOV
        );
    }

    // ---- Sign convention canaries -------------------------------------- //

    function test_sign_inflation_contracts_credit() public {
        // BUCK undervalued: buckValue < basketValue -> error < 0 -> buckK
        // contracts with positive Kp.
        ctrl.setReferences(0.95e18, 1.0e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertLt(k, 1.0e18, "buckK must contract on inflation");
        assertLt(ctrl.P(), 0, "P sign wrong on inflation");
    }

    function test_sign_deflation_expands_credit() public {
        ctrl.setReferences(1.05e18, 1.0e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertGt(k, 1.0e18, "buckK must expand on deflation");
        assertGt(ctrl.P(), 0, "P sign wrong on deflation");
    }

    // ---- Constructor prime --------------------------------------------- //

    function test_constructor_primes_I_for_steady_state() public {
        // Deploy with non-neutral initial buckK; verify I is pre-loaded.
        BuckKControllerBaseHarness fresh = new BuckKControllerBaseHarness(
            0.1e18, 0.01e18, 0,
            3600,
            0.50e18, 1.50e18,
            1.20e18,
            GOV
        );
        // I = ((1.20e18 - 1e18) * 1e18) / 0.01e18 = 20e18
        assertApproxEqRel(fresh.I(), 20e18, 0.0001e18);
        assertEq(fresh.P(), 0);
        assertEq(fresh.D(), 0);
        assertTrue(fresh.primed());
    }

    function test_first_compute_at_parity_reproduces_buckK() public {
        BuckKControllerBaseHarness fresh = new BuckKControllerBaseHarness(
            0.1e18, 0.01e18, 0,
            3600,
            0.50e18, 1.50e18,
            1.20e18,
            GOV
        );
        // Hold at parity.
        fresh.setReferences(1.0e18, 1.0e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = fresh.compute();
        assertApproxEqRel(k, 1.20e18, 0.0001e18,
            "first compute at parity must reproduce initial buckK");
    }

    // ---- Anti-windup --------------------------------------------------- //

    function test_anti_windup_ceiling() public {
        // Deep deflation -> output saturates at buckKMax.
        ctrl.setReferences(2.0e18, 1.0e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertEq(k, 1.50e18);
    }

    function test_anti_windup_floor() public {
        // Deep inflation -> output saturates at buckKMin.
        ctrl.setReferences(0.10e18, 1.0e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertEq(k, 0.50e18);
    }

    // ---- dTMax clamp --------------------------------------------------- //

    function test_dTMax_clamps_long_gap() public {
        vm.prank(GOV);
        ctrl.setGains(0.1e18, 0.01e18, 0);     // disable D
        vm.prank(GOV);
        ctrl.setDTMax(3600);

        ctrl.setReferences(0.99e18, 1.0e18);    // small inflation
        vm.warp(block.timestamp + 24 hours);
        ctrl.compute();
        // Without clamp, 24h * sustained -0.01 error would push buckK to a
        // rail.  With clamp it dips just below 1.0.
        assertLt(ctrl.buckK(), 1.0e18);
        assertGt(ctrl.buckK(), 0.90e18);
    }

    // ---- Reprime ------------------------------------------------------- //

    function test_reprime_recaptures_state() public {
        // Drive a real cycle to move buckK off neutral.
        ctrl.setReferences(0.90e18, 1.0e18);
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        uint256 kBeforeReprime = ctrl.buckK();
        int256  pBeforeReprime = ctrl.P();
        assertLt(pBeforeReprime, 0);

        // Step references; reprime; verify P captures new error and the
        // next no-error cycle reproduces the post-reprime buckK.
        ctrl.setReferences(1.0e18, 1.0e18);
        ctrl.harness_reprime();
        assertEq(ctrl.P(), 0, "reprime did not capture current error");

        vm.warp(block.timestamp + 3601);
        uint256 kAfter = ctrl.compute();
        assertApproxEqRel(kAfter, kBeforeReprime, 0.001e18,
            "first cycle after reprime did not reproduce buckK");
    }

    // ---- fundingFactor ------------------------------------------------- //

    function test_funding_factor_at_parity_is_unit() public {
        ctrl.setReferences(1.0e18, 1.0e18);
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        assertApproxEqRel(ctrl.fundingFactor(), 1e18, 0.0001e18);
    }

    function test_funding_factor_rises_on_inflation() public {
        ctrl.setReferences(0.95e18, 1.0e18);   // BUCK 5% under basket
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        // factor = 1 + 10 * (basket - buck) / basket = 1 + 10 * 0.05 = 1.5
        assertApproxEqRel(ctrl.fundingFactor(), 1.5e18, 0.001e18);
    }

    function test_funding_factor_saturates_at_zero_on_deflation() public {
        ctrl.setReferences(1.20e18, 1.0e18);   // BUCK 20% over basket
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        // factor = 1 + 10 * (1 - 1.2) / 1 = -1, clamped to 0
        assertEq(ctrl.fundingFactor(), 0);
    }

    // ---- setRails ------------------------------------------------------- //

    function test_set_rails_only_governance() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        ctrl.setRails(0.4e18, 1.6e18);
    }

    function test_set_rails_rejects_inverted() public {
        vm.prank(GOV);
        vm.expectRevert("min>max");
        ctrl.setRails(1.6e18, 0.4e18);
    }

    function test_set_rails_clamps_live_buckK() public {
        // buckK sits at 1.0; a new ceiling below it must clamp the output.
        vm.prank(GOV);
        ctrl.setRails(0.50e18, 0.90e18);
        assertEq(ctrl.buckK(), 0.90e18, "buckK not clamped to new max");
        assertEq(ctrl.buckKMax(), 0.90e18);
        // And a floor above it clamps upward.
        vm.prank(GOV);
        ctrl.setRails(0.95e18, 1.50e18);
        assertEq(ctrl.buckK(), 0.95e18, "buckK not clamped to new min");
    }

    // ---- retune (bumpless transfer) -------------------------------------- //

    function test_retune_only_governance() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        ctrl.retune(0.1e18, 0.02e18, 0);
    }

    function test_retune_requires_Ki() public {
        vm.prank(GOV);
        vm.expectRevert("retune needs Ki");
        ctrl.retune(0.1e18, 0, 0);
    }

    function test_retune_is_bumpless_where_setGains_steps() public {
        // Start at buckK = 1.20 (constructor primes I = 20e18 with Ki=0.01).
        BuckKControllerBaseHarness fresh = new BuckKControllerBaseHarness(
            0.1e18, 0.01e18, 0,
            3600,
            0.50e18, 1.50e18,
            1.20e18,
            GOV
        );
        fresh.setReferences(1.0e18, 1.0e18);   // hold parity (error 0)

        // Raw setGains doubling Ki would step the output:
        //   I*Ki_new/UNIT = 20e18*0.02 = 0.40e18  (vs 0.20e18 before).
        // retune() must re-derive I so the output is continuous.
        vm.prank(GOV);
        fresh.retune(0.1e18, 0.02e18, 0);
        vm.warp(block.timestamp + 3601);
        uint256 k = fresh.compute();
        assertApproxEqRel(k, 1.20e18, 0.001e18,
            "retune stepped the output (not bumpless)");
    }

    function test_setGains_steps_output_control_case() public {
        // The contrast case for the bumpless test above: an identical gain
        // change through raw setGains DOES step the output.
        BuckKControllerBaseHarness fresh = new BuckKControllerBaseHarness(
            0.1e18, 0.01e18, 0,
            3600,
            0.50e18, 1.50e18,
            1.20e18,
            GOV
        );
        fresh.setReferences(1.0e18, 1.0e18);
        vm.prank(GOV);
        fresh.setGains(0.1e18, 0.02e18, 0);
        vm.warp(block.timestamp + 3601);
        uint256 k = fresh.compute();
        assertApproxEqRel(k, 1.40e18, 0.001e18,
            "expected the raw setGains step (I unchanged, Ki doubled)");
    }
}
