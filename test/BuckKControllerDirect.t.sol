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
        // basket at 1.05 BUCK -> factor = 0.5 + 25 * (1.05 - 1) / 1.05 ~ 1.690
        basket.setBasketValue(int256(1.05e18));
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        uint256 f = ctrl.fundingFactor();
        assertGt(f, 1.68e18);
        assertLt(f, 1.70e18);
    }

    function test_funding_factor_direct_deflation_saturates() public {
        basket.setBasketValue(int256(0.85e18));
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        // factor = 0.5 + 25 * (0.85 - 1)/0.85 ~ -3.9 -> clamped 0
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

    // ---- leak: the integrator decays toward 0, so K returns to buckK0 ---- //
    //
    // The simulations' own gains (core/js/src/buckworld.js DEPLOY_DEFAULTS,
    // experiment.deploy_params): a sustained 1% error moves K about 0.2 a year.
    // With a leak of time constant tau the integral of a sustained error e
    // settles at e * tau, so K settles at buckK0 + Kp*e + Ki*e*tau instead of
    // winding to a rail; at parity K returns to buckK0.

    int256  constant SIM_KP  = 1e11;
    int256  constant SIM_KI  = 643_004;
    uint256 constant SIM_K0  = 0.5e18;
    int256  constant CHEAP1  = 1.01e18;          // basket 1% rich: err = -10_000 ppm
    int256  constant ERR1    = -10_000;

    event LeakUpdated(uint256 leakTau);

    function _simCtrl(uint256 tau) internal returns (BuckKControllerDirect c, MockBasket b) {
        c = new BuckKControllerDirect(SIM_KP, SIM_KI, 0, 1800, 0, 0.95e18, SIM_K0, GOV);
        b = new MockBasket();
        b.setController(address(c));
        b.setBasketValue(int256(1e18));
        vm.startPrank(GOV);
        c.setBasket(address(b));
        if (tau > 0) c.setLeak(tau);
        vm.stopPrank();
    }

    function _days(BuckKControllerDirect c, uint256 n) internal {
        // vm.getBlockTimestamp(): under via-IR a loop may read a cached
        // block.timestamp, and every warp would land on the same second.
        for (uint256 i = 0; i < n; i++) {
            vm.warp(vm.getBlockTimestamp() + 1 days);
            c.compute();
        }
    }

    function test_leak_is_off_by_default() public {
        (BuckKControllerDirect c,) = _simCtrl(0);
        assertEq(c.leakTau(), 0);
    }

    function test_set_leak_only_governance() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        ctrl.setLeak(180 days);
        vm.expectEmit(address(ctrl));
        emit LeakUpdated(180 days);
        vm.prank(GOV);
        ctrl.setLeak(180 days);
        assertEq(ctrl.leakTau(), 180 days);
    }

    function test_leak_one_cycle_is_first_order() public {
        (BuckKControllerDirect c, MockBasket b) = _simCtrl(0);
        b.setBasketValue(CHEAP1);
        _days(c, 1);
        int256 i0 = c.I();
        assertEq(i0, ERR1 * 1 days, "unleaked integral");
        vm.prank(GOV);
        c.setLeak(365 days);
        assertEq(c.I(), i0, "setting the leak does not move I");
        _days(c, 1);
        assertEq(c.I(), i0 - i0 * int256(1 days) / int256(365 days) + ERR1 * int256(1 days));
    }

    function test_leak_forgets_a_cycle_longer_than_tau() public {
        (BuckKControllerDirect c, MockBasket b) = _simCtrl(1 days);
        b.setBasketValue(CHEAP1);
        _days(c, 1);
        vm.warp(vm.getBlockTimestamp() + 2 days);
        c.compute();
        assertEq(c.I(), ERR1 * int256(2 days), "only this cycle's increment is left");
    }

    function test_leak_bounds_K_under_a_sustained_error() public {
        // Without a leak a 1% error winds K from 0.5 to its 0 rail in about
        // two and a half years; with tau = 180 days it settles about 0.1 below K0.
        (BuckKControllerDirect plain, MockBasket pb) = _simCtrl(0);
        (BuckKControllerDirect leaky, MockBasket lb) = _simCtrl(180 days);
        pb.setBasketValue(CHEAP1);
        lb.setBasketValue(CHEAP1);
        _days(plain, 5 * 365);
        _days(leaky, 5 * 365);
        assertEq(plain.buckK(), 0, "the unleaked loop rails");
        int256 settled = int256(SIM_K0) + SIM_KP * ERR1 + SIM_KI * ERR1 * int256(180 days);
        assertApproxEqAbs(int256(leaky.buckK()), settled, 0.002e18, "settles at K0 + Kp*e + Ki*e*tau");
        assertGt(leaky.buckK(), 0.39e18);
    }

    function test_leak_returns_K_to_K0_at_parity() public {
        (BuckKControllerDirect c, MockBasket b) = _simCtrl(180 days);
        b.setBasketValue(CHEAP1);
        _days(c, 365);
        assertLt(c.buckK(), 0.42e18, "wound below K0");
        b.setBasketValue(int256(1e18));
        _days(c, 5 * 180);
        assertApproxEqAbs(c.buckK(), SIM_K0, 0.001e18, "back at K0 after five time constants");
    }

    function test_leak_lifts_K_off_a_rail() public {
        // Railed without a leak, then a leak at parity: the anti-windup lets the
        // integrator move back toward the band, so K leaves the rail for K0.
        (BuckKControllerDirect c, MockBasket b) = _simCtrl(0);
        b.setBasketValue(CHEAP1);
        _days(c, 3 * 365);
        assertEq(c.buckK(), 0, "railed");
        vm.prank(GOV);
        c.setLeak(90 days);
        b.setBasketValue(int256(1e18));
        _days(c, 5 * 90);
        assertGt(c.buckK(), 0.49e18, "returned toward K0");
    }
}
