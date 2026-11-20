// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeWheelFixture} from "./JubileeWheel.t.sol";
import {CompoundBasketHarness} from "../basket/BuckCompoundBasket.t.sol";
import {JubileeKind} from "../../src/wheel/JubileeKind.sol";
import {EqToken, IPool} from "../basket/BuckBasketEquity.t.sol";

/// @dev Reuses the canonical fixture, without inheriting its standalone tests.
contract BasketRetirementWheelTest is JubileeWheelFixture {
    function _policy() internal pure override returns (JubileeKind.JubileePolicy memory p) {
        p = JubileeWheelFixture._policy();
        p.retirementMinimum = 1e6;
        p.retirementCap = 10e6;
    }

    function _cashDebt(uint256 principal) private returns (CompoundBasketHarness b) {
        return _cashDebt(principal, 10_000e6);
    }

    function _cashDebt(uint256 principal, uint256 seed) private returns (CompoundBasketHarness b) {
        _credit(ALICE, seed);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 1000e6, 0);
        vm.stopPrank();
        b = CompoundBasketHarness(address(basket));
        vm.startPrank(GOV);
        b.openAccountingCredit(address(credit), 1e15);
        b.issueForAccounting(principal);
        vm.stopPrank();
    }

    function _repay(bool older) private {
        _cashDebt(40e6);
        JubileeKind w = _attach(older);
        assertEq(w.slotCount(), 4);
        uint256 supply = buck.totalIssued();
        uint256 cash = buck.balanceOf(address(basket));
        (uint256 work,) = w.tick(4, 0);
        assertEq(work, 1); assertEq(basket.lien(), 30e6);
        assertEq(buck.totalIssued(), supply - 10e6);
        assertEq(buck.balanceOf(address(basket)), cash - 10e6);
        w.rearm(); (work,) = w.tick(4, 0);
        assertEq(work, 0); assertEq(basket.lien(), 30e6);
        for (uint256 i; i < 3; ++i) {
            vm.roll(vm.getBlockNumber() + 1); (work,) = w.tick(4, 0);
            assertEq(work, 1);
        }
        assertEq(basket.lien(), 0); assertFalse(basket.creditLive());
        assertEq(buck.totalIssued(), supply - 40e6);
        assertEq(credit.ownerOf(basket.creditId()), address(basket));
    }

    function test_machineWheelActuallyRetiresPrincipalAndClosesItsActivatedLiability() public { _repay(false); }
    function test_olderWheelUsesTheSameBoundedPrincipalRetirement() public { _repay(true); }

    function test_partialDustDoesNotRunButACompleteDustTailClearsWithoutReward() public {
        CompoundBasketHarness b = _cashDebt(40e6);
        JubileeKind w = _attach(false);
        uint256 out = buck.balanceOf(address(basket)) - 3;
        vm.prank(GOV); b.sendCash(BOB, out);
        (uint256 work,) = w.tick(4, 0);
        assertEq(work, 0); assertEq(basket.lien(), 40e6);
        vm.prank(ALICE); buck.transfer(address(basket), 40e6);
        vm.prank(GOV); basket.retireCredit(40e6 - 3, 40e6 - 3, 0);
        assertEq(basket.lien(), 3);
        vm.roll(vm.getBlockNumber() + 1);
        (work,) = w.tick(4, 0);
        assertEq(work, 0, "three raw units cannot earn a work reward");
        assertEq(basket.lien(), 0); assertFalse(basket.creditLive());
    }

    function test_sharedBudgetDownsizesPrincipalAndNextBlockCanContinue() public {
        _cashDebt(900e6, 1000e6);
        JubileeKind w = _attach(false);
        JubileeKind.JubileePolicy memory p = _policy(); p.retirementCap = 500e6;
        vm.prank(GOV); w.setJubilee(fund, p);
        vm.warp(t0 + YEAR);
        uint256 available = buck.availableReliefScaled();
        uint256 supply = buck.totalIssued();
        uint256 beforePrincipal = basket.lien();
        (uint256 work,) = w.tick(4, 0);
        assertEq(work, 1);
        uint256 retired = beforePrincipal - basket.lien();
        assertGt(retired, 1e6); assertLt(retired, p.retirementCap);
        assertEq(buck.totalIssued(), supply - retired);
        assertLt(buck.availableReliefScaled(), available);
        uint256 remaining = basket.lien();
        w.rearm(); (work,) = w.tick(4, 0);
        assertEq(work, 0); assertEq(basket.lien(), remaining);
        vm.roll(vm.getBlockNumber() + 1); (work,) = w.tick(4, 0);
        assertEq(work, 1); assertLt(basket.lien(), remaining);
    }

    function test_anExhaustedBudgetDoesNotAuthorizeOrPretendToRepayBasketDebt() public {
        CompoundBasketHarness b = _cashDebt(900e6, 1000e6);
        JubileeKind w = _attach(false);
        vm.prank(GOV); b.sendCash(ALICE, 500e6);
        vm.warp(t0 + YEAR);
        uint256 aliceId = credit.tokenOfOwnerByIndex(ALICE, 0);
        _authorize(ALICE, aliceId, 190e6, 0);
        vm.prank(ALICE); fund.reduce(aliceId, 190e6);
        assertEq(buck.availableReliefScaled(), 0);
        uint256 cash = buck.netBalanceScaled(address(basket));
        uint256 supply = buck.totalIssued();
        (uint256 work,) = w.tick(4, 0);
        assertEq(work, 0); assertEq(basket.lien(), 900e6);
        assertEq(buck.netBalanceScaled(address(basket)), cash); assertEq(buck.totalIssued(), supply);
        (uint256 authorized,,) = buck.reductionAuthorization(basket.creditId());
        assertEq(authorized, 0);
        vm.roll(vm.getBlockNumber() + 1); (work,) = w.tick(4, 0);
        assertEq(work, 1); assertLt(basket.lien(), 900e6);
    }

    function test_mixedAgeRoundingUsesFullFacilityReliefToFindAnAffordableBatch() public {
        CompoundBasketHarness b = _cashDebt(5, 1000e6);
        vm.warp(t0 + 1);
        vm.startPrank(GOV);
        b.issueForAccounting(4); // Nine raw units, five raw BUCK-seconds.
        b.sendCash(ALICE, 900e6);
        vm.stopPrank();
        vm.prank(ALICE); buck.transfer(address(fund), 900e6);
        // Leave less than nine raw units liquid, the rest in the actual
        // mutual receipt. This makes single-BUCK-second rounding material.
        _invest(899_999_992);
        JubileeKind w = _attach(false);
        JubileeKind.JubileePolicy memory p = _policy();
        p.retirementMinimum = 1; p.retirementCap = 5;
        vm.prank(GOV); w.setJubilee(fund, p);
        w.refreshValuation();
        uint256 available = buck.availableReliefScaled();
        (uint256 partialRelief,, bool limited) = fund.previewRelief(basket.creditId(), 5);
        assertTrue(limited);
        uint256 naive = 5 * available / partialRelief;
        assertEq(naive, 2);
        (uint256 naiveRelief,,) = fund.previewRelief(basket.creditId(), naive);
        assertGt(naiveRelief, available, "partial-quote inversion would retry an unaffordable amount");
        (uint128 held,) = basket.holdings(fund.positionId());
        uint256 supply = buck.totalIssued();
        (uint256 work,) = w.tick(4, 0);
        assertEq(work, 1); assertEq(basket.lien(), 8);
        assertEq(buck.totalIssued(), supply - 1);
        assertEq(buck.availableReliefScaled(), available, "zero selected age consumes no relief budget");
        (uint128 left,) = basket.holdings(fund.positionId()); assertEq(left, held);
    }

    function test_agedRetirementRefreshesTheRealMutualFundReceiptWithoutSellingIt() public {
        _cashDebt(40e6);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(100e6);
        JubileeKind w = _attach(false);
        (uint128 shares,) = basket.holdings(id);
        uint256 cash = buck.netBalanceScaled(address(basket));
        (uint256 work,) = w.tick(4, 0);
        assertEq(work, 1); assertEq(basket.lien(), 30e6);
        assertTrue(w.valuationReady());
        (uint128 left,) = basket.holdings(id);
        assertEq(left, shares); assertEq(fund.positionId(), id);
        assertLt(cash - buck.netBalanceScaled(address(basket)), 10e6 * Q);
        assertGe(buck.balanceOf(address(basket)), basket.liquidityTarget());
    }

    function test_maturedFacilityIsNotClosedOrRepaidByTheWheel() public {
        _cashDebt(40e6);
        JubileeKind w = _attach(false);
        vm.warp(t0 + 50 * YEAR);
        w.tick(4, 0);
        vm.roll(vm.getBlockNumber() + 1); w.tick(4, 0);
        assertFalse(buck.claimBook().pending());
        assertEq(basket.lien(), 40e6);
        assertEq(credit.ownerOf(basket.creditId()), address(basket));
        assertEq(buck.claimBook().deferredPrincipal(), 0);
        assertFalse(w.jubileeDue(3));
    }

    function test_retirementSpendsOnlyTheCashAboveTheExistingLiquidityTargetEvenAtLowK() public {
        _bind(address(this), false, address(this));
        _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE); buck.transfer(address(this), 1_500_000e6);
        EqToken asset = new EqToken("Asset", "AST", 18);
        asset.mint(address(this), 2_000_000e18); asset.mint(ALICE, 100e18);
        vm.prank(GOV);
        address pool = basket.addBasketToken(address(asset), 18, 1e6, 10_000, 3000);
        _bind(pool, true, address(this));
        (,,,,,, int24 lower, int24 upper,,,) = basket.constituents(0);
        IPool(pool).mint(address(this), lower, upper, 1e18, "");
        vm.warp(t0 + 601);
        vm.startPrank(ALICE);
        asset.approve(address(basket), type(uint256).max);
        basket.deposit(address(asset), 100e18, 0);
        vm.stopPrank();
        CompoundBasketHarness b = CompoundBasketHarness(address(basket));
        vm.startPrank(GOV);
        b.openAccountingCredit(address(credit), 1e15); b.issueForAccounting(40e6);
        b.sendCash(BOB, 38_500_000);
        vm.stopPrank();
        JubileeKind w = _attach(false);
        k.setK(0.1e18);
        assertLt(basket.headroom(), 0);
        uint256 target = basket.liquidityTarget();
        assertGt(target, 900_000); assertLe(target, 1e6);
        // The 0.5 BUCK excess is below the configured one-BUCK work threshold.
        (uint256 work,) = w.tick(4, 0);
        assertEq(work, 0); assertEq(basket.lien(), 40e6);
        JubileeKind.JubileePolicy memory p = _policy(); p.retirementMinimum = 100_000;
        vm.prank(GOV); w.setJubilee(fund, p);
        (work,) = w.tick(4, 0);
        assertEq(work, 1); assertEq(basket.lien(), 40e6 - (1_500_000 - target));
        assertEq(buck.balanceOf(address(basket)), target);
        assertLt(basket.headroom(), 0, "only the actual repayment repairs backing");
        assertEq(basket.liquidityOf(0), 0, "no forced portfolio operation");
    }
}

/// @notice The canonical basket is Carrying: the retirement job burns its idle BUCK above its liquidity
///         target too (owner, 2026-11-20: "burn BUCKs instead of leave them standing idle"), its cash
///         read net, as its target is.
contract CarryingBasketRetirementWheelTest is JubileeWheelFixture {
    function _carryingBasket() internal pure override returns (bool) { return true; }

    function _policy() internal pure override returns (JubileeKind.JubileePolicy memory p) {
        p = JubileeWheelFixture._policy();
        p.retirementMinimum = 1e6;
        p.retirementCap = 10e6;
    }

    function _cashDebt(uint256 principal) private returns (CompoundBasketHarness b) {
        _credit(ALICE, 10_000e6);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 1000e6, 0);
        vm.stopPrank();
        b = CompoundBasketHarness(address(basket));
        vm.startPrank(GOV);
        b.openAccountingCredit(address(credit), 1e15);
        b.issueForAccounting(principal);
        vm.stopPrank();
    }

    function test_aCarryingBasketBurnsItsIdleBuckAboveItsTarget() public {
        assertTrue(identity.isCarrying(address(basket)));
        _cashDebt(40e6);
        JubileeKind w = _attach(false);
        uint256 supply = buck.totalIssued();
        uint256 cash = buck.netBalanceOf(address(basket));
        (uint256 work,) = w.tick(4, 0);
        assertEq(work, 1);
        assertEq(basket.lien(), 30e6);
        assertEq(buck.totalIssued(), supply - 10e6);
        assertEq(buck.netBalanceOf(address(basket)), cash - 10e6);
        for (uint256 i; i < 3; ++i) {
            vm.roll(vm.getBlockNumber() + 1); (work,) = w.tick(4, 0);
            assertEq(work, 1);
        }
        assertEq(basket.lien(), 0);
        assertGe(buck.netBalanceOf(address(basket)), basket.liquidityTarget());
    }

    function test_anAgedCarryingBasketSpendsOnlyItsNetCashAboveTheTarget() public {
        _cashDebt(40e6);
        vm.warp(block.timestamp + 365 days);            // its BUCK now carry a year's fee
        (uint256 gross, uint256 net) = buck.accountBalancesScaled(address(basket));
        assertGt(gross, net, "hollow: nominal above net");
        JubileeKind w = _attach(false);
        uint256 lien = basket.lien();
        (uint256 work,) = w.tick(4, 0);
        assertEq(work, 1);
        assertLt(basket.lien(), lien, "it retired principal");
        assertGe(buck.netBalanceOf(address(basket)), basket.liquidityTarget(), "never below the target, net");
    }
}
