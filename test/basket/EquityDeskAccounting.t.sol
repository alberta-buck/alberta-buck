// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {EquityDeskTest} from "./EquityDesk.t.sol";
import {MonetaryDesk} from "../../src/basket/MonetaryDesk.sol";
import {BuckBasketStorage} from "../../src/basket/BuckBasketStorage.sol";

contract EquityDeskAccountingTest is EquityDeskTest {
    function _accountMatches() internal view {
        int256 actual = buck.signedBalanceOf(address(desk));
        assertEq(desk.monetaryBuckHeld(), actual > 0 ? uint256(actual) : 0);
        assertEq(desk.monetaryOutstanding(), actual < 0 ? -actual : int256(0));
        assertEq(desk.markNow(), desk.netValue(), "fresh end mark");
    }

    function test_accounting_persistentAbsorbDoesNotPretendToBurnHeldBuck() public {
        _placed(); _grantDesk(1, 50_000e18);
        md.set(-50, false); desk.monetaryOperation();
        uint256 held = desk.monetaryBuckHeld();
        md.set(-50, true);
        assertEq(desk.monetaryOperation(), 1, "ordinary holder accumulates, cannot burn");
        assertGt(desk.monetaryBuckHeld(), held);
        assertEq(desk.monetaryOutstanding(), 0);
        _accountMatches();
    }

    function test_accounting_receiptsRepayBeforeBecomingInventory() public {
        _placed(); _grantDesk(1, 50_000e18);
        md.set(50, true); desk.monetaryOperation();
        uint256 debt = _deskLien();
        md.set(-10, false); assertEq(desk.monetaryOperation(), 2);
        assertLt(_deskLien(), debt);
        assertEq(desk.monetaryBuckHeld(), 0);
        _accountMatches();
    }

    function test_accounting_clipsToRemainingInventoryCap() public {
        _placed(); _grantDesk(1, 50_000e18);
        vm.prank(GOV);
        desk.setOpsParams(MonetaryDesk.OpsParams(40, 1, 1000, true));
        uint256 cap = b.equity() / 10000;
        md.set(-50, false); desk.monetaryOperation();
        assertGt(desk.monetaryBuckHeld(), 0);
        assertLe(desk.monetaryBuckHeld(), cap);
        _accountMatches();
    }

    function test_accounting_clipsToRemainingDebtCap() public {
        _placed(); _grantDesk(1, 50_000e18);
        vm.prank(GOV);
        desk.setOpsParams(MonetaryDesk.OpsParams(40, 1000, 1, true));
        uint256 cap = b.equity() / 10000;
        md.set(50, true); desk.monetaryOperation();
        assertGt(_deskLien(), 0);
        assertLe(_deskLien(), cap);
        _accountMatches();
    }

    function test_accounting_turnoverBudgetDoesNotReplenishOnReversal() public {
        _placed(); _grantDesk(1, 50_000e18);
        vm.prank(GOV); desk.setRiskBudget(100 * B, 0, 500);
        md.set(-50, false); desk.monetaryOperation();
        uint256 remaining = desk.turnoverRemaining();
        assertLt(remaining, 100 * B);
        md.set(50, false); desk.monetaryOperation();
        assertLt(desk.turnoverRemaining(), remaining);
        _accountMatches();
    }

    function test_accounting_capitalFloorRollsBackWholeOperation() public {
        _placed(); _grantDesk(1, 50_000e18);
        uint256 floor = desk.netValue();
        vm.prank(GOV); desk.setRiskBudget(1_000_000 * B, floor, 500);
        md.set(-50, false);
        vm.expectRevert(BuckBasketStorage.MonetaryBound.selector);
        desk.monetaryOperation();
        assertEq(desk.monetaryTokenHeld(1), 50_000e18);
        assertEq(desk.turnoverRemaining(), 1_000_000 * B);
        assertEq(desk.lastMonetaryEpoch(), 0);
    }

    function test_accounting_unavailableTwapDoesNotSpend() public {
        _placed(); _grantDesk(1, 50_000e18);
        // A manipulated current spot is not a substitute for the TWAP.
        _push(1, true, 200_000 * B);
        md.set(-50, false);
        vm.expectRevert(BuckBasketStorage.Slippage.selector);
        desk.monetaryOperation();
        assertEq(desk.monetaryTokenHeld(1), 50_000e18);
    }
}
