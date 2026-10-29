// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {JubileeReserveMath} from "../src/lib/JubileeReserveMath.sol";

contract JubileeReserveMathHarness {
    function factor(uint256 elapsed) external pure returns (uint256) { return JubileeReserveMath.factor(elapsed); }
    function accrue(uint256 budget, uint256 excess, uint256 elapsed) external pure returns (uint256) {
        return JubileeReserveMath.accrue(budget, excess, elapsed);
    }
}

contract JubileeReserveMathTest is Test {
    uint256 constant Q = 1e36;
    JubileeReserveMathHarness h;
    function setUp() public { h = new JubileeReserveMathHarness(); }

    function test_oneDayAccumulatesHalfTheObservedGap() public view {
        assertApproxEqAbs(h.factor(1 days), Q / 2, 1e12);
        assertApproxEqAbs(h.accrue(10 * Q, 110 * Q, 1 days), 60 * Q, 1e14);
    }
    function test_twoDaysLeavesOneQuarterAndImmediateCheckpointAddsNothing() public view {
        assertApproxEqAbs(h.factor(2 days), Q / 4, 1e12);
        assertEq(h.accrue(10 * Q, 110 * Q, 0), 10 * Q);
    }
    function test_excessBelowExistingBudgetIsCappedImmediately() public view {
        assertEq(h.accrue(100 * Q, 12 * Q, 1 days), 12 * Q);
    }
    function test_longIntervalAndMaximumValuesRemainBounded() public view {
        assertEq(h.factor(type(uint64).max), 0);
        assertEq(h.accrue(0, type(uint256).max, type(uint64).max), type(uint256).max);
    }
    function testFuzz_budgetAlwaysStaysWithinObservedExcess(uint128 budget, uint128 excess, uint32 elapsed)
        public view
    {
        uint256 result = h.accrue(budget, excess, elapsed);
        assertLe(result, excess);
        if (budget <= excess) assertGe(result, budget);
    }
    function testFuzz_checkpointFrequencyChangesOnlyFractionalRounding(uint64 excess, uint32 time1, uint32 time2)
        public view
    {
        uint256 total = uint256(time1) + time2;
        uint256 amount = uint256(excess) * Q;
        uint256 once = h.accrue(0, amount, total);
        uint256 twice = h.accrue(h.accrue(0, amount, time1), amount, time2);
        assertApproxEqAbs(once, twice, 1e20);
    }
}
