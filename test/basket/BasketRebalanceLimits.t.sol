// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BasketRebalanceMath} from "../../src/basket/BasketRebalanceMath.sol";

contract BasketRebalanceLimitsTest is Test {
    function triple(uint256 a, uint256 b, uint256 c) private pure returns (uint256[] memory row) {
        row = new uint256[](3);
        (row[0], row[1], row[2]) = (a, b, c);
    }

    function testReportsOnlyCapsThatChangeThePlan() public pure {
        (uint256[] memory sells, uint256[] memory buys, uint8[] memory sellLimits, uint8[] memory buyLimits) =
            BasketRebalanceMath.limitRebalance(triple(40, 9, 15), triple(40, 60, 1), triple(200, 100, 100),
                triple(140, 300, 10), triple(20, 50, 8), 1000, 1, 0);
        assertEq(sells, triple(20, 9, 8));
        assertEq(buys, triple(11, 25, 0));
        assertEq(sellLimits[0], 3, "equal step and depth limits");
        assertEq(sellLimits[1], 0, "requested amount fits");
        assertEq(sellLimits[2], 2, "depth limited");
        assertEq(buyLimits[0], 5, "step and portfolio ceiling");
        assertEq(buyLimits[1], 5, "step and portfolio ceiling");
        assertEq(buyLimits[2], 0, "dust removed before any allocation");
    }

    function testNoHeadroomSuppressesPurchasesAfterSales() public pure {
        (, uint256[] memory buys,, uint8[] memory limits) = BasketRebalanceMath.limitRebalance(triple(5, 0, 0),
            triple(0, 10, 0), triple(100, 100, 100), triple(100, 100, 100), triple(20, 20, 20), 1000, 1, -5);
        assertEq(buys, triple(0, 0, 0));
        assertEq(limits[1], 4, "portfolio ceiling prevented the buy");
    }
}
