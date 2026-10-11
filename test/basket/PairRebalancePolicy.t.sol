// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {PairSignalsTest} from "./PairSignals.t.sol";
import {PairSignals} from "../../src/basket/PairSignals.sol";
import {PairRebalancePolicy} from "../../src/basket/PairRebalancePolicy.sol";

contract PairBookView {
    PairSignals immutable s;
    constructor(PairSignals s_) { s = s_; }
    function constituentsLength() external view returns (uint256) { return s.tokenCount(); }
    function constituents(uint256 i) external view returns (address, uint8, uint256, uint256, uint24, address, int24, int24, bool, uint256, uint128) {
        (address pool, bool token0,) = s.routes(i);
        return (s.tokens(i), 18, 0, 0, 3000, pool, 0, 0, !token0, 3333, 0);
    }
}

contract PairRebalancePolicyTest is PairSignalsTest {
    // The first experiment's sizing: 10%/day braking release, closure by speed alone.
    function _first() private pure returns (PairRebalancePolicy.Sizing memory) {
        return PairRebalancePolicy.Sizing(2e15, 1e17, 20, 0, 1e18, 1e18, 0, 0);
    }
    function _sized(address book, int128 anchorA, PairRebalancePolicy.Sizing memory sizing)
        private returns (PairRebalancePolicy)
    {
        int128[] memory anchors = new int128[](3); anchors[0] = anchorA;
        return new PairRebalancePolicy(address(s), book, anchors, sizing);
    }
    function _policy(address book, int128 anchorA) private returns (PairRebalancePolicy) {
        return _sized(book, anchorA, _first());
    }
    function test_policy_departureQuenchesBrakingReleasesAndClosureStrengthens() public {
        PairRebalancePolicy p = _policy(address(new PairBookView(s)), 0);
        p.observe(); assertEq(p.desire(0, 1), 0, "unready is not stationary");
        warm(); p.observe();
        for (uint256 k = 1; k <= 100; ++k) {
            setTicks(int24(int256(k * 12)), 0, 0, 0); step(12); p.observe();
        }
        assertEq(p.desire(0, 1), 0, "steady departure is quenched");
        for (uint256 k; k < 10; ++k) { step(12); p.observe(); }
        assertGt(s.pair(0, 1).rate, 0);
        uint256 braking = p.desire(0, 1);
        assertGt(braking, 0, "participate before the rate crosses zero");
        for (uint256 k = 1; k <= 60; ++k) {
            setTicks(1200 - int24(int256(k * 12)), 0, 0, 0); step(12); p.observe();
        }
        assertGt(p.desire(0, 1), braking, "closure increases participation");
        uint256 desire = p.desire(0, 1);
        p.observe(); p.observe(); assertEq(p.desire(0, 1), desire, "duplicate reads do not reset evidence");
    }

    function test_policy_ownImpactWidensUncertaintyAndRequiresBasketAuthority() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy p = _policy(book, -3000e18);
        warm(); p.observe();
        for (uint256 k = 1; k <= 100; ++k) {
            setTicks(-int24(int256(k * 12)), 0, 0, 0); step(12); p.observe();
        }
        uint256 before = p.desire(0, 1); assertGt(before, 0);
        vm.expectRevert(bytes("basket only")); p.recordImpact(0, 0, 4000);
        vm.prank(book); p.recordImpact(0, 0, 4000);
        assertLt(p.desire(0, 1), before);
        assertEq(p.remainingImpact(0), 4000e18);
        vm.warp(vm.getBlockTimestamp() + 2 days);
        assertEq(p.remainingImpact(0), 0);
    }

    uint256 constant LN2 = 693147180559945309;

    function _rates(uint256 a, uint256 b, uint256 c) internal pure returns (uint256[] memory r) {
        r = new uint256[](3); r[0] = a; r[1] = b; r[2] = c;
    }

    function test_policy_aRateOfLn2HalvesTheGapEachDay() public {
        PairRebalancePolicy p = _policy(address(new PairBookView(s)), 0);
        assertEq(p.share(0, LN2), 0, "no time, no share");
        vm.warp(vm.getBlockTimestamp() + 1 days);
        assertApproxEqAbs(p.share(0, LN2), 0.5e18, 1e12);
        vm.warp(vm.getBlockTimestamp() + 30 days);
        assertApproxEqAbs(p.share(0, LN2), 0.5e18, 1e12, "an unobserved stretch earns a day and no more");
    }

    function test_policy_theSameTimeReleasesTheSameShareHoweverOftenItIsAsked() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy once = _policy(book, 0);
        PairRebalancePolicy often = _policy(book, 0);
        uint256[] memory rates = _rates(LN2, 0, 0);
        // Twelve hours asked in 3,600 blocks of twelve seconds, or once.
        for (uint256 k; k < 3600; ++k) {
            vm.warp(vm.getBlockTimestamp() + 12);
            vm.prank(book); often.recordRates(rates);
        }
        assertApproxEqAbs(often.share(0, LN2), once.share(0, LN2), 1e12);
        assertApproxEqAbs(once.share(0, LN2), 1e18 - 707106781186547524, 1e12, "half a day: 1 - 1/sqrt(2)");
        assertEq(often.share(1, 0), 0, "a constituent not asked accrues nothing");
    }

    function test_policy_allowanceIsSpentByItsDonorAndLostWhenTheAskIsWithdrawn() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy p = _policy(book, 0);
        vm.expectRevert(bytes("basket only")); p.recordRates(_rates(LN2, LN2, 0));
        vm.warp(vm.getBlockTimestamp() + 1 days);
        vm.prank(book); p.recordRates(_rates(LN2, LN2, 0));
        assertApproxEqAbs(p.share(0, 0), 0.5e18, 1e12); assertApproxEqAbs(p.share(1, 0), 0.5e18, 1e12);
        vm.expectRevert(bytes("basket only")); p.recordExecution(0);
        vm.prank(book); p.recordExecution(0);
        assertEq(p.lastExecutionAt(), block.timestamp);
        assertEq(p.share(0, 0), 0, "the donor that transferred starts again");
        assertApproxEqAbs(p.share(1, 0), 0.5e18, 1e12, "another donor keeps what it has accrued");
        vm.warp(vm.getBlockTimestamp() + 6 hours);
        vm.prank(book); p.recordRates(_rates(LN2, 0, 0));
        assertEq(p.share(1, 0), 0, "an ask withdrawn forfeits its allowance");
        assertGt(p.share(0, 0), 0);
        vm.warp(vm.getBlockTimestamp() + 60 days);
        for (uint256 k; k < 40; ++k) { vm.warp(vm.getBlockTimestamp() + 1 days); vm.prank(book); p.recordRates(_rates(4e18, 0, 0)); }
        assertLt(p.share(0, 4e18), 1e18, "never the whole gap");
        assertGt(p.share(0, 4e18), 0.98e18);
    }

    function test_policy_crossedReferenceAndLiveCrossingStopReversionSales() public {
        PairRebalancePolicy p = _policy(address(new PairBookView(s)), 0);
        setTicks(3000, 0, 0, 0); warm(); p.observe();
        for (uint256 k = 1; k <= 100; ++k) {
            setTicks(3000 - int24(int256(k * 12)), 0, 0, 0); step(12); p.observe();
        }
        assertGt(p.desire(0, 1), 0, "dear TOKEN closing toward history can be released");
        assertEq(p.desire(1, 0), 0, "cheap TOKEN is not a reversion seller");
        int256 rate = s.pair(0, 1).rate;
        setTicks(-500, 0, 0, 0); // Same block: filtered prices and velocity still lag.
        assertEq(s.pair(0, 1).rate, rate);
        assertEq(p.desire(0, 1), 0, "live crossing vetoes stale filtered closure");
        for (uint256 k = 1; k <= 100; ++k) {
            setTicks(-500 - int24(int256(k * 12)), 0, 0, 0); step(12); p.observe();
        }
        assertLt(s.pair(0, 1).high, 0);
        assertEq(p.desire(0, 1), 0, "negative velocity beyond the reference is departure");
    }

    function test_policy_quietEqualityAndSubMinimumSeparationHaveNoReversionIntent() public {
        PairRebalancePolicy p = _policy(address(new PairBookView(s)), 0);
        warm(); p.observe(); assertEq(p.desire(0, 1), 0);
        setTicks(50, 0, 0, 0);
        for (uint256 k; k < 100; ++k) { step(12); p.observe(); }
        assertEq(p.desire(0, 1), 0, "sub-minimum displacement is not a spread opportunity");
        PairRebalancePolicy normalized = _policy(address(new PairBookView(s)), 2000e18);
        setTicks(1000, 0, 0, 0);
        for (uint256 k; k < 100; ++k) { step(12); normalized.observe(); }
        assertEq(normalized.desire(0, 1), 0, "raw higher price can be below its relative reference");
    }

    function test_policy_sizingIsValidatedAndPublished() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy.Sizing memory z = _first();
        PairRebalancePolicy p = _sized(book, 0, z);
        assertEq(p.releaseSpeed(), 2e15); assertEq(p.releaseRate(), 1e17); assertEq(p.closureGain(), 20);
        assertEq(p.closureRate(), 0); assertEq(p.maxRate(), 1e18); assertEq(p.impactShare(), 1e18);
        int128[] memory anchors = new int128[](3);
        z.maxRate = 0;
        vm.expectRevert(bytes("sizing")); new PairRebalancePolicy(address(s), book, anchors, z);
        z = _first(); z.impactShare = 1e18 + 1;
        vm.expectRevert(bytes("sizing")); new PairRebalancePolicy(address(s), book, anchors, z);
    }

    /// A slow reversal, equally clean: speed alone earns it under 5% of the
    /// gap a day; confidence earns it the same full closure rate as a fast one.
    function test_policy_closureConfidenceDoesNotDependOnSpeed() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy bySpeed = _sized(book, 0, _first());
        PairRebalancePolicy.Sizing memory z = _first();
        z.releaseRate = 0; z.closureGain = 0; z.closureRate = 5e17;
        PairRebalancePolicy byConfidence = _sized(book, 0, z);
        setTicks(3000, 0, 0, 0); warm(); bySpeed.observe(); byConfidence.observe();
        // One tick an hour: 24 log bp/day, sampled hourly so the ramp is clean.
        for (uint256 k = 1; k <= 40; ++k) {
            setTicks(3000 - int24(int256(k)), 0, 0, 0); step(3600); bySpeed.observe(); byConfidence.observe();
        }
        assertLt(s.pair(0, 1).high, 0, "slow closure clears its allowance");
        uint256 slow = bySpeed.desire(0, 1);
        assertGt(slow, 0); assertLt(slow, 5e16, "speed alone: under 5%/day for a slow turn");
        assertEq(byConfidence.desire(0, 1), 5e17, "a clean slow turn earns the whole closure rate");
        assertEq(byConfidence.desire(1, 0), 0, "and only for the dear TOKEN");
    }

    function test_policy_closureConfidenceGrowsWithEvidenceAndIsCapped() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy.Sizing memory z = _first();
        z.releaseRate = 0; z.closureGain = 0; z.closureRate = 2e18; z.maxRate = 15e17;
        PairRebalancePolicy p = _sized(book, 0, z);
        setTicks(3000, 0, 0, 0); warm(); p.observe();
        uint256 last; bool graded;
        for (uint256 k = 1; k <= 60; ++k) {
            setTicks(3000 - int24(int256(k * 12)), 0, 0, 0); step(12); p.observe();
            uint256 d = p.desire(0, 1);
            if (d > 0 && d < 15e17) graded = true;
            assertGe(d, last, "evidence accumulates as the closure persists");
            last = d;
        }
        assertTrue(graded, "confidence is graded, not a switch");
        assertEq(last, 15e17, "the ceiling bounds the sum");
    }

    /// The first sizing halves the release at 20 log bp/day outward, whatever
    /// the pair. With no fixed scale the rate's own allowance sets it, so a
    /// fast pair that is plainly slowing is released sooner.
    function test_policy_releaseScaleDefaultsToTheRatesOwnAllowance() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy fixedScale = _sized(book, 0, _first());
        PairRebalancePolicy.Sizing memory z = _first(); z.releaseSpeed = 0;
        PairRebalancePolicy ownScale = _sized(book, 0, z);
        warm(); fixedScale.observe(); ownScale.observe();
        for (uint256 k = 1; k <= 100; ++k) {
            setTicks(int24(int256(k * 12)), 0, 0, 0); step(12); fixedScale.observe(); ownScale.observe();
        }
        assertEq(ownScale.desire(0, 1), 0, "steady departure is quenched either way");
        for (uint256 k; k < 10; ++k) { step(12); fixedScale.observe(); ownScale.observe(); }
        assertGt(s.pair(0, 1).rate, 0, "still outward, and slowing");
        assertGt(fixedScale.desire(0, 1), 0);
        assertGt(ownScale.desire(0, 1), fixedScale.desire(0, 1), "released sooner than a 20 bp/day scale allows");
        assertLe(ownScale.desire(0, 1), ownScale.releaseRate(), "never beyond the release rate");
    }

    function test_policy_impactShareScalesOwnAllowance() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy.Sizing memory z = _first(); z.closureGain = 0; z.closureRate = 1e18;
        PairRebalancePolicy whole = _sized(book, -3000e18, z);
        z.impactShare = 0;
        PairRebalancePolicy none = _sized(book, -3000e18, z);
        warm(); whole.observe(); none.observe();
        for (uint256 k = 1; k <= 100; ++k) {
            setTicks(-int24(int256(k * 12)), 0, 0, 0); step(12); whole.observe(); none.observe();
        }
        uint256 before = whole.desire(0, 1);
        assertGt(before, 0); assertEq(none.desire(0, 1), before);
        vm.startPrank(book); whole.recordImpact(0, 0, 4000); none.recordImpact(0, 0, 4000); vm.stopPrank();
        assertLt(whole.desire(0, 1), before, "its own impact widens the allowance");
        assertEq(none.desire(0, 1), before, "unless the policy is told to disregard it");
        assertEq(none.remainingImpact(0), 4000e18, "the measurement is kept either way");
    }

    // A quarter point of slack and two point-days of patience.
    function _patient(address book) private returns (PairRebalancePolicy) {
        PairRebalancePolicy.Sizing memory z = _first(); z.slack = 0.0025e18; z.patience = 0.02e18;
        return _sized(book, 0, z);
    }
    function _gaps(int256 a, int256 b, int256 c) private pure returns (int256[] memory gaps) {
        gaps = new int256[](3); gaps[0] = a; gaps[1] = b; gaps[2] = c;
    }

    /// A gap of 2.25 points qualifies in a day, 1.25 in two, and one inside
    /// the slack never does. Working a constituent starts its count again.
    function test_policy_urgencyBuildsWithGapAndTimeAndStaysOpenUntilWorkedOff() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy p = _patient(book);
        assertEq(p.slack(), 0.0025e18); assertEq(p.patience(), 0.02e18);
        int256[] memory gaps = _gaps(0.0225e18, -0.0125e18, -0.002e18);
        assertEq(p.urgent(gaps), 0, "nothing is urgent at once, however large");
        vm.expectRevert(bytes("basket only")); p.recordGaps(gaps);
        vm.warp(vm.getBlockTimestamp() + 12 hours);
        vm.prank(book); p.recordGaps(gaps);
        assertEq(p.urgencyOf(0, gaps[0]), 0.02e18 * 12 hours);
        assertEq(p.urgent(gaps), 0, "half a day is half the patience for 2.25 points");
        vm.warp(vm.getBlockTimestamp() + 12 hours);
        assertEq(p.urgent(gaps), 1, "the view carries the count to the present before any report");
        vm.prank(book); p.recordGaps(gaps);
        assertEq(p.urgent(gaps), 1);
        vm.warp(vm.getBlockTimestamp() + 1 days);
        vm.prank(book); p.recordGaps(gaps);
        assertEq(p.urgent(gaps), 3, "1.25 points takes two days");
        assertEq(p.urgencyOf(0, gaps[0]), 0.02e18 * 1 days, "and the count stops at the full patience");
        assertEq(p.urgencyOf(2, gaps[2]), 0, "a gap inside the slack builds nothing");
        vm.prank(book); p.recordExecution(0);
        assertEq(p.urgent(gaps), 3, "a transfer leaves both eligible: the gap stays open until it is worked off");
        vm.warp(vm.getBlockTimestamp() + 6 hours);
        vm.prank(book); p.recordGaps(_gaps(0.002e18, -0.0125e18, -0.002e18));
        assertEq(p.urgent(_gaps(0.002e18, -0.0125e18, -0.002e18)), 2, "back inside the slack, it closes at once");
    }

    function test_policy_urgencyDrainsInsideTheSlackAndRestartsAcrossTarget() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy p = _patient(book);
        vm.warp(vm.getBlockTimestamp() + 12 hours);
        vm.prank(book); p.recordGaps(_gaps(0.0225e18, 0, 0));
        uint256 built = p.urgencyOf(0, 0.0225e18);
        vm.warp(vm.getBlockTimestamp() + 6 hours);
        vm.prank(book); p.recordGaps(_gaps(0.0005e18, 0, 0));
        assertEq(p.urgencyOf(0, 0.0005e18), built - 0.002e18 * 6 hours, "inside the slack it drains at the shortfall");
        vm.warp(vm.getBlockTimestamp() + 6 hours);
        assertEq(p.urgencyOf(0, -0.0225e18), 0.02e18 * 6 hours, "across target nothing carries over");
        vm.prank(book); p.recordGaps(_gaps(-0.0225e18, 0, 0));
        (uint128 sum, int8 side) = p.urgency(0);
        assertEq(sum, 0.02e18 * 6 hours); assertEq(side, -1);
        vm.warp(vm.getBlockTimestamp() + 30 days);
        vm.prank(book); p.recordGaps(_gaps(0.0005e18, 0, 0));
        (sum,) = p.urgency(0);
        assertEq(sum, 0, "a long quiet stretch empties it");
    }

    function test_policy_urgencyCatchUpIsBoundedAndNoPatienceKeepsNoCount() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy.Sizing memory z = _first(); z.slack = 0.0025e18; z.patience = 0.2e18;
        PairRebalancePolicy p = _sized(book, 0, z);
        int256[] memory gaps = _gaps(0.0225e18, -0.0225e18, 0);
        vm.warp(vm.getBlockTimestamp() + 30 days);
        assertEq(p.urgencyOf(0, gaps[0]), 0.02e18 * 1 days, "an unobserved month earns one day");
        vm.prank(book); p.recordGaps(gaps);
        assertEq(p.gapsAt(), vm.getBlockTimestamp());
        assertEq(p.urgent(gaps), 0, "twenty point-days are not built in one report");
        int256[] memory short_ = new int256[](2);
        vm.expectRevert(bytes("gaps size")); p.urgent(short_);
        PairRebalancePolicy band = _policy(book, 0);
        uint64 deployedAt = band.gapsAt();
        vm.warp(vm.getBlockTimestamp() + 3 days);
        vm.prank(book); band.recordGaps(gaps);
        assertEq(band.urgent(gaps), 0, "with no patience the basket's band decides");
        assertEq(band.gapsAt(), deployedAt); assertEq(band.urgencyOf(0, gaps[0]), 0);
    }
}
