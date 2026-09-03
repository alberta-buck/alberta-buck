// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckKControllerDirect} from "../src/BuckKControllerDirect.sol";
import {BuckKControllerShadow} from "../src/BuckKControllerShadow.sol";
import {MockBasket}            from "./mocks/MockBasket.sol";

/// @dev Stands in for the ops shell's observer: bvib from the MockBasket plus
///      a settable shadow term (18-dec -- already lambda * I / D, the
///      registry arithmetic is tested on the real shell in BuckBasketOps.t.sol)
///      and a settable saturation.
contract MockShadowObserver {
    MockBasket public basket;
    int256  public shadowTerm;     // 18-dec
    uint256 public sat;            // 1e18

    constructor(MockBasket b) { basket = b; }
    function set(int256 term, uint256 s) external { shadowTerm = term; sat = s; }
    function shadowValueInBuck() external view returns (int256) {
        return basket.basketValueInBuck() + shadowTerm;
    }
    function shadowSaturation() external view returns (uint256) { return sat; }
}

/// @title BuckKControllerShadowTest -- WP-3a: the shadow-bvib controller.
///
/// The identity is the regression test: with gamma = 0 and no shadow term
/// (every lambda 0) the Shadow controller must reproduce Direct's state
/// EXACTLY, cycle for cycle, rails included.  The sign canaries pin the
/// direction of CARRY-CONVEXITY.org 7.3; the scheduling tests pin the
/// increment form of Ki_eff.
contract BuckKControllerShadowTest is Test {

    address GOV = makeAddr("governance");

    // Same integral-dominant ppm gains the Direct suite and the sim use.
    int256 constant KP = 5e11;
    int256 constant KI = 2e7;
    int256 constant KD = 0;
    uint256 constant K0 = 1.0e18;

    BuckKControllerDirect internal direct;
    BuckKControllerShadow internal shadow;
    MockBasket            internal basket;
    MockShadowObserver    internal observer;

    function setUp() public {
        direct = new BuckKControllerDirect(KP, KI, KD, 3600, 0.50e18, 1.50e18, K0, GOV);
        shadow = new BuckKControllerShadow(KP, KI, KD, 3600, 0.50e18, 1.50e18, K0, GOV);
        basket = new MockBasket();
        basket.setBasketValue(int256(1e18));
        observer = new MockShadowObserver(basket);
        vm.prank(GOV); direct.setBasket(address(basket));
        vm.prank(GOV); shadow.setBasket(address(basket));
        vm.prank(GOV); shadow.setObserver(address(observer));
    }

    // ---- helpers ---------------------------------------------------------- //

    /// @dev Advance the clock from the cheatcode's view of it.  Under via_ir the
    ///      optimizer folds `block.timestamp` to ONE read per function, so a
    ///      repeated `vm.warp(block.timestamp + dt)` warps to the same instant
    ///      and every later compute() is a cached read.
    function _advance(uint256 dt) internal {
        vm.warp(vm.getBlockTimestamp() + dt);
    }

    function _assertSameState(string memory tag) internal view {
        assertEq(shadow.buckK(),          direct.buckK(),          string.concat(tag, ": buckK"));
        assertEq(shadow.P(),              direct.P(),              string.concat(tag, ": P"));
        assertEq(shadow.I(),              direct.I(),              string.concat(tag, ": I"));
        assertEq(shadow.D(),              direct.D(),              string.concat(tag, ": D"));
        assertEq(shadow.lastBasketCost(), direct.lastBasketCost(), string.concat(tag, ": lastBasketCost"));
        assertEq(shadow.lastBuckPrice(),  direct.lastBuckPrice(),  string.concat(tag, ": lastBuckPrice"));
        assertEq(shadow.lastUpdate(),     direct.lastUpdate(),     string.concat(tag, ": lastUpdate"));
        assertEq(shadow.fundingFactor(),  direct.fundingFactor(),  string.concat(tag, ": fundingFactor"));
    }

    /// @dev A schedule that walks both signs, a cached read (dt < dT), a long
    ///      step, and a rail hit with its anti-windup.
    function _schedule() internal pure returns (int256[9] memory v, uint256[9] memory dt) {
        v  = [int256(1.00e18), 1.05e18, 1.02e18, 0.97e18, 0.97e18, 1.00e18, 0.90e18, 0.90e18, 1.00e18];
        dt = [uint256(3601),   3600,    7200,    3601,    1800,    86400,   3600,    2_600_000, 3601];
    }

    function _runSchedule() internal returns (bool railed) {
        (int256[9] memory v, uint256[9] memory dt) = _schedule();
        for (uint256 i = 0; i < v.length; i++) {
            basket.setBasketValue(v[i]);
            _advance(dt[i]);
            direct.compute();
            shadow.compute();
            _assertSameState(string.concat("step ", vm.toString(i)));
            if (shadow.buckK() == 1.50e18) railed = true;
        }
    }

    // ---- the lambda = gamma = 0 identity ----------------------------------- //

    function test_identity_observerWired_zeroTerm_zeroGamma() public {
        observer.set(0, 0);
        assertTrue(_runSchedule(), "schedule must have railed");   // sanity: anti-windup exercised
    }

    function test_identity_noObserver() public {
        vm.prank(GOV); shadow.setObserver(address(0));
        _runSchedule();
    }

    function test_identity_gammaWithoutObserver_isInert() public {
        vm.prank(GOV); shadow.setObserver(address(0));
        vm.prank(GOV); shadow.setGamma(1e18);
        assertEq(shadow.integralBoost(), 1e18);
        _runSchedule();
    }

    function test_identity_saturatedObserver_zeroGamma_isInert() public {
        observer.set(0, 1e18);           // pinned stabilizer, but gamma = 0
        assertEq(shadow.integralBoost(), 1e18);
        _runSchedule();
    }

    function test_identity_lambdaZero_saturationIgnored_reprime() public {
        // reprime through the shadow path with a zero term equals Direct's.
        basket.setController(address(direct));
        basket.setBasketValue(int256(1.05e18));
        _advance(3601);
        direct.compute(); shadow.compute();
        basket.setBasketValue(int256(1.0e18));
        basket.callReprime();
        basket.setController(address(shadow));
        basket.callReprime();
        _assertSameState("after reprime");
        _advance(3601);
        direct.compute(); shadow.compute();
        _assertSameState("cycle after reprime");
    }

    // ---- sign canaries (CARRY-CONVEXITY.org 7.3) --------------------------- //

    /// BUCK absorbed under the weak side: netInventory > 0 raises the shadow
    /// bvib, the error goes more negative, K falls HARDER than Direct's.
    function test_sign_absorbedInventory_lowersK() public {
        observer.set(int256(0.02e18), 0);           // +2% of depth absorbed
        _advance(3601);
        uint256 kD = direct.compute();
        uint256 kS = shadow.compute();
        assertEq(kD, K0, "direct at parity rests at K0");
        assertLt(kS, kD, "absorbed inventory must contract credit");
        assertLt(shadow.P(), 0, "shadow error negative");
    }

    /// BUCK issued under the strong side: netInventory < 0 lowers the shadow
    /// bvib, the error goes positive, K RISES -- restoring the headroom the
    /// fast actuators spent.
    function test_sign_issuedInventory_raisesK() public {
        observer.set(-int256(0.02e18), 0);          // -2% of depth issued
        _advance(3601);
        uint256 kD = direct.compute();
        uint256 kS = shadow.compute();
        assertEq(kD, K0);
        assertGt(kS, kD, "issued inventory must expand credit");
        assertGt(shadow.P(), 0, "shadow error positive");
    }

    /// The shadow term is ADDITIVE on bvib: a desk that has damped a 3%
    /// discount back to parity while holding 3% of depth leaves K seeing the
    /// same error as an undamped 3% discount.
    function test_sign_dampedExcursion_equalsUndamped() public {
        // Undamped: Direct sees bvib 1.03.
        basket.setBasketValue(int256(1.03e18));
        _advance(3601);
        uint256 kUndamped = direct.compute();
        // Damped: the pools read parity, the desk holds 3% of depth.
        basket.setBasketValue(int256(1.00e18));
        observer.set(int256(0.03e18), 0);
        uint256 kDamped = shadow.compute();
        assertEq(kDamped, kUndamped, "shadow restores the damped error");
    }

    // ---- gain scheduling -------------------------------------------------- //

    /// Ki_eff = Ki (1 + gamma sat) applied to the INCREMENT: at full
    /// saturation and gamma = 1 the integral step is exactly doubled.
    function test_gamma_doublesIntegralStep_atFullSaturation() public {
        vm.prank(GOV); shadow.setGamma(1e18);
        observer.set(0, 1e18);
        assertEq(shadow.integralBoost(), 2e18);
        assertEq(shadow.kiEffective(), KI * 2);

        basket.setBasketValue(int256(0.95e18));     // err = +50_000 ppm
        _advance(3601);
        direct.compute();
        shadow.compute();
        assertEq(direct.I(), int256(50_000) * 3601, "direct step is err*dt");
        assertEq(shadow.I(), 2 * direct.I(),        "shadow step is 2*err*dt");
        assertEq(shadow.P(), direct.P(),            "P unaffected by the schedule");
        assertEq(int256(shadow.buckK()) - int256(direct.buckK()),
                 KI * int256(50_000) * 3601,        "output differs by Ki * extra step");
    }

    function test_gamma_halfSaturation_isOneAndAHalf() public {
        vm.prank(GOV); shadow.setGamma(1e18);
        observer.set(0, 0.5e18);
        assertEq(shadow.integralBoost(), 1.5e18);

        basket.setBasketValue(int256(0.95e18));
        _advance(3601);
        direct.compute();
        shadow.compute();
        int256 step = int256(50_000) * 3601;
        assertEq(shadow.I(), step + step / 2);
    }

    function test_gamma_two_atFullSaturation_isTriple() public {
        vm.prank(GOV); shadow.setGamma(2e18);
        observer.set(0, 1e18);
        assertEq(shadow.integralBoost(), 3e18);
        basket.setBasketValue(int256(1.05e18));    // err = -50_000 ppm: sign carried
        _advance(3601);
        shadow.compute();
        assertEq(shadow.I(), 3 * int256(-50_000) * 3601);
    }

    function test_gamma_saturationAboveUnit_isClamped() public {
        vm.prank(GOV); shadow.setGamma(1e18);
        observer.set(0, 5e18);                      // a misbehaving reporter
        assertEq(shadow.integralBoost(), 2e18, "clamped to full saturation");
    }

    /// The schedule multiplies the increment, never the stock: setting gamma
    /// (or a bound being hit) does not step the live buckK, and the next
    /// cycle differs from an unscheduled twin by exactly one boosted
    /// increment.
    function test_gamma_isBumpless_onTheWoundIntegral() public {
        basket.setBasketValue(int256(0.95e18));
        _advance(3601);
        direct.compute(); shadow.compute();
        _advance(3601);
        direct.compute(); shadow.compute();
        _assertSameState("wound identically");
        uint256 kBefore = shadow.buckK();
        int256  iBefore = shadow.I();

        vm.prank(GOV); shadow.setGamma(1e18);
        observer.set(0, 1e18);                      // the bound is hit now
        assertEq(shadow.buckK(), kBefore, "setter must not step buckK");
        assertEq(shadow.I(),     iBefore, "setter must not touch the stock");

        _advance(3601);
        direct.compute(); shadow.compute();
        int256 extra = int256(50_000) * 3601;       // one extra err*dt
        assertEq(shadow.I() - direct.I(), extra);
        assertEq(int256(shadow.buckK()) - int256(direct.buckK()), KI * extra);

        // Bound released: the boost stops, the stock keeps its history.
        observer.set(0, 0);
        _advance(3601);
        direct.compute(); shadow.compute();
        assertEq(shadow.I() - direct.I(), extra, "no rescaling on release");
    }

    /// Anti-windup still governs the boosted increment: an increment that
    /// would carry the output past the rail is discarded, exactly as
    /// Direct discards its own.
    function test_gamma_respectsAntiWindup() public {
        vm.prank(GOV); shadow.setGamma(1e18);
        observer.set(0, 1e18);                      // boost 2: 200_000 ppm/s
        basket.setBasketValue(int256(0.90e18));     // err = +100_000 ppm
        // Cycle 1: I = 2e10 -> raw = 1 + 0.05 + 2e7*2e10/1e18 = 1.45, inside.
        _advance(100_000);
        shadow.compute();
        assertEq(shadow.I(), 2 * int256(100_000) * 100_000);
        assertEq(shadow.buckK(), 1.45e18);
        int256 iInside = shadow.I();
        // Cycle 2: the boosted increment would put raw at 1.85 -> clamped to
        // the rail, increment discarded.
        _advance(100_000);
        shadow.compute();
        assertEq(shadow.buckK(), 1.50e18, "railed");
        assertEq(shadow.I(), iInside, "boosted increment discarded at the rail");
        // Error reverses: the (boosted) unwinding increment is accepted.
        basket.setBasketValue(int256(1.10e18));
        _advance(3600);
        shadow.compute();
        assertEq(shadow.I(), iInside - 2 * int256(100_000) * 3600, "unwinds back toward the band");
    }

    // ---- reprime ---------------------------------------------------------- //

    function test_reprime_only_basket() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("only basket");
        shadow.reprime();
    }

    /// reprime reads the observer (the process variable IS the shadow
    /// value) and absorbs a step in it -- inventory released in one go --
    /// so the next no-motion cycle reproduces the current buckK.
    function test_reprime_absorbsShadowStep() public {
        basket.setController(address(shadow));
        observer.set(int256(0.03e18), 0);
        _advance(3601);
        shadow.compute();
        uint256 kBefore = shadow.buckK();
        assertLt(kBefore, K0);

        observer.set(0, 0);                         // the desk unwound
        basket.callReprime();
        assertEq(shadow.P(), 0, "P recaptured against the shadow value");
        assertEq(shadow.lastBasketCost(), 1_000_000, "ppm, from the observer");
        _advance(3601);
        uint256 kAfter = shadow.compute();
        assertApproxEqRel(kAfter, kBefore, 0.001e18);
    }

    function test_reprime_withGamma_isUnaffected() public {
        basket.setController(address(shadow));
        vm.prank(GOV); shadow.setGamma(1e18);
        observer.set(int256(0.03e18), 1e18);
        _advance(3601);
        shadow.compute();
        uint256 kBefore = shadow.buckK();
        observer.set(0, 1e18);
        basket.callReprime();
        _advance(3601);
        assertApproxEqRel(shadow.compute(), kBefore, 0.001e18);
    }

    // ---- governance / access control --------------------------------------- //

    function test_setObserver_only_governance() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        shadow.setObserver(address(observer));
    }

    function test_setGamma_only_governance_and_bounded() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        shadow.setGamma(1e18);
        vm.prank(GOV);
        vm.expectRevert("gamma too large");
        shadow.setGamma(1_001e18);
    }

    function test_setBasket_one_shot_and_governance() public {
        vm.prank(GOV);
        vm.expectRevert("basket already set");
        shadow.setBasket(makeAddr("other"));
        BuckKControllerShadow fresh = new BuckKControllerShadow(
            KP, KI, KD, 3600, 0.50e18, 1.50e18, K0, GOV);
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        fresh.setBasket(address(basket));
    }

    function test_observer_unwired_readsBasket() public {
        observer.set(int256(0.05e18), 0);
        vm.prank(GOV); shadow.setObserver(address(0));
        basket.setBasketValue(int256(1.0e18));
        _advance(3601);
        assertEq(shadow.compute(), K0, "parity from the basket, term ignored");
    }
}
