// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityTest, IBasketUpkeep} from "./BuckBasketEquity.t.sol";

contract BasketArbIncomeTest is BuckBasketEquityTest {
    function test_arbIncomeAuthorityCannotSpendOrPerformUpkeep() public {
        _placed();
        vm.prank(alice);
        vm.expectRevert();
        b.setArbitrageCreditor(alice);
        vm.prank(GOV);
        b.setArbitrageCreditor(alice);
        uint256 beforeEquity = b.equity();
        uint256 beforeLien = b.lien();
        vm.startPrank(alice);
        buck.approve(address(b), 100 * B);
        b.creditTreasury(100 * B);
        vm.expectRevert();
        IBasketUpkeep(address(b)).wheelStep(0, 0);
        vm.stopPrank();
        assertApproxEqAbs(b.equity() - beforeEquity, 100 * B, 2);
        assertApproxEqAbs(beforeLien - b.lien(), 100 * B, 2);
        vm.prank(GOV);
        b.setArbitrageCreditor(address(0));
        vm.prank(alice);
        vm.expectRevert();
        b.creditTreasury(1);
        // The existing wheel's income authority is unaffected.
        buck.approve(address(b), B);
        b.creditTreasury(B);
        _books();
    }

    function test_arbTokenIncomeIsBackedAndRevocationAppliesToBothAssets() public {
        vm.prank(GOV);
        b.setArbitrageCreditor(alice);
        uint256 idle = b.idleToken(0);
        vm.startPrank(alice);
        tok[0].approve(address(b), 5e18);
        b.creditDepositors(0, 5e18);
        vm.stopPrank();
        assertEq(b.idleToken(0), idle + 5e18);
        vm.prank(GOV);
        b.setArbitrageCreditor(address(0));
        vm.prank(alice);
        vm.expectRevert();
        b.creditDepositors(0, 1);
        _books();
    }
}
