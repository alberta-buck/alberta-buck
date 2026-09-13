// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {SimStabilizer}   from "../src/sim_build/SimStabilizer.sol";
import {ShadowObserver}  from "../src/basket/ShadowObserver.sol";
import {MockDepthBasket} from "./BuckKControllerShadow.t.sol";

/// @title SimStabilizerTest -- WP-14 (WAVE3.org decision 17): the sim-only
///        per-class stabilizer against the REAL observer.
///
/// The three outcomes of `positionCap()` (IStabilizer; decision 9) are
/// asserted through `ShadowObserver.observe()`: a positive cap is held and
/// includes the class; zero excludes it and renormalizes the V weights; a
/// revert holds the last good cap, flags it stale and keeps the position
/// readable.  The capacity / saturation arithmetic is the seam's.  The two
/// aggregate tests are the L3 argument of the package: under S the
/// per-class registry at the same lambda reproduces the lumped
/// pseudo-stabilizer's number exactly (the sum is the same integer), and
/// under V the decision-11 weights give sum w_i q_i / cap_i over the
/// INCLUDED classes, computable by hand.
contract SimStabilizerTest is Test {

    address GOV = makeAddr("governance");
    address ANON = makeAddr("anon");

    uint256 constant D = 10_000_000e6;          // the pools' BUCK depth (S)

    MockDepthBasket internal basket;
    ShadowObserver  internal obs;
    SimStabilizer   internal uts;               // undertakings, strong side
    SimStabilizer   internal utw;               // undertakings, weak side
    SimStabilizer   internal fac;               // the facility population
    SimStabilizer   internal sd;                // the seeder

    function setUp() public {
        basket = new MockDepthBasket();
        basket.setBasketValue(int256(1e18));
        basket.setDepth(D);
        obs = new ShadowObserver(address(basket), GOV);
        uts = new SimStabilizer(GOV, "uts");
        utw = new SimStabilizer(GOV, "utw");
        fac = new SimStabilizer(GOV, "fac");
        sd  = new SimStabilizer(GOV, "sd");
    }

    function _register(SimStabilizer s, uint256 lambda, uint256 weight) internal {
        vm.prank(GOV); obs.addStabilizer(address(s), lambda);
        if (weight != 1e18) { vm.prank(GOV); obs.setStabilizerWeight(address(s), weight); }
    }

    function _bookAndCap(SimStabilizer s, int256 q, uint256 c) internal {
        vm.prank(GOV); s.setBookAndCap(q, c);
    }

    // ---- governance --------------------------------------------------------- //

    function test_governance_settersRevertForOthers() public {
        vm.startPrank(ANON);
        vm.expectRevert(SimStabilizer.NotGovernance.selector); uts.setBook(1);
        vm.expectRevert(SimStabilizer.NotGovernance.selector); uts.setCap(1);
        vm.expectRevert(SimStabilizer.NotGovernance.selector); uts.setBookAndCap(1, 1);
        vm.expectRevert(SimStabilizer.NotGovernance.selector); uts.setCapReverts(true);
        vm.expectRevert(SimStabilizer.NotGovernance.selector); uts.setGovernance(ANON);
        vm.stopPrank();
        vm.expectRevert(SimStabilizer.Gov0.selector);
        new SimStabilizer(address(0), "x");
        vm.prank(GOV);
        vm.expectRevert(SimStabilizer.Gov0.selector);
        uts.setGovernance(address(0));
        assertEq(uts.tag(), bytes32("uts"));
        assertEq(uts.governance(), GOV);
    }

    function test_setBookAndCap_oneTx_emits() public {
        vm.expectEmit(true, true, true, true, address(utw));
        emit SimStabilizer.BookSet(250_000e6, 1_000_000e6);
        _bookAndCap(utw, 250_000e6, 1_000_000e6);
        assertEq(utw.book(), 250_000e6);
        assertEq(utw.cap(), 1_000_000e6);
        assertEq(utw.netInventory(), 250_000e6);
        assertEq(utw.positionCap(), 1_000_000e6);
        vm.prank(GOV); utw.setBook(-40e6);
        assertEq(utw.netInventory(), -40e6);
        assertEq(utw.positionCap(), 1_000_000e6, "setBook keeps the cap");
        vm.prank(GOV); utw.setCap(7e6);
        assertEq(utw.netInventory(), -40e6, "setCap keeps the book");
        assertEq(utw.positionCap(), 7e6);
    }

    // ---- the seam's arithmetic ---------------------------------------------- //

    function test_capacity_saturation_arithmetic() public {
        // disabled (cap 0): no room, pinned -- like the disabled desk.
        assertEq(uts.capacity(), 0);
        assertEq(uts.saturation(), 1e18);
        // an empty book against a cap: all the room.
        _bookAndCap(uts, 0, 1_000_000e6);
        assertEq(uts.capacity(), 1e18);
        assertEq(uts.saturation(), 0);
        // a quarter absorbed.
        _bookAndCap(uts, 250_000e6, 1_000_000e6);
        assertEq(uts.capacity(), 0.75e18);
        assertEq(uts.saturation(), 0.25e18);
        // half issued: the sign does not matter to the fill.
        _bookAndCap(uts, -500_000e6, 1_000_000e6);
        assertEq(uts.capacity(), 0.5e18);
        assertEq(uts.saturation(), 0.5e18);
        // beyond the bound: clamped.
        _bookAndCap(uts, 1_500_000e6, 1_000_000e6);
        assertEq(uts.capacity(), 0);
        assertEq(uts.saturation(), 1e18);
        // the cap moved under the book (a facility's limit shrank with K).
        _bookAndCap(uts, 300_000e6, 400_000e6);
        assertEq(uts.capacity(), 0.25e18);
        assertEq(uts.saturation(), 0.75e18);
    }

    // ---- positionCap: the three outcomes, through the real observer ------- //

    /// A positive cap: read at registration, held, refreshed on observe();
    /// the class is included and its position is q / cap.
    function test_positionCap_positive_heldAndIncluded() public {
        _bookAndCap(utw, 500_000e6, 1_000_000e6);
        _register(utw, 1e18, 1e18);
        assertEq(obs.heldCap(address(utw)), 1_000_000e6, "read at registration");
        assertFalse(obs.stale(address(utw)));
        assertFalse(obs.excluded(address(utw)));
        assertEq(obs.position(address(utw)), 0.5e18);
        assertEq(obs.stabilizerSaturation(address(utw)), 0.5e18);
        // mode S: lambda * q / D.
        assertEq(obs.aggregatePosition(), int256(1e18) * 500_000e6 / int256(D));
        // the cap moves (NAV grew): the next observe() refreshes it.
        _bookAndCap(utw, 500_000e6, 2_000_000e6);
        assertEq(obs.heldCap(address(utw)), 1_000_000e6, "held until observed");
        obs.observe();
        assertEq(obs.heldCap(address(utw)), 2_000_000e6);
        assertEq(obs.position(address(utw)), 0.25e18);
    }

    /// A ZERO cap: the class is disabled -- excluded from s in both modes,
    /// the V weights renormalized without it, the excluded bit set; a
    /// class that never booked a cap (absent from the cell) is excluded
    /// from registration on.
    function test_positionCap_zero_excludedAndRenormalized() public {
        _bookAndCap(utw, 500_000e6, 1_000_000e6);    // fill +0.5, w 1
        _bookAndCap(fac, -200_000e6, 400_000e6);     // fill -0.5, w 0.5
        _register(utw, 1e18, 1e18);
        _register(fac, 1e18, 0.5e18);
        _register(sd, 1e18, 0.25e18);                // never booked: cap 0
        assertTrue(obs.excluded(address(sd)), "absent class excluded");
        assertFalse(obs.stale(address(sd)), "a successful zero read is not stale");
        vm.prank(GOV); obs.setMode(ShadowObserver.Mode.V);
        // (1 * 0.5 + 0.5 * -0.5) / (1 + 0.5) = 0.25 / 1.5
        assertEq(obs.aggregatePosition(), int256(0.25e36) / int256(1.5e18));
        (, uint256 ex) = obs.flags();
        assertEq(ex, uint256(1) << 2, "only the seeder's bit");
        // the facility disables (its population left): its weight leaves
        // the denominator and s is the weak side's fill alone.
        _bookAndCap(fac, -200_000e6, 0);
        obs.observe();
        assertTrue(obs.excluded(address(fac)));
        assertEq(obs.position(address(fac)), 0, "position 0 when excluded");
        assertEq(obs.aggregatePosition(), 0.5e18);
        (, ex) = obs.flags();
        assertEq(ex, (uint256(1) << 1) | (uint256(1) << 2));
        // and under S the disabled class drops out of the sum too (WP-13
        // open issue 2: exclusion applies in both units).
        vm.prank(GOV); obs.setMode(ShadowObserver.Mode.S);
        assertEq(obs.aggregatePosition(), int256(1e18) * 500_000e6 / int256(D));
    }

    /// A REVERT: the held cap stays, the class is flagged stale and stays
    /// included; the position is always readable (the NEW book against the
    /// held cap); the first successful read clears the flag.
    function test_positionCap_revert_holdsCapFlagsStale_clearsOnRead() public {
        _bookAndCap(utw, 500_000e6, 1_000_000e6);
        _register(utw, 1e18, 1e18);
        vm.prank(GOV); utw.setCapReverts(true);
        vm.expectRevert(SimStabilizer.CapUnreadable.selector);
        utw.positionCap();
        assertEq(utw.netInventory(), 500_000e6, "the book is readable");
        // the book moves while the cap is unreadable.
        vm.prank(GOV); utw.setBookAndCap(750_000e6, 5_000_000e6);
        obs.observe();
        assertEq(obs.heldCap(address(utw)), 1_000_000e6, "held, not the new 5M");
        assertTrue(obs.stale(address(utw)));
        assertFalse(obs.excluded(address(utw)), "still included");
        assertEq(obs.position(address(utw)), 0.75e18, "new book / held cap");
        assertEq(obs.aggregatePosition(), int256(1e18) * 750_000e6 / int256(D));
        (uint256 st, uint256 ex) = obs.flags();
        assertEq(st, 1); assertEq(ex, 0);
        // readable again: the next observe() clears stale and takes the cap.
        vm.prank(GOV); utw.setCapReverts(false);
        obs.observe();
        assertFalse(obs.stale(address(utw)));
        assertEq(obs.heldCap(address(utw)), 5_000_000e6);
        assertEq(obs.position(address(utw)), 0.15e18);
        (st, ) = obs.flags();
        assertEq(st, 0);
    }

    // ---- the aggregates: per class == the lumped sum (S); by hand (V) ----- //

    /// Under S at one lambda the per-class registry reproduces the lumped
    /// pseudo-stabilizer's number EXACTLY (sum lambda_i q_i is the same
    /// integer): the L3 argument for the ops30 identity in K.
    function test_modeS_perClassEqualsLumpedOffset() public {
        int256 qUts = -24_149e6;      // issued open
        int256 qUtw =  15_242e6;      // absorbed open
        int256 qFac = -1_337_001e6;   // drawn lines
        int256 qSd  = -2_500_000e6;   // a BUCK-funded range converted
        _bookAndCap(uts, qUts, 15_300_000e6);
        _bookAndCap(utw, qUtw, 15_300_000e6);
        _bookAndCap(fac, qFac, 4_000_000e6);
        _bookAndCap(sd,  qSd,  10_000_000e6);
        _register(uts, 1e18, 1e18);
        _register(utw, 1e18, 1e18);
        _register(fac, 1e18, 0.5e18);
        _register(sd,  1e18, 0.25e18);
        // the lumped twin: one pseudo-stabilizer at the same lambda.
        ShadowObserver lumped = new ShadowObserver(address(basket), GOV);
        vm.prank(GOV); lumped.setShadowLambda(1e18);
        vm.prank(GOV); lumped.setShadowOffset(qUts + qUtw + qFac + qSd);
        assertEq(obs.aggregatePosition(), lumped.aggregatePosition(), "same number");
        assertEq(obs.aggregatePosition(), (qUts + qUtw + qFac + qSd) * int256(1e18) / int256(D));
        assertEq(obs.shadowValueInBuck(), lumped.shadowValueInBuck());
        // the observer's per-class views carry each class's own fill.
        assertEq(obs.position(address(sd)), -0.25e18);
        assertEq(obs.position(address(fac)), int256(qFac) * int256(1e18) / int256(4_000_000e6));
    }

    /// Under V with decision 11's weights: s = sum w_i q_i / cap_i over the
    /// INCLUDED classes / sum w_i, computed by hand -- and the desk's empty
    /// book at weight 1 dilutes it as D7 specifies (decision 18).
    function test_modeV_decision11Weights_byHand() public {
        _bookAndCap(uts, -3_060_000e6, 15_300_000e6);   // fill -0.2
        _bookAndCap(utw,  7_650_000e6, 15_300_000e6);   // fill +0.5
        _bookAndCap(fac, -1_000_000e6,  4_000_000e6);   // fill -0.25
        _bookAndCap(sd,   1_000_000e6, 10_000_000e6);   // fill +0.1
        _register(uts, 0, 1e18);
        _register(utw, 0, 1e18);
        _register(fac, 0, 0.5e18);
        _register(sd,  0, 0.25e18);
        vm.prank(GOV); obs.setMode(ShadowObserver.Mode.V);
        // (1 * -0.2 + 1 * 0.5 + 0.5 * -0.25 + 0.25 * 0.1) / 2.75
        int256 num = int256(-0.2e36) + int256(0.5e36) + int256(-0.125e36) + int256(0.025e36);
        assertEq(obs.aggregatePosition(), num / int256(2.75e18));
        assertEq(obs.aggregatePosition(), int256(0.2e36) / int256(2.75e18));
        // the desk-shaped idle stabilizer at weight 1 (an empty book with a
        // cap) enters the denominator: 0.2 / 3.75.
        SimStabilizer desk = new SimStabilizer(GOV, "desk");
        _bookAndCap(desk, 0, 1_000_000e6);
        _register(desk, 0, 1e18);
        assertEq(obs.aggregatePosition(), int256(0.2e36) / int256(3.75e18));
        // saturation: the weighted max of the fills, w_i * |fill_i|.
        assertEq(obs.shadowSaturation(), 0.5e18);
    }
}
