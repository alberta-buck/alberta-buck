// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeEquityFixture} from "./JubileeEquityIntegration.t.sol";
import {CompoundBasketHarness} from "./BuckCompoundBasket.t.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {BuckBasketUniswapV3} from "../../src/basket/BuckBasketUniswapV3.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {BasketVenueFacet} from "../../src/basket/BasketVenueFacet.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {ControlledPairIntent} from "./BasketUpkeepFacet.t.sol";
import {EqToken, EqController, IPool} from "./BuckBasketEquity.t.sol";

contract RejectCompoundPosition is BuckBasketUniswapV3 {
    error RejectedPosition();
    function positionMint(uint256 i, uint256 tokenAmount, uint256 buckAmount)
        public override onlySelf returns (uint128, uint256, uint256)
    {
        super.positionMint(i, tokenAmount, buckAmount);
        revert RejectedPosition();
    }
}

contract CutKAfterCompoundPosition is BuckBasketUniswapV3 {
    function positionMint(uint256 i, uint256 tokenAmount, uint256 buckAmount)
        public override onlySelf returns (uint128 l, uint256 t, uint256 b)
    {
        (l, t, b) = super.positionMint(i, tokenAmount, buckAmount);
        EqController(address(controller)).setK(0);
    }
}

/// @dev Actual Buck, claim book, fund, credit NFT and V3 pools. Harness probes
///      isolate funding/rollback boundaries; BasketPublicLifecycleTest covers
///      the unmodified shell's public opening and complete V3 lifecycle.
contract BuckCompoundBasketFlowTest is JubileeEquityFixture {
    EqToken[3] assets;
    address[3] pools;
    CompoundBasketHarness b;

    function _makeBasket(address factory) internal override returns (BuckBasketEquity) {
        return new CompoundBasketHarness(address(buck), address(k), factory, GOV);
    }

    function _open(uint256 face) private {
        b = CompoundBasketHarness(address(basket));
        vm.startPrank(GOV);
        basket.setNetMark(true);
        b.openAccountingCredit(address(credit), face);
        vm.stopPrank();
    }

    function _pools() private {
        _bind(address(this), false, address(this));
        _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE);
        buck.transfer(address(this), 1_000_000e6);
        for (uint256 i; i < 3; ++i) {
            assets[i] = new EqToken("Asset", "AST", 18);
            assets[i].mint(address(this), 1_000_000e18);
            assets[i].mint(ALICE, 10_000e18);
            vm.prank(GOV);
            pools[i] = basket.addBasketToken(address(assets[i]), 18, 1e6, 10_000 / (i + 1), 3000);
            _bind(pools[i], true, address(this));
            (,,,,,, int24 lo, int24 hi,,,) = basket.constituents(i);
            IPool(pools[i]).mint(address(this), lo, hi, 1e17, "");
        }
        vm.warp(t0 + 601); // Actual V3 history, not the bootstrap spot fallback.
        k.setK(0.75e18);
        vm.startPrank(ALICE);
        for (uint256 i; i < 3; ++i) {
            assets[i].approve(address(basket), type(uint256).max);
            basket.deposit(address(assets[i]), 1000e18, 0);
        }
        vm.stopPrank();
    }

    function _machine() private {
        ControlledPairIntent intent = new ControlledPairIntent(address(basket));
        intent.setRate(0);
        vm.startPrank(GOV);
        basket.setVenue(address(new BasketVenueFacet()));
        basket.setEquityWheel(address(new BasketUpkeepFacet(address(intent), address(0), 0, 0)));
        vm.stopPrank();
    }

    function test_budgetUsesExistingCashThenIssuesOnlyItsShortfall() public {
        _credit(ALICE, 1000e6);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        _open(1e12);
        (uint256 cash, uint256 capacity) = b.availableForAccounting();
        assertEq(cash, 100e6);
        assertEq(capacity, 100e6);
        assertEq(buck.balanceOf(address(basket)), cash);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(140e6), 140e6);
        assertEq(basket.lien(), 40e6);
        assertEq(buck.balanceOf(address(basket)), 140e6);
        assertEq(basket.equity(), 100e6);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(120e6), 120e6);
        assertEq(basket.lien(), 40e6); // Existing cash does not cause a second draw.
    }

    function test_componentWheelIssuesForActualLpPlacementAndFundPurchase() public {
        _pools();
        _open(1e12);
        assertEq(buck.balanceOf(address(basket)), 0);
        uint256 supplyBefore = buck.totalIssued();
        for (uint256 i; i < 3; ++i) {
            assertEq(IBasketUpkeep(address(basket)).wheelStep(2, i), 1);
            assertGt(basket.liquidityOf(i), 0);
        }
        uint256 beforePrincipal = basket.lien();
        // The component wheel can sell part of waiting TOKEN to balance a
        // reserve-constrained pair. Its debt is the actual draw, not all TOKEN.
        assertGt(beforePrincipal, 2000e6);
        assertEq(buck.totalIssued() - supplyBefore, beforePrincipal);
        assertTrue(basket.creditLive());
        assertLt(buck.balanceOf(address(basket)), 1e6);
        assertEq(IBasketUpkeep(address(basket)).wheelStep(3, 0), 1);
        assertGt(basket.lien(), beforePrincipal);
        assertGt(basket.idleToken(0) + basket.idleToken(1) + basket.idleToken(2), 0);
    }

    function test_cashFundedPurchaseCannotLeaveActualActivatedBackingShort() public {
        _pools();
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        _open(1e12);
        vm.startPrank(GOV);
        b.fundForAccounting(200e6);
        b.buyFromCashForAccounting(0, 1e6);
        vm.stopPrank();
        assertEq(basket.lien(), 100e6);
        uint256 actualLimit = credit.currentValue(basket.creditId()) * k.currentBuckK() / 1e18;
        assertLe(basket.lien(), actualLimit);
    }

    function test_existingCashCanRestoreFreeBackingAfterRepaymentWithoutIssuing() public {
        _pools();
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        _open(1e12);
        vm.prank(GOV);
        b.fundForAccounting(200e6);
        basket.retireCredit(50e6, 50e6, 0);
        k.setK(0.02e18);
        b.refreshMark();
        assertLt(basket.headroom(), 0); // Only half the backing is still active.
        uint256 supply = buck.totalIssued();
        vm.prank(GOV);
        b.buyFromCashForAccounting(0, 1e6);
        assertEq(basket.lien(), 50e6);
        assertEq(buck.totalIssued(), supply);
        (uint256 face, uint256 active,) = credit.creditInfo(basket.creditId());
        assertEq(active, face);
        assertGt(basket.headroom(), 0);
    }

    function test_machinePairsDepositsUsingActualIssuanceAndDailyHasNoSignedCalls() public {
        _pools();
        _open(1e12);
        _machine();
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        for (uint256 i; i < 3; ++i) assertGt(basket.liquidityOf(i), 0);
        assertGt(basket.lien(), 2900e6);
        assertLt(buck.balanceOf(address(basket)), 1e6);
        uint256 principal = basket.lien();
        assertEq(IBasketUpkeep(address(basket)).wheelStep(0, 0), 1);
        assertEq(basket.lien(), principal); // Marked claims are not automatic paydown.
    }

    function test_faceCapStopsFurtherDrawsWithoutActivatingOrMintingExtraRoom() public {
        _pools();
        _open(600e6);
        _machine();
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        assertEq(basket.lien(), 450e6);
        (, uint256 active,) = credit.creditInfo(basket.creditId());
        assertEq(active, 600e6);
        (, uint256 room) = b.availableForAccounting();
        assertEq(room, 0);
        assertGt(basket.idleToken(1), 900e18);
    }

    function test_failedLpMintRollsBackIssuanceCoveragePoolTransfersAndClaims() public {
        _failedPlacement(address(new RejectCompoundPosition()), RejectCompoundPosition.RejectedPosition.selector);
    }

    function test_postTradeCreditFailureRollsBackTheDrawAndRealLpMint() public {
        _failedPlacement(address(new CutKAfterCompoundPosition()),
            BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        assertEq(k.currentBuckK(), 0.75e18);
    }

    function _failedPlacement(address rejectedVenue, bytes4 reason) private {
        _pools();
        _open(1e12);
        vm.prank(GOV);
        basket.setVenue(rejectedVenue);
        uint256 supply = buck.totalIssued();
        uint256 cash = buck.balanceOf(pools[0]);
        uint256 seconds_ = buck.claimSeconds();
        vm.expectRevert(reason);
        IBasketUpkeep(address(basket)).wheelStep(2, 0);
        assertEq(basket.lien(), 0);
        (, uint256 active,) = credit.creditInfo(basket.creditId());
        assertEq(active, 0);
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.claimSeconds(), seconds_);
        assertEq(buck.balanceOf(pools[0]), cash);
        assertEq(buck.balanceOf(address(basket)), 0);
        assertEq(basket.idleToken(0), 1000e18);
        assertEq(basket.liquidityOf(0), 0);
        assertFalse(basket.creditLive());
    }

    function test_zeroKCannotTurnCreditAvailabilityIntoWalletCash() public {
        _pools();
        _open(1e12);
        k.setK(0);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(1000e6), 0);
        assertEq(basket.lien(), 0);
        assertEq(buck.balanceOf(address(basket)), 0);
        assertFalse(basket.creditLive());
    }

    function test_fullyAgedFacilityHasNoNewCapacityDespitePositiveCashAndCollateral() public {
        _credit(ALICE, 10_000e6);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        _open(1e12);
        vm.prank(GOV);
        b.fundForAccounting(140e6);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(10);
        vm.prank(ALICE);
        buck.transfer(address(basket), 200e6);
        b.refreshMark();
        assertGt(basket.headroom(), 0);
        (uint256 cash, uint256 capacity) = b.availableForAccounting();
        assertGt(cash, 200e6);
        assertEq(capacity, 0);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(cash + 100e6), cash);
        assertEq(basket.lien(), 40e6);
    }

    function testFuzz_capacityAfterKChangeMatchesExecutableRootQuotes(
        uint64 capital_, uint64 face_, uint64 k_
    ) public {
        uint256 capital = bound(capital_, 1000, 10_000e6);
        uint256 face = bound(face_, 1, 2 * capital);
        _credit(ALICE, 2 * capital);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), capital, 0);
        vm.stopPrank();
        _open(face);
        vm.prank(GOV);
        b.fundForAccounting(capital + (face < capital ? face : capital) / 2);
        uint256 prior = basket.lien();
        k.setK(bound(k_, 0, 1e18));
        b.refreshMark();
        (uint256 cash, uint256 room) = b.availableForAccounting();
        uint256[] memory ids = new uint256[](1);
        ids[0] = basket.creditId();
        if (room != 0) {
            vm.prank(address(basket));
            buck.quoteMint(room, ids);
        }
        vm.expectRevert();
        vm.prank(address(basket));
        buck.quoteMint(room + 1, ids);
        vm.prank(GOV);
        assertEq(b.fundForAccounting(cash + room), cash + room);
        assertEq(basket.lien(), prior + room);
        assertEq(buck.balanceOf(address(basket)), cash + room);
        assertEq(basket.equity(), capital);
    }
}
