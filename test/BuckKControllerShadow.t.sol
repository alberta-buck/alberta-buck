// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckKControllerDirect} from "../src/BuckKControllerDirect.sol";
import {BuckKControllerShadow} from "../src/BuckKControllerShadow.sol";
import {ShadowObserver}        from "../src/basket/ShadowObserver.sol";
import {IStabilizer}           from "../src/basket/IStabilizer.sol";
import {MockBasket}            from "./mocks/MockBasket.sol";

/// @dev Stands in for the observer at the controller's seam: a settable
///      aggregate position s (1e18), a settable held saturation and settable
///      flags.  The registry arithmetic (modes, weights, held caps) is tested
///      on the real ShadowObserver below and in BuckBasketOps.t.sol.
contract MockShadowObserver {
    MockBasket public basket;
    int256  public s;              // 1e18, absorbed positive
    uint256 public sat;            // 1e18
    uint256 public staleMask;
    uint256 public excludedMask;
    uint256 public observed;       // observe() calls

    constructor(MockBasket b) { basket = b; }
    function set(int256 position, uint256 saturation) external { s = position; sat = saturation; }
    function setFlags(uint256 st, uint256 ex) external { staleMask = st; excludedMask = ex; }
    function observe() external returns (int256) { observed += 1; return s; }
    function aggregatePosition() external view returns (int256) { return s; }
    function shadowValueInBuck() external view returns (int256) {
        return basket.basketValueInBuck() + s;
    }
    function shadowSaturation() external view returns (uint256) { return sat; }
    function flags() external view returns (uint256, uint256) { return (staleMask, excludedMask); }
}

/// @dev A MockBasket that also serves the observer's reference depth D.
contract MockDepthBasket is MockBasket {
    uint256 public shadowDepth;
    function setDepth(uint256 d) external { shadowDepth = d; }
}

/// @dev A level-1 stabilizer with a settable book and a settable cap that
///      can be made to revert on demand (the sensor fault of decision 9).
contract MockPositionStabilizer is IStabilizer {
    int256  public netInventory;
    uint256 public cap;
    bool    public capReverts;
    error NavDown();
    constructor(uint256 c) { cap = c; }
    function set(int256 q) external { netInventory = q; }
    function setCap(uint256 c) external { cap = c; }
    function setCapReverts(bool r) external { capReverts = r; }
    function capacity() external pure returns (uint256) { return 1e18; }
    function saturation() external pure returns (uint256) { return 0; }
    function positionCap() external view returns (uint256) {
        if (capReverts) revert NavDown();
        return cap;
    }
}

/// @title BuckKControllerShadowTest -- WP-3a / WP-13: the two-loop controller.
///
/// The identity is the regression test: with Kq = Kqi = Kqd = 0 and gamma =
/// 0 the Shadow controller must reproduce Direct's state EXACTLY, cycle for
/// cycle, rails included, whatever the observer reports.  The S0 tests pin
/// the D7 claim that mode S with the price loop's gains on the position
/// loop IS D4 as built, term for term in the interior.  The sign canaries
/// pin the direction of 7.3; the anti-windup test the per-integrator rule;
/// the bumpless tests the retune / setBuckK0 / reprime algebra with the
/// second integrator in it; the scheduling tests the increment form of
/// Ki_eff.
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

    /// @dev The S0 design tag: the position loop carries the price loop's gains.
    function _s0Gains() internal {
        vm.prank(GOV); shadow.setPositionGains(KP, KI, KD);
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

    // ---- the Kq = Kqi = Kqd = 0 identity (R15) ---------------------------- //

    function test_identity_observerWired_zeroPosition_zeroGains() public {
        observer.set(0, 0);
        assertTrue(_runSchedule(), "schedule must have railed");   // sanity: anti-windup exercised
    }

    /// The observer reports a LIVE position, but with every position gain 0
    /// the loop is inert: Direct's state, cycle for cycle, rails included.
    /// The position state is tracked (S, IS, lastS) without entering K.
    function test_identity_nonzeroPosition_zeroGains() public {
        observer.set(int256(0.05e18), 0.7e18);
        _runSchedule();
        assertEq(shadow.lastS(), int256(0.05e18), "position tracked");
        assertEq(shadow.S(), -50_000, "position error tracked in ppm");
        assertLt(shadow.IS(), 0, "position integrator wound");
        assertEq(shadow.Kq(), 0); assertEq(shadow.Kqi(), 0); assertEq(shadow.Kqd(), 0);
        assertGt(observer.observed(), 0, "the observer was read every cycle");
    }

    function test_identity_noObserver() public {
        vm.prank(GOV); shadow.setObserver(address(0));
        _s0Gains();                                  // gains without an observer: s = 0
        _runSchedule();
        assertEq(shadow.IS(), 0);
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

    function test_identity_reprime_zeroGains() public {
        basket.setController(address(direct));
        basket.setBasketValue(int256(1.05e18));
        observer.set(int256(0.02e18), 0);
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

    // ---- S0: mode S with the price loop's gains IS D4 as built (D7) ------- //

    ShadowObserver           internal obsS;
    MockDepthBasket          internal depthBasket;
    MockPositionStabilizer   internal stab;
    MockBasket               internal composite;
    BuckKControllerShadow    internal kS0;
    BuckKControllerDirect    internal kD4;
    uint256 constant LAMBDA = 0.5e18;
    uint256 constant DEPTH  = 1_000_000e6;      // 1M BUCK of pool reserve

    /// @dev kS0: the two-loop controller over the REAL observer (mode S,
    ///      lambda) on a mock book; kD4: a Direct instance fed the composite
    ///      bvib + lambda * q / D -- D4 as WP-3a built it.
    function _setUpS0(int256 kd) internal {
        depthBasket = new MockDepthBasket();
        depthBasket.setBasketValue(int256(1e18));
        depthBasket.setDepth(DEPTH);
        obsS = new ShadowObserver(address(depthBasket), GOV);
        stab = new MockPositionStabilizer(100_000e6);
        vm.prank(GOV); obsS.addStabilizer(address(stab), LAMBDA);
        assertFalse(obsS.excluded(address(stab)), "readable cap: included at once");
        composite = new MockBasket();
        composite.setBasketValue(int256(1e18));
        kS0 = new BuckKControllerShadow(KP, KI, kd, 3600, 0.50e18, 1.50e18, K0, GOV);
        vm.prank(GOV); kS0.setBasket(address(depthBasket));
        vm.prank(GOV); kS0.setObserver(address(obsS));
        vm.prank(GOV); kS0.setPositionGains(KP, KI, kd);
        kD4 = new BuckKControllerDirect(KP, KI, kd, 3600, 0.50e18, 1.50e18, K0, GOV);
        vm.prank(GOV); kD4.setBasket(address(composite));
    }

    /// @dev The observer's own integer arithmetic for the composite.
    function _compositeValue(int256 bvib, int256 q) internal pure returns (int256) {
        return bvib + (int256(LAMBDA) * q) / int256(DEPTH);
    }

    /// @dev An interior schedule of (bvib, q): both signs of both, held and
    ///      moving, with every value a multiple of 1 ppm so the composite
    ///      and its parts round identically at the ppm boundary.
    function _s0Schedule() internal pure
        returns (int256[8] memory v, int256[8] memory q, uint256[8] memory dt)
    {
        v  = [int256(1.00e18), 1.02e18,  1.02e18,  0.99e18,  0.99e18,  1.00e18,  1.01e18,  1.00e18];
        q  = [int256(0),       20_000e6, 40_000e6, 40_000e6, -30_000e6, -30_000e6, 0,       0];
        dt = [uint256(3601),   7200,     86400,    3601,     43200,    86400,    3601,     172800];
    }

    function _runS0(uint256 tolWei) internal {
        (int256[8] memory v, int256[8] memory q, uint256[8] memory dt) = _s0Schedule();
        for (uint256 i = 0; i < v.length; i++) {
            depthBasket.setBasketValue(v[i]);
            stab.set(q[i]);
            composite.setBasketValue(_compositeValue(v[i], q[i]));
            assertEq(obsS.shadowValueInBuck(), _compositeValue(v[i], q[i]), "observer composite");
            _advance(dt[i]);
            kD4.compute();
            kS0.compute();
            string memory tag = string.concat("step ", vm.toString(i));
            assertLt(kS0.buckK(), 1.50e18, string.concat(tag, ": interior"));
            assertGt(kS0.buckK(), 0.50e18, string.concat(tag, ": interior"));
            assertApproxEqAbs(kS0.buckK(), kD4.buckK(), tolWei, string.concat(tag, ": buckK"));
            assertEq(kS0.P() + kS0.S(), kD4.P(),   string.concat(tag, ": e + r == composite error"));
            assertEq(kS0.I() + kS0.IS(), kD4.I(),  string.concat(tag, ": I + IS == composite I"));
            assertEq(kS0.lastUpdate(), kD4.lastUpdate());
        }
        // Decision 10: the price loop and fundingFactor() read the RAW
        // basket; only the composite twin's mint gate moves with the book.
        assertEq(kS0.lastBasketCost(), 1_000_000, "raw process at parity");
        assertEq(kS0.fundingFactor(), 1e18, "funding factor sees the raw bvib");
    }

    function test_s0_reproducesD4_interior_exact() public {
        _setUpS0(0);
        _runS0(0);
        assertGt(kS0.observer().aggregatePosition() == 0 ? 1 : 0, 0);
    }

    /// With a D term on both loops the split derivative rounds once per
    /// term instead of once on the sum: equal to within a wei per term.
    function test_s0_reproducesD4_interior_withKd() public {
        _setUpS0(1e9);
        _runS0(2);
    }

    /// The composite twin's mint gate DOES move with the book (the WP-3a
    /// behaviour decision 10 retires); the two-loop controller's does not.
    function test_decision10_fundingFactor_readsRawBasket() public {
        _setUpS0(0);
        depthBasket.setBasketValue(int256(1.00e18));
        stab.set(int256(60_000e6));                       // +3% of depth absorbed
        composite.setBasketValue(_compositeValue(1.00e18, 60_000e6));
        _advance(3601);
        kD4.compute(); kS0.compute();
        assertEq(kS0.buckK(), kD4.buckK(), "K agrees (S0)");
        assertGt(kD4.fundingFactor(), 1e18, "composite twin: gate moved");
        assertEq(kS0.fundingFactor(), 1e18, "two-loop: gate on the raw bvib");
    }

    // ---- sign canaries (CARRY-CONVEXITY.org 7.3, D7) ------------------------ //

    /// BUCK absorbed under the weak side: s > 0, the position error is
    /// negative, K falls BELOW Direct's -- the contraction the absorption
    /// was betting on.
    function test_sign_absorbedInventory_lowersK() public {
        _s0Gains();
        observer.set(int256(0.02e18), 0);           // +2% of depth absorbed
        _advance(3601);
        uint256 kD = direct.compute();
        uint256 kS = shadow.compute();
        assertEq(kD, K0, "direct at parity rests at K0");
        assertLt(kS, kD, "absorbed inventory must contract credit");
        assertEq(shadow.P(), 0, "price error untouched by the position");
        assertLt(shadow.S(), 0, "position error negative");
        assertLt(shadow.IS(), 0);
    }

    /// BUCK issued under the strong side: s < 0, K RISES -- restoring the
    /// headroom the fast actuators spent.
    function test_sign_issuedInventory_raisesK() public {
        _s0Gains();
        observer.set(-int256(0.02e18), 0);          // -2% of depth issued
        _advance(3601);
        uint256 kD = direct.compute();
        uint256 kS = shadow.compute();
        assertEq(kD, K0);
        assertGt(kS, kD, "issued inventory must expand credit");
        assertGt(shadow.S(), 0, "position error positive");
    }

    /// The same canaries in V units through the real observer: a half-full
    /// book absorbed / issued, s = +/- 0.5 (fill), K below / above Direct's.
    function test_sign_modeV_absorbedLowers_issuedRaises() public {
        _setUpS0(0);
        vm.prank(GOV); obsS.setMode(ShadowObserver.Mode.V);
        stab.set(int256(50_000e6));                       // cap 100_000: fill +0.5
        assertEq(obsS.aggregatePosition(), int256(0.5e18));
        _advance(3601);
        uint256 kAbs = kS0.compute();
        assertLt(kAbs, K0, "absorbed: K below K0");
        assertEq(kS0.S(), -500_000, "fill in ppm");

        BuckKControllerShadow k2 = new BuckKControllerShadow(KP, KI, 0, 3600, 0.50e18, 1.50e18, K0, GOV);
        vm.prank(GOV); k2.setBasket(address(depthBasket));
        vm.prank(GOV); k2.setObserver(address(obsS));
        vm.prank(GOV); k2.setPositionGains(KP, KI, 0);
        stab.set(-int256(50_000e6));
        assertEq(obsS.aggregatePosition(), -int256(0.5e18));
        _advance(3601);
        assertGt(k2.compute(), K0, "issued: K above K0");
    }

    /// The position term is ADDITIVE on the price error at S0 gains: a desk
    /// that has damped a 3% discount back to parity while holding 3% of
    /// depth leaves K seeing the same error as an undamped 3% discount.
    function test_sign_dampedExcursion_equalsUndamped() public {
        _s0Gains();
        // Undamped: Direct sees bvib 1.03.
        basket.setBasketValue(int256(1.03e18));
        _advance(3601);
        uint256 kUndamped = direct.compute();
        // Damped: the pools read parity, the level holds 3% of depth.
        basket.setBasketValue(int256(1.00e18));
        observer.set(int256(0.03e18), 0);
        uint256 kDamped = shadow.compute();
        assertEq(kDamped, kUndamped, "the position loop restores the damped error");
    }

    // ---- the observer: modes, weights, held caps, sensor faults ------------ //

    /// Mode V: cost-weighted fill, normalized by the weights of the INCLUDED
    /// stabilizers; a disabled one (cap 0) drops out of both sums; the
    /// pseudo-stabilizer counts once its sim-only cap is set; fills clamp.
    function test_observer_modeV_weightedFill_renormalized() public {
        _setUpS0(0);
        vm.prank(GOV); obsS.setMode(ShadowObserver.Mode.V);
        MockPositionStabilizer b = new MockPositionStabilizer(100_000e6);
        vm.prank(GOV); obsS.addStabilizer(address(b), 0);
        vm.prank(GOV); obsS.setStabilizerWeight(address(b), 0.25e18);
        stab.set(int256(50_000e6));                       // fill +0.5, w 1
        b.set(-int256(100_000e6));                        // fill -1,   w 0.25
        assertEq(obsS.aggregatePosition(), int256(0.2e18), "(0.5 - 0.25) / 1.25");
        assertEq(obsS.position(address(stab)), int256(0.5e18));
        assertEq(obsS.position(address(b)), -int256(1e18));

        // b disabled: cap 0 on the next read -> excluded, weights renormalized.
        b.setCap(0);
        obsS.refresh();
        assertTrue(obsS.excluded(address(b)));
        assertFalse(obsS.stale(address(b)), "a good read of 0 is not stale");
        assertEq(obsS.position(address(b)), 0);
        assertEq(obsS.aggregatePosition(), int256(0.5e18), "0.5 / 1");
        (uint256 st, uint256 ex) = obsS.flags();
        assertEq(st, 0); assertEq(ex, 2, "bit 1: b excluded");
        // ... and its book, however large, cannot move s.
        b.set(int256(1_000_000e6));
        assertEq(obsS.aggregatePosition(), int256(0.5e18), "excluded book is invisible");

        // The pseudo-stabilizer needs a cap to be in V (bit 255 until then).
        vm.prank(GOV); obsS.setShadowWeight(1e18);
        vm.prank(GOV); obsS.setShadowOffset(int256(100_000e6));
        assertEq(obsS.aggregatePosition(), int256(0.5e18), "no cap: not in V");
        (, ex) = obsS.flags();
        assertEq(ex, 2 | (1 << 255));
        vm.prank(GOV); obsS.setShadowCap(100_000e6);
        assertEq(obsS.aggregatePosition(), int256(0.75e18), "(0.5 + 1) / 2");
        (, ex) = obsS.flags();
        assertEq(ex, 2);

        // Fills clamp to [-1, 1]: a book past its bound is full, s stays in range.
        stab.set(int256(300_000e6));
        assertEq(obsS.position(address(stab)), int256(1e18));
        assertEq(obsS.aggregatePosition(), int256(1e18));
        assertEq(obsS.shadowSaturation(), 1e18);

        // Mode S in the same registry: lambda_i q_i / D, the pseudo at its lambda.
        vm.prank(GOV); obsS.setMode(ShadowObserver.Mode.S);
        vm.prank(GOV); obsS.setShadowLambda(1e18);
        assertEq(obsS.aggregatePosition(),
                 (int256(LAMBDA) * int256(300_000e6) + int256(1e18) * int256(100_000e6)) / int256(DEPTH));
    }

    /// The sensor fault (decision 9): a cap that reverts leaves the held cap,
    /// the position, the saturation and the aggregate unchanged and sets
    /// stale(); the first successful read clears stale and refreshes the cap.
    function test_observer_guardTrip_holdsCap_flagsStale_clearsOnRead() public {
        _setUpS0(0);
        vm.prank(GOV); obsS.setMode(ShadowObserver.Mode.V);
        stab.set(int256(50_000e6));
        assertEq(obsS.heldCap(address(stab)), 100_000e6);
        uint256 t0 = obsS.heldAt(address(stab));
        int256  pos0 = obsS.position(address(stab));
        uint256 sat0 = obsS.stabilizerSaturation(address(stab));
        int256  s0 = obsS.aggregatePosition();
        assertEq(pos0, int256(0.5e18)); assertEq(sat0, 0.5e18); assertEq(s0, int256(0.5e18));

        stab.setCapReverts(true);
        stab.setCap(999e6);                               // a new cap it cannot deliver
        _advance(100);
        vm.expectEmit(true, false, false, true, address(obsS));
        emit ShadowObserver.StabilizerStale(address(stab), true);
        obsS.refresh();
        assertTrue(obsS.stale(address(stab)), "stale");
        assertFalse(obsS.excluded(address(stab)), "still included on the held cap");
        assertEq(obsS.heldCap(address(stab)), 100_000e6, "cap held");
        assertEq(obsS.heldAt(address(stab)), t0, "held time unchanged");
        assertEq(obsS.position(address(stab)), pos0, "position unchanged");
        assertEq(obsS.stabilizerSaturation(address(stab)), sat0, "saturation unchanged");
        assertEq(obsS.aggregatePosition(), s0, "aggregate unchanged");
        assertEq(obsS.shadowSaturation(), 0.5e18, "never pinned by a trip");
        (uint256 st, uint256 ex) = obsS.flags();
        assertEq(st, 1); assertEq(ex, 0);

        // The controller keeps computing through the outage, and flags it.
        _advance(3601);
        kS0.compute();
        BuckKControllerShadow.Terms memory t = kS0.terms();
        assertEq(t.staleMask, 1); assertEq(t.s, s0);

        // Position stays readable during the outage.
        stab.set(int256(100_000e6));
        assertEq(obsS.position(address(stab)), int256(1e18), "position always reported");

        // First successful read: stale cleared, cap refreshed, fill renormalized.
        stab.setCapReverts(false);
        vm.expectEmit(true, false, false, true, address(obsS));
        emit ShadowObserver.StabilizerStale(address(stab), false);
        obsS.refresh();
        assertFalse(obsS.stale(address(stab)));
        assertEq(obsS.heldCap(address(stab)), 999e6);
        assertGt(obsS.heldAt(address(stab)), t0);
        assertEq(obsS.position(address(stab)), int256(1e18), "100_000 / 999: clamped full");
        stab.setCap(200_000e6);
        obsS.refresh();
        assertEq(obsS.position(address(stab)), int256(0.5e18));
    }

    /// Before any successful read a stabilizer is excluded and stale; a
    /// reverting cap at registration is not an error (the desk before
    /// bootstrap), and a codeless address is (a governance error).
    function test_observer_unreadAtRegistration_excludedAndStale() public {
        _setUpS0(0);
        MockPositionStabilizer c = new MockPositionStabilizer(100_000e6);
        c.setCapReverts(true);
        c.set(int256(100_000e6));
        vm.prank(GOV); obsS.addStabilizer(address(c), 1e18);
        assertTrue(obsS.stale(address(c)));
        assertTrue(obsS.excluded(address(c)));
        assertEq(obsS.heldCap(address(c)), 0);
        assertEq(obsS.position(address(c)), 0);
        assertEq(obsS.aggregatePosition(), 0, "a never-read book is not in s");
        c.setCapReverts(false);
        obsS.refresh();
        assertFalse(obsS.stale(address(c)));
        assertFalse(obsS.excluded(address(c)));
        assertGt(obsS.aggregatePosition(), 0);

        vm.prank(GOV);
        vm.expectRevert(ShadowObserver.NoCode.selector);
        obsS.addStabilizer(makeAddr("eoa"), 1e18);
    }

    /// A stabilizer that reverts on netInventory() reverts the whole read,
    /// loudly (today's rule stays): a cap outage is a sensor fault, a book
    /// outage is a governance error.
    function test_observer_revertingBook_revertsLoudly() public {
        _setUpS0(0);
        RevertingBook r = new RevertingBook();
        vm.prank(GOV); obsS.addStabilizer(address(r), 1e18);
        assertFalse(obsS.excluded(address(r)), "its cap read fine");
        vm.expectRevert(RevertingBook.Down.selector);
        obsS.aggregatePosition();
        vm.expectRevert(RevertingBook.Down.selector);
        obsS.shadowValueInBuck();
        vm.prank(GOV); obsS.removeStabilizer(address(r));
        assertEq(obsS.aggregatePosition(), 0);
    }

    // ---- anti-windup: per integrator ----------------------------------------- //

    /// At a rail each integrator may only move back toward the band.  With
    /// the price error reversing while the position error keeps pushing
    /// outward, I unwinds and IS is frozen -- the two are judged separately.
    function test_antiWindup_perIntegrator() public {
        _s0Gains();
        basket.setBasketValue(int256(0.90e18));     // err = +100_000 ppm
        observer.set(-int256(0.10e18), 0);          // issued 10%: r = +100_000 ppm
        _advance(100_000);
        shadow.compute();
        // I = IS = 1e10: raw = 1 + 0.05 + 0.2 + 0.05 + 0.2 = 1.50, on the rail.
        assertEq(shadow.I(),  int256(100_000) * 100_000);
        assertEq(shadow.IS(), int256(100_000) * 100_000);
        assertEq(shadow.buckK(), 1.50e18);
        int256 i1 = shadow.I(); int256 is1 = shadow.IS();
        // Both would wind further: both increments discarded.
        _advance(100_000);
        shadow.compute();
        assertEq(shadow.buckK(), 1.50e18, "railed");
        assertEq(shadow.I(), i1, "price increment discarded");
        assertEq(shadow.IS(), is1, "position increment discarded");
        // The price error reverses a little while the position error keeps
        // pushing outward, hard enough that the raw output stays past the
        // rail: I unwinds (it brings the output back), IS is frozen (it
        // would wind further).  raw = 1 - 0.0005 + 0.198 + 0.05 + 0.373 = 1.62.
        basket.setBasketValue(int256(1.001e18));    // err = -1_000 ppm
        _advance(86400);
        shadow.compute();
        assertEq(shadow.buckK(), 1.50e18, "still railed");
        assertEq(shadow.I(), i1 - int256(1_000) * 86400, "I unwinds toward the band");
        assertEq(shadow.IS(), is1, "IS still frozen at the rail");
        int256 i3 = shadow.I();
        // Position error reverses too: IS unwinds.
        observer.set(int256(0.10e18), 0);           // absorbed 10%: r = -100_000 ppm
        _advance(3600);
        shadow.compute();
        assertEq(shadow.IS(), is1 - int256(100_000) * 3600, "IS unwinds toward the band");
        assertEq(shadow.I(), i3 - int256(1_000) * 3600, "I keeps unwinding");
    }

    // ---- attribution: terms() and BuckKTerms (R14) --------------------------- //

    function test_terms_decomposeK_andEvent() public {
        _s0Gains();
        observer.setFlags(1, 2);
        basket.setBasketValue(int256(1.05e18));     // err = -50_000 ppm
        observer.set(int256(0.02e18), 0);           // r = -20_000 ppm
        _advance(3601);
        int256 uP  = KP * -50_000;
        int256 uI  = KI * (-50_000 * 3601);
        int256 uQ  = KP * -20_000;
        int256 uQI = KI * (-20_000 * 3601);
        vm.expectEmit(false, false, false, true, address(shadow));
        emit BuckKControllerShadow.BuckKTerms(uP, uI, 0, uQ, uQI, 0, int256(0.02e18), 1, 2);
        shadow.compute();
        BuckKControllerShadow.Terms memory t = shadow.terms();
        assertEq(t.uP, uP); assertEq(t.uI, uI); assertEq(t.uD, 0);
        assertEq(t.uQ, uQ); assertEq(t.uQI, uQI); assertEq(t.uQD, 0);
        assertEq(t.s, int256(0.02e18));
        assertEq(t.staleMask, 1); assertEq(t.excludedMask, 2);
        assertEq(int256(shadow.buckK()), int256(K0) + uP + uI + uQ + uQI, "terms sum to K in the interior");
        assertEq(shadow.lastDt(), 3601);
    }

    // ---- bumpless: retunePosition, setBuckK0, reprime ------------------------ //

    /// Wind both integrators away from zero, then retune the position gains:
    /// the live buckK is unchanged and the next no-motion cycle continues
    /// from it; the raw setter, by contrast, steps K by IS * dKqi.
    function test_bumpless_retunePosition() public {
        _s0Gains();
        basket.setBasketValue(int256(1.02e18));
        observer.set(int256(0.03e18), 0);
        _advance(86400); shadow.compute();
        _advance(86400); shadow.compute();
        uint256 kBefore = shadow.buckK();
        assertLt(kBefore, K0);
        assertLt(shadow.IS(), 0);

        vm.prank(GOV); shadow.retunePosition(KP * 3, KI * 2, 0);
        assertEq(shadow.buckK(), kBefore, "retune must not step buckK");
        // The invariant holds to the integer remainder of I's division (< Ki).
        int256 base = int256(K0) + KP * shadow.P() + KI * shadow.I() + KP * 3 * shadow.S() + KI * 2 * shadow.IS();
        assertApproxEqAbs(uint256(base), kBefore, uint256(KI), "the invariant holds under the new gains");
        // A no-motion cycle: only the integrators' increments move K.
        _advance(3600);
        uint256 kAfter = shadow.compute();
        int256 expected = base + KI * shadow.P() * 3600 + KI * 2 * shadow.S() * 3600;
        assertEq(int256(kAfter), expected, "continues from the live K");

        // The raw setter is NOT bumpless: it steps K by IS * (Kqi_new - Kqi_old).
        uint256 kRaw = shadow.buckK();
        int256 isNow = shadow.IS();
        vm.prank(GOV); shadow.setPositionGains(KP * 3, KI * 4, 0);
        _advance(3600);
        uint256 kStepped = shadow.compute();
        int256 noStep = int256(kRaw) + KI * shadow.P() * 3600 + KI * 4 * shadow.S() * 3600;
        assertEq(int256(kStepped) - noStep, KI * 2 * isNow, "raw setGains steps by IS * dKqi");
    }

    function test_bumpless_setBuckK0_withPositionLoop() public {
        _s0Gains();
        basket.setBasketValue(int256(0.98e18));
        observer.set(-int256(0.02e18), 0);
        _advance(86400); shadow.compute();
        uint256 kBefore = shadow.buckK();
        vm.prank(GOV); shadow.setBuckK0(0.80e18);
        assertEq(shadow.buckK(), kBefore, "setBuckK0 must not step buckK");
        assertApproxEqAbs(uint256(int256(0.80e18) + KP * shadow.P() + KI * shadow.I()
                                  + KP * shadow.S() + KI * shadow.IS()),
                          kBefore, uint256(KI), "invariant under the new K0 (to I's remainder)");
        vm.prank(GOV); shadow.retune(KP * 2, KI * 3, 0);
        assertEq(shadow.buckK(), kBefore, "retune must not step buckK");
        assertApproxEqAbs(uint256(int256(0.80e18) + KP * 2 * shadow.P() + KI * 3 * shadow.I()
                                  + KP * shadow.S() + KI * shadow.IS()),
                          kBefore, uint256(KI * 3), "invariant under the retuned price loop");
        // A no-motion cycle continues from the live K.
        _advance(3600);
        assertApproxEqAbs(shadow.compute(),
                          uint256(int256(kBefore) + KI * 3 * shadow.P() * 3600 + KI * shadow.S() * 3600),
                          uint256(KI * 3));
    }

    /// reprime after a dilution step: P and S are recaptured against the
    /// raw basket and the observer, I re-derived, and the next no-motion
    /// cycle at parity reproduces the current buckK.
    function test_bumpless_reprime_afterDilutionStep() public {
        _s0Gains();
        basket.setController(address(shadow));
        basket.setBasketValue(int256(1.03e18));
        observer.set(int256(0.03e18), 0);
        _advance(86400); shadow.compute();
        uint256 kBefore = shadow.buckK();
        assertLt(kBefore, K0);
        int256 isBefore = shadow.IS();

        // The dilution: the basket value and the level's position both step.
        basket.setBasketValue(int256(0.97e18));
        observer.set(-int256(0.01e18), 0);
        basket.callReprime();
        assertEq(shadow.buckK(), kBefore, "reprime never touches the output");
        assertEq(shadow.P(), 30_000, "P recaptured against the raw basket");
        assertEq(shadow.S(), 10_000, "S recaptured against the observer");
        assertEq(shadow.lastS(), -int256(0.01e18));
        assertEq(shadow.IS(), isBefore, "the position integrator keeps its history");
        assertEq(int256(shadow.buckK()),
                 int256(K0) + KP * shadow.P() + KI * shadow.I() + KP * shadow.S() + KI * shadow.IS(),
                 "invariant after reprime");
        // Back to parity with a flat book: the next cycle reproduces K.
        basket.setBasketValue(int256(1.00e18));
        observer.set(0, 0);
        basket.callReprime();
        _advance(3601);
        assertEq(shadow.compute(), kBefore, "no-motion cycle at parity reproduces buckK");
    }

    // ---- gain scheduling (WP-3a; gamma keyed to the HELD saturation) ------- //

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

    /// reprime reads the observer too and absorbs a step in the position --
    /// inventory released in one go -- so the next no-motion cycle
    /// reproduces the current buckK.
    function test_reprime_absorbsPositionStep() public {
        _s0Gains();
        basket.setController(address(shadow));
        observer.set(int256(0.03e18), 0);
        _advance(3601);
        shadow.compute();
        uint256 kBefore = shadow.buckK();
        assertLt(kBefore, K0);

        observer.set(0, 0);                         // the level unwound
        basket.callReprime();
        assertEq(shadow.P(), 0, "P recaptured against the raw basket");
        assertEq(shadow.S(), 0, "S recaptured against the observer");
        assertEq(shadow.lastBasketCost(), 1_000_000, "ppm, raw");
        _advance(3601);
        uint256 kAfter = shadow.compute();
        assertApproxEqRel(kAfter, kBefore, 0.001e18);
    }

    function test_reprime_withGamma_isUnaffected() public {
        _s0Gains();
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

    function test_positionGains_only_governance_retuneNeedsKi() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        shadow.setPositionGains(1, 2, 3);
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        shadow.retunePosition(1, 2, 3);
        vm.prank(GOV); shadow.setGains(KP, 0, 0);
        vm.prank(GOV);
        vm.expectRevert("retune needs Ki");
        shadow.retunePosition(1, 2, 3);
        vm.prank(GOV); shadow.setPositionGains(1, 2, 3);
        assertEq(shadow.Kq(), 1); assertEq(shadow.Kqi(), 2); assertEq(shadow.Kqd(), 3);
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

    function test_observer_unwired_positionIgnored() public {
        _s0Gains();
        observer.set(int256(0.05e18), 0);
        vm.prank(GOV); shadow.setObserver(address(0));
        basket.setBasketValue(int256(1.0e18));
        _advance(3601);
        assertEq(shadow.compute(), K0, "parity from the basket, position ignored");
        assertEq(shadow.lastS(), 0);
    }
}

/// @dev A stabilizer whose book cannot be read: the loud failure.
contract RevertingBook is IStabilizer {
    error Down();
    function netInventory() external pure returns (int256) { revert Down(); }
    function capacity() external pure returns (uint256) { return 1e18; }
    function saturation() external pure returns (uint256) { return 0; }
    function positionCap() external pure returns (uint256) { return 1e12; }
}
