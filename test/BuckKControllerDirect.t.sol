// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckKControllerDirect} from "../src/BuckKControllerDirect.sol";
import {MockBasket}            from "./mocks/MockBasket.sol";

/// @title BuckKControllerDirectTest -- Layer 3 unit tests for the
///        USD-free direct-embodiment PID controller.
contract BuckKControllerDirectTest is Test {

    address GOV = makeAddr("governance");

    // Rescaled ppm gains (real_gain * 1e12).  Kp_real=0.5, Ki_real~2e-5,
    // Kd_real=0 -- the same integral-dominant defaults the sim deploys.
    int256 constant KP = 5e11;
    int256 constant KI = 2e7;
    int256 constant KD = 0;

    BuckKControllerDirect internal ctrl;
    MockBasket            internal basket;

    function setUp() public {
        ctrl = new BuckKControllerDirect(
            KP, KI, KD,
            3600,
            0.50e18, 1.50e18,
            1.0e18,
            GOV
        );
        basket = new MockBasket();
        basket.setController(address(ctrl));
        basket.setBasketValue(int256(1e18));     // start at parity
        vm.prank(GOV);
        ctrl.setBasket(address(basket));
    }

    function test_set_basket_only_governance() public {
        BuckKControllerDirect fresh = new BuckKControllerDirect(
            KP, KI, KD, 3600, 0.50e18, 1.50e18, 1.0e18, GOV
        );
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        fresh.setBasket(address(basket));
    }

    function test_set_basket_one_shot() public {
        vm.prank(GOV);
        vm.expectRevert("basket already set");
        ctrl.setBasket(address(makeAddr("other")));
    }

    function test_pre_setBasket_returns_steady_state_at_unit() public {
        BuckKControllerDirect fresh = new BuckKControllerDirect(
            KP, KI, KD, 3600, 0.50e18, 1.50e18, 1.20e18, GOV
        );
        // No basket wired -> _readReferences returns (UNIT, UNIT) -> error 0.
        vm.warp(block.timestamp + 3601);
        uint256 k = fresh.compute();
        assertApproxEqRel(k, 1.20e18, 0.0001e18);
    }

    // ---- Sign convention canaries (direct embodiment) ------------------- //

    function test_direct_inflation_contracts_credit() public {
        // basket appreciated 5% above 1.0 BUCK -> BUCK is undervalued
        // (inflation).  error = 1.0 - 1.05 = -0.05.  Expect buckK < 1.0.
        basket.setBasketValue(int256(1.05e18));
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertLt(k, 1.0e18, "direct: inflation must contract credit");
    }

    function test_direct_deflation_expands_credit() public {
        // basket depreciated 5% below 1.0 BUCK -> BUCK overvalued (deflation).
        // error = 1.0 - 0.95 = +0.05.  Expect buckK > 1.0.
        basket.setBasketValue(int256(0.95e18));
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertGt(k, 1.0e18, "direct: deflation must expand credit");
    }

    // ---- Reprime ------------------------------------------------------- //

    function test_reprime_only_basket() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("only basket");
        ctrl.reprime();
    }

    function test_reprime_absorbs_step() public {
        // Drive a cycle to move buckK off neutral.
        basket.setBasketValue(int256(1.05e18));
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        uint256 kBefore = ctrl.buckK();

        // Step the process variable (simulating addBasketToken's dilution).
        basket.setBasketValue(int256(1.0e18));
        basket.callReprime();
        // After reprime, next no-error cycle should reproduce kBefore.
        vm.warp(block.timestamp + 3601);
        uint256 kAfter = ctrl.compute();
        assertApproxEqRel(kAfter, kBefore, 0.001e18);
    }

    // ---- fundingFactor ------------------------------------------------- //

    function test_funding_factor_direct_inflation() public {
        // basket at 1.05 BUCK -> factor = 1 + 10 * (1.05 - 1) / 1.05 ~ 1.476
        basket.setBasketValue(int256(1.05e18));
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        uint256 f = ctrl.fundingFactor();
        assertGt(f, 1.4e18);
        assertLt(f, 1.5e18);
    }

    function test_funding_factor_direct_deflation_saturates() public {
        basket.setBasketValue(int256(0.85e18));
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        // factor = 1 + 10 * (0.85 - 1)/0.85 ~ -0.76 -> clamped 0
        assertEq(ctrl.fundingFactor(), 0);
    }

    // ---- retune (bumpless, ppm algebra) --------------------------------- //

    function test_retune_bumpless_ppm() public {
        // Wind the loop off neutral with a sustained 5% deflation.
        basket.setBasketValue(int256(0.95e18));
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        uint256 kBefore = ctrl.buckK();
        assertGt(kBefore, 1.0e18);

        // Quadruple Ki through retune: the output must NOT step.  (Raw
        // setGains would multiply the whole uI contribution by 4.)
        vm.prank(GOV);
        ctrl.retune(KP, KI * 4, KD);

        // Next cycle, process unchanged: the only movement is the fresh
        // integral increment Ki*err*dt (= 4*2e7 * 50_000ppm * 3601s
        // ~ 0.0144e18), NOT a 4x re-scale of the wound integral.
        vm.warp(block.timestamp + 3601);
        uint256 kAfter = ctrl.compute();
        assertApproxEqRel(kAfter, kBefore, 0.02e18,
            "retune stepped the ppm output (not bumpless)");
    }

    // ---- setBuckK0 ------------------------------------------------------- //

    function test_set_buckK0_only_governance() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        ctrl.setBuckK0(0.8e18);
    }

    function test_set_buckK0_bounds() public {
        vm.prank(GOV);
        vm.expectRevert("buckK0 out of bounds");
        ctrl.setBuckK0(0.4e18);          // below buckKMin = 0.5
    }

    function test_set_buckK0_bumpless() public {
        // Wind the loop off neutral, then move the feed-forward: the live
        // buckK must be unchanged at the setter and (to within one integral
        // increment) across the next cycle.
        basket.setBasketValue(int256(0.95e18));
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        uint256 kBefore = ctrl.buckK();

        vm.prank(GOV);
        ctrl.setBuckK0(0.80e18);
        assertEq(ctrl.buckK(), kBefore, "setter itself moved buckK");
        assertEq(ctrl.buckK0(), 0.80e18);

        vm.warp(block.timestamp + 3601);
        uint256 kAfter = ctrl.compute();
        assertApproxEqRel(kAfter, kBefore, 0.02e18,
            "setBuckK0 stepped the output (not bumpless)");
    }
}
