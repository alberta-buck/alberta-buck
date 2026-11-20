// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckCompoundArbFixture} from "./BuckCompoundWheelArb.t.sol";
import {IBasketArb} from "../../src/basket/IBasketArb.sol";
import {IBuckFlashBorrower} from "../../src/IBuckFlashBorrower.sol";
import {BuckBasketStorage} from "../../src/basket/BuckBasketStorage.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {BasketMarketOps} from "../../src/basket/BasketMarketOps.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

/// @notice Flash arbitrage (owner, 2026-11-20): the machine's cycles borrow their BUCK leg fresh from the
///         basket's credit line for the cycle's length, so an empty wallet still arbitrages, the cycle
///         sells fresh BUCK into its first pool, and supply, lien and shares are unchanged after.
contract BuckCompoundFlashArbTest is BuckCompoundArbFixture {
    BasketMachineWheel internal machine;

    function _carryingBasket() internal pure override returns (bool) { return true; }

    function setUp() public override {
        super.setUp();
        machine = new BasketMachineWheel(address(buck), address(usdc), GOV, 0, 0);
        _configureArb(machine);
        vm.prank(GOV); wheel.setArb(ub, address(basket), 0, 100, 0);
        // An empty wallet: today's wallet-funded cycle has nothing to spend.
        uint256 cash = buck.balanceOf(address(basket));
        if (cash != 0) { vm.prank(address(basket)); buck.transfer(ALICE, cash); }
    }

    function _plan() private view returns (IBasketArb.ArbPlan memory) {
        return IBasketArb(address(basket)).basketArbPlan(0);
    }

    function _execute(IBasketArb.ArbPlan memory p) private returns (IBasketArb.ArbPlan memory) {
        vm.prank(address(wheel));
        return IBasketArb(address(basket)).executeBasketArb(0, p.direction, p.amount, 2);
    }

    function _ratio(address pool) private view returns (uint256) {
        (uint256 gross, uint256 net) = buck.accountBalancesScaled(pool);
        return Math.mulDiv(net, Q, gross);
    }

    function test_withoutTheFlashAnEmptyWalletFundsNoCycle() public {
        _trade(ub, address(buck), int256(100_000e6));
        assertEq(_plan().amount, 0);
    }

    function test_aFlashCycleRecoversAShockWithAnEmptyWallet() public {
        vm.prank(GOV); machine.setArbFlash(true);
        _trade(ub, address(buck), int256(100_000e6));
        IBasketArb.ArbPlan memory p = _plan();
        assertGt(p.amount, 0); assertGt(p.totalReturn, 0);
        uint256 supply = buck.totalIssued();
        uint256 principal = basket.lien();
        uint256 shares = basket.totalShares();
        uint256 room = buck.flashRoom(basket.creditId());
        uint256 cash = buck.netBalanceScaled(address(basket));
        IBasketArb.ArbPlan memory actual = _execute(p);
        assertGt(actual.totalReturn, 0);
        assertEq(actual.amount, p.amount);
        assertEq(actual.totalReturn, actual.cashReturn + actual.lpReturn);
        assertApproxEqAbs(actual.totalReturn, p.totalReturn, 12);
        assertEq(buck.totalIssued(), supply, "the flash is repaid: no standing supply");
        assertEq(basket.lien(), principal);
        assertEq(basket.totalShares(), shares);
        assertGe(buck.flashRoom(basket.creditId()), room, "the line's room is unchanged, or grows with the gain");
        assertGe(buck.netBalanceScaled(address(basket)), cash, "the wallet keeps what it had, and gains");
    }

    function test_aFlashCycleSellsFreshBuckIntoItsFirstPool() public {
        vm.prank(GOV); machine.setArbFlash(true);
        vm.warp(t0 + YEAR);                             // the basket's pool is a year hollow
        _trade(ub, address(buck), int256(100_000e6));
        IBasketArb.ArbPlan memory p = _plan();
        assertGt(p.amount, 0); assertEq(p.direction, 0, "BUCK into its own pool first");
        uint256 before = _ratio(own);
        assertLt(before, Q, "hollow before");
        _execute(p);
        assertGt(_ratio(own), before, "fresh BUCK lifted the pool's net ratio");
    }

    function test_aFlashCycleStaysWithinTheLinesRoom() public {
        vm.prank(GOV); machine.setArbFlash(true);
        _trade(ub, address(buck), int256(100_000e6));
        uint256 room = buck.flashRoom(basket.creditId());
        IBasketArb.ArbPlan memory p = _plan();
        assertLe(p.amount, room);
        // With the line nearly drawn, the cycle shrinks to fit (or none is planned).
        k.setK(Math.mulDiv(basket.lien(), 1e18, credit.currentValue(basket.creditId())) + 1e12);
        IBasketArb.ArbPlan memory small = _plan();
        assertLe(small.amount, buck.flashRoom(basket.creditId()));
        assertLt(small.amount, p.amount);
    }

    function test_onlyBuckInsideAFlashCanRunTheLeg() public {
        vm.prank(GOV); machine.setArbFlash(true);
        uint256 id = basket.creditId();
        bytes memory data = abi.encode(uint256(0), uint8(0), uint256(2));
        vm.expectRevert(BuckBasketStorage.NotWheel.selector);
        IBuckFlashBorrower(address(basket)).onBuckFlash(id, 1e6, data);
        // Buck itself, outside the basket's own arbitrage: refused too.
        vm.expectRevert(BuckBasketStorage.NotWheel.selector);
        vm.prank(address(buck));
        IBuckFlashBorrower(address(basket)).onBuckFlash(id, 1e6, data);
    }

    function test_aWheelTickRunsAFlashCycle() public {
        vm.prank(GOV); machine.setArbFlash(true);
        _trade(ub, address(buck), int256(100_000e6));
        (uint256 input,, uint256 profit) = wheel.plan(0);
        assertGt(input, 0); assertGt(profit, 0);
        uint256 supply = buck.totalIssued();
        uint256 reserve = buck.netBalanceOf(address(wheel));
        (uint256 work,) = wheel.tick(1, 0);
        assertEq(work, 1);
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.netBalanceOf(address(wheel)), reserve);
    }

    /// @notice tickAt (owner, 2026-11-20): a keeper runs the slot pending() names, passing nothing.
    function test_tickAtRunsTheSlotPendingNames() public {
        vm.prank(GOV); machine.setArbFlash(true);       // the emptied wallet funds no cycle otherwise
        _trade(ub, address(buck), int256(100_000e6));
        (bool due, uint256 steps) = machine.pending();
        assertTrue(due);
        uint256 n = machine.slotCount();
        uint256 slot = (machine.cursor() + steps) % n;
        uint256 supply = buck.totalIssued();
        (uint256 work,) = machine.tickAt(slot);
        assertEq(work, 1);
        assertEq(machine.cursor(), (slot + 1) % n, "the cursor moves past it");
        assertEq(buck.totalIssued(), supply);
    }

    function test_tickAtOnASlotWithoutWorkDoesNothing() public {
        (bool due,) = machine.pending();
        assertFalse(due, "a quiet market: nothing to do");
        uint256 n = machine.slotCount();
        for (uint256 slot; slot < n; ++slot) {
            (uint256 work, uint256 pay) = machine.tickAt(slot);
            assertEq(work, 0); assertEq(pay, 0);
        }
        vm.expectRevert(BasketMachineWheel.NoSuchSlot.selector);
        machine.tickAt(n);
    }

    function test_theFacetsAndLibraryStillFit() public {
        BasketUpkeepFacet facet = new BasketUpkeepFacet(address(0), address(0), 0, 0);
        assertLe(address(facet).code.length, 24_576);
        assertLe(address(basket).code.length, 24_576);
        assertLe(address(BasketMarketOps).code.length, 24_576);
        assertLe(address(buck).code.length, 24_576);
        assertLe(address(machine).code.length, 24_576);
    }
}
