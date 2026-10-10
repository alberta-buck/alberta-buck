// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BlockPairSignalsTest} from "./BlockPairSignals.t.sol";
import {BlockPairSignals} from "../../src/basket/BlockPairSignals.sol";
import {PairRebalancePolicy} from "../../src/basket/PairRebalancePolicy.sol";

contract PairBookView {
    BlockPairSignals immutable s;
    constructor(BlockPairSignals s_) { s = s_; }
    function constituentsLength() external view returns (uint256) { return s.tokenCount(); }
    function constituents(uint256 i) external view returns (address, uint8, uint256, uint256, uint24, address, int24, int24, bool, uint256, uint128) {
        (address pool, bool token0,) = s.routes(i);
        return (s.tokens(i), 18, 0, 0, 3000, pool, 0, 0, !token0, 3333, 0);
    }
}

contract PairRebalancePolicyTest is BlockPairSignalsTest {
    // The first experiment's sizing: 10%/day braking release, closure by speed alone.
    function _first() private pure returns (PairRebalancePolicy.Sizing memory) {
        return PairRebalancePolicy.Sizing(2e15, 1e17, 20, 0, 1e18, 1e18);
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

    function test_policy_fractionIsTimeScaledAndCatchUpIsBounded() public {
        PairRebalancePolicy p = _policy(address(new PairBookView(s)), 0);
        assertEq(p.fraction(1e18, 0), 0);
        assertLt(p.fraction(1e18, 12), p.fraction(1e18, 3600));
        assertEq(p.fraction(1e18, 1 days), 0.5e18);
        assertEq(p.fraction(1e18, 30 days), p.fraction(1e18, 1 days));
    }

    function test_policy_onlyExecutedTransfersResetElapsedBudget() public {
        address book = address(new PairBookView(s));
        PairRebalancePolicy p = _policy(book, 0);
        uint64 deployedAt = p.lastExecutionAt();
        warm(); p.observe();
        assertGt(block.timestamp, deployedAt);
        assertEq(p.lastExecutionAt(), deployedAt, "sampling preserves unspent elapsed time");
        vm.prank(book); p.recordImpact(0, 0, 40);
        assertEq(p.lastExecutionAt(), deployedAt, "other basket operations preserve the pair clock");
        vm.expectRevert(bytes("basket only")); p.recordExecution();
        vm.prank(book); p.recordExecution();
        assertEq(p.lastExecutionAt(), block.timestamp);
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
}
