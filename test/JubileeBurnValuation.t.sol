// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckNet} from "../src/BuckNet.sol";
import {JubileeFund} from "../src/JubileeFund.sol";
import {JubileeFundTest} from "./JubileeFund.t.sol";
import {TestWheel} from "./wheel/WorkWheel.t.sol";

/// @dev Test-only entry for the future burn's valuation hook. It uses the
///      actual BuckNet non-reentrancy modifier, not a separate mock lock.
contract ValuationLockedBuck is BuckNet {
    constructor(address credit, address controller, address identity, address fund)
        BuckNet(credit, controller, identity, fund) {}

    function probeBurnValuation() external nonReentrant returns (uint256) {
        (bool ok, bytes memory reason) = address(this).call(
            abi.encodeWithSelector(BuckNet.transfer.selector, msg.sender, 1));
        require(!ok && bytes4(reason) == BuckNet.Reentrant.selector, "token guard not held");
        return JubileeFund(jubilee).refreshBurnValuation();
    }
}

contract JubileeBurnValuationTest is JubileeFundTest {
    function _newBuck() internal override returns (BuckNet) {
        return new ValuationLockedBuck(address(credit), address(k), address(identity), address(fund));
    }

    function test_burnValuationUsesTheWheelWhileTheRealTokenGuardIsHeld() public {
        _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(2e6);
        TestWheel wheel = _valuationWheel();
        wheel.setA(1);
        wheel.setAPending(0, 1);
        vm.expectRevert(JubileeFund.NotToken.selector);
        fund.refreshBurnValuation();
        vm.expectRevert(JubileeFund.MarkUnavailable.selector);
        fund.reliefPositionMark();
        (uint256 grossBefore, uint256 cashBefore) = buck.fundBalancesScaled();
        (uint128 sharesBefore, uint128 basisBefore) = basket.holdings(id);
        uint256 issuedBefore = buck.totalIssued();
        uint256 aliceBefore = buck.balanceOf(ALICE);
        vm.prank(ALICE);
        uint256 mark = ValuationLockedBuck(address(buck)).probeBurnValuation();
        assertGt(mark, 0);
        assertEq(mark, wheel.valuationMark());
        assertEq(mark, fund.reliefPositionMark());
        assertEq(wheel.valuationBlock(), block.number);
        assertEq(wheel.cursor(), 0);
        assertEq(wheel.aPending(0), 1);
        assertEq(wheel.reserve(), 0);
        assertEq(buck.totalIssued(), issuedBefore);
        assertEq(buck.balanceOf(ALICE), aliceBefore);
        (uint256 grossAfter, uint256 cashAfter) = buck.fundBalancesScaled();
        (uint128 sharesAfter, uint128 basisAfter) = basket.holdings(id);
        assertEq(grossAfter, grossBefore);
        assertEq(cashAfter, cashBefore);
        assertEq(sharesAfter, sharesBefore);
        assertEq(basisAfter, basisBefore);

        basket.realizeLoss(BOB, 100_000);
        assertEq(fund.reliefPositionMark(), mark); // quotes retain the last publication
        vm.prank(ALICE);
        uint256 lower = ValuationLockedBuck(address(buck)).probeBurnValuation();
        assertLt(lower, mark);
        assertEq(lower, fund.reliefPositionMark());

        _invest(250_000); // same receipt, new shares and basis invalidate the quote
        vm.expectRevert(JubileeFund.MarkUnavailable.selector);
        fund.reliefPositionMark();
        vm.prank(ALICE);
        uint256 refreshed = ValuationLockedBuck(address(buck)).probeBurnValuation();
        assertEq(refreshed, fund.reliefPositionMark());
        assertGt(refreshed, mark);
        assertEq(wheel.aPending(0), 1);
        assertEq(wheel.cursor(), 0);
    }

    function test_emptyPositionNeedsNoWheelPublication() public {
        TestWheel wheel = _valuationWheel();
        vm.prank(ALICE);
        assertEq(ValuationLockedBuck(address(buck)).probeBurnValuation(), 0);
        assertEq(fund.reliefPositionMark(), 0);
        assertFalse(wheel.valuationReady());
    }

    function test_burnValuationRejectsAChangedWheelSource() public {
        _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        _invest(2e6);
        TestWheel wheel = _valuationWheel();
        vm.prank(GOV);
        wheel.setValuationSource(address(basket));
        vm.expectRevert(JubileeFund.MarkUnavailable.selector);
        vm.prank(ALICE);
        ValuationLockedBuck(address(buck)).probeBurnValuation();
        assertFalse(wheel.valuationReady());
    }

    function test_unconfiguredWheelRetainsTheExplicitLiveMarkFallback() public {
        _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        _invest(2e6);
        uint256 observed = fund.reliefPositionMark();
        assertGt(observed, 0);
        vm.prank(ALICE);
        assertEq(ValuationLockedBuck(address(buck)).probeBurnValuation(), observed);
    }
}
