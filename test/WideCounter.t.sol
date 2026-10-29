// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {WideCounter} from "../src/lib/WideCounter.sol";

contract WideCounterHarness {
    using WideCounter for WideCounter.Count;
    WideCounter.Count private counter;
    function add(uint256 scaled) external { counter.add(scaled); }
    function count() external view returns (WideCounter.Count memory) { return counter; }
    function preview(uint256 scaled) external view returns (WideCounter.Count memory) {
        return WideCounter.plus(counter, scaled);
    }
}

contract WideCounterTest is Test {
    uint256 constant Q = 1e36;
    WideCounterHarness h;
    function setUp() public { h = new WideCounterHarness(); }

    function test_fractionalFlowsCarryIntoTheWholeUnitCounter() public {
        h.add(2 * Q + 3 * Q / 4);
        h.add(Q / 2);
        WideCounter.Count memory c = h.count();
        assertEq(c.units, 3);
        assertEq(c.fraction, Q / 4);
    }
    function test_lifetimeTotalCanExceedASingleScaledUint256() public {
        h.add(type(uint256).max);
        h.add(type(uint256).max);
        WideCounter.Count memory c = h.count();
        uint256 remainder = type(uint256).max % Q;
        assertEq(c.units, 2 * (type(uint256).max / Q) + 2 * remainder / Q);
        assertEq(c.fraction, 2 * remainder % Q);
        assertGt(c.units, type(uint80).max);
    }
    function test_previewIncludesPendingFlowWithoutChangingStorage() public {
        h.add(Q / 2);
        WideCounter.Count memory preview = h.preview(Q);
        WideCounter.Count memory original = h.count();
        assertEq(preview.units, 1);
        assertEq(preview.fraction, Q / 2);
        assertEq(original.units, 0);
        assertEq(original.fraction, Q / 2);
    }
    function testFuzz_groupingScaledFlowsDoesNotChangeTheCount(uint128 a, uint128 b) public {
        h.add(a);
        h.add(b);
        WideCounter.Count memory c = h.count();
        uint256 sum = uint256(a) + b;
        assertEq(c.units, sum / Q);
        assertEq(c.fraction, sum % Q);
    }
}
