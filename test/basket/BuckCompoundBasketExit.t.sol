// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeEquityFixture} from "./JubileeEquityIntegration.t.sol";
import {JubileeFund} from "../../src/JubileeFund.sol";
import {CompoundBasketHarness} from "./BuckCompoundBasket.t.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {BuckBasketStorage} from "../../src/basket/BuckBasketStorage.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {EqToken, IPool} from "./BuckBasketEquity.t.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

/// @dev A receipt exit pays assets; it does not silently retire NFT principal.
///      Even ample wallet cash or a marked claim cannot waive actual K backing.
contract BuckCompoundBasketExitTest is JubileeEquityFixture {
    CompoundBasketHarness b;

    function _makeBasket(address factory) internal override returns (BuckBasketEquity) {
        return new CompoundBasketHarness(address(buck), address(k), factory, GOV);
    }

    function _cashDebt() private returns (uint256 id) {
        b = CompoundBasketHarness(address(basket));
        _credit(ALICE, 1000e6);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        id = basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        vm.startPrank(GOV);
        b.openAccountingCredit(address(credit), 1e12);
        b.issueForAccounting(40e6);
        vm.stopPrank();
    }

    function test_lowKRefusesCashExitDespiteCashExceedingReceiptValue() public {
        uint256 id = _cashDebt();
        k.setK(0.1e18);
        assertGt(basket.liquidity(), basket.valueOf(id));
        assertLt(basket.headroom(), 0);
        uint256 beforeCash = buck.balanceOf(address(basket));
        uint256 supply = buck.totalIssued();
        vm.expectRevert(BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        vm.prank(ALICE); basket.redeem(id, 10_000);
        assertEq(buck.balanceOf(address(basket)), beforeCash);
        assertEq(basket.lien(), 40e6);
        assertEq(buck.totalIssued(), supply);
        assertEq(basket.receipt().ownerOf(id), ALICE);
    }

    function test_cashExitRefusesToCreateAnActualCollateralShortfall() public {
        uint256 id = _cashDebt();
        assertGt(basket.headroom(), 0);
        uint256 shares = basket.totalShares();
        vm.expectRevert(BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        vm.prank(ALICE); basket.redeemShares(id, shares * 3 / 4, 0);
        assertEq(basket.totalShares(), shares);
        assertEq(buck.balanceOf(address(basket)), 140e6);
        assertEq(basket.markNow(), 100e6);
    }

    function test_receivingCashDoesNotRepairLowKUntilPrincipalIsActuallyRetired() public {
        uint256 id = _cashDebt();
        k.setK(0.1e18);
        vm.prank(ALICE); buck.transfer(address(basket), 40e6);
        assertEq(basket.lien(), 40e6);
        vm.expectRevert(BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        vm.prank(ALICE); basket.redeem(id, 10_000);
        vm.prank(GOV);
        basket.retireCredit(40e6, 40e6, 0);
        assertEq(basket.lien(), 0);
        assertEq(buck.totalIssued(), 1000e6);
        vm.prank(ALICE);
        (uint256 remainingId, uint256 paid,, uint256 remaining) =
            basket.redeemShares(id, 100e6, 1);
        assertEq(remainingId, 0);
        assertEq(remaining, 0);
        assertGt(paid, 0);
        assertGt(basket.treasuryShares(), 0);
    }

    function test_partialCashExitPreservesPrincipalAgeAndIncumbentPriceThenRepaymentAllowsClose() public {
        uint256 id = _cashDebt();
        vm.warp(t0 + YEAR);
        uint256 supply = buck.totalIssued();
        uint256 price = basket.sharePrice();
        uint256 age = buck.claimSeconds();
        uint256 beforeCash = buck.netBalanceScaled(ALICE);
        uint256 shares = basket.totalShares();
        vm.prank(ALICE);
        (uint256 remainingId, uint256 paid, uint256 retired, uint256 remaining) =
            basket.redeemShares(id, shares / 2, 1);
        assertEq(remainingId, id);
        assertEq(retired, shares / 2);
        assertEq(remaining, shares - retired);
        assertEq(buck.netBalanceScaled(ALICE) - beforeCash, paid * Q);
        assertApproxEqAbs(basket.sharePrice(), price, 3e10);
        assertEq(basket.lien(), 40e6);
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.claimSeconds(), age);
        assertEq(basket.treasuryShares(), 0); // No gain fee on a demurrage loss.
        assertEq(basket.markNow(), buck.netBalanceOf(address(basket)) - 40e6);
        assertGe(basket.headroom(), 0);

        vm.expectRevert(BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        vm.prank(ALICE); basket.redeemShares(id, remaining, 0);
        vm.prank(GOV);
        basket.retireCredit(40e6, 40e6, 0);
        vm.prank(ALICE);
        (remainingId, paid, retired, remaining) = basket.redeemShares(id, remaining, 1);
        assertEq(remainingId, 0);
        assertGt(paid, 0);
        assertGt(retired, 0);
        assertEq(remaining, 0);
        assertEq(basket.totalShares(), 0);
        assertEq(basket.lien(), 0);
        assertEq(buck.totalIssued(), supply - 40e6);
    }

    function test_unpaidClaimPricesTheReceiptButCannotBackItsExit() public {
        uint256 id = _cashDebt();
        vm.warp(t0 + YEAR);
        uint256 claim = basket.reliefAccrued();
        assertGt(claim, 0);
        uint256 e = basket.equity();
        uint256 S = basket.totalShares();
        // Leave marked equity above principal, but bare collateral below it.
        uint256 stay = Math.ceilDiv((40e6 + claim / 2) * S, e);
        uint256 out = S - stay;
        uint256 pay = e * out / S;
        assertGt(e - pay, 40e6);
        assertLt(e - pay - claim, 40e6);
        vm.expectRevert(BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        vm.prank(ALICE); basket.redeemShares(id, out, 0);
        assertEq(basket.totalShares(), S);
    }

    function test_treasuryCannotBypassTheLowKExitGuard() public {
        uint256 id = _cashDebt();
        vm.prank(ALICE); buck.transfer(address(basket), 100e6);
        vm.prank(ALICE); basket.redeem(id, 5000);
        uint256 treasury = basket.treasuryShares();
        assertGt(treasury, 0);
        k.setK(0.1e18);
        vm.expectRevert(BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        vm.prank(GOV); basket.redeemTreasury(10_000, ALICE);
        assertEq(basket.treasuryShares(), treasury);
        assertEq(basket.lien(), 40e6);
    }

    function test_cashMinimumRevertsAllReceiptCashAndMarkChanges() public {
        uint256 id = _cashDebt();
        uint256 S = basket.totalShares();
        vm.expectRevert(BuckBasketEquityStorage.MinOut.selector);
        vm.prank(ALICE); basket.redeemShares(id, S / 2, type(uint256).max);
        assertEq(basket.totalShares(), S);
        assertEq(buck.balanceOf(address(basket)), 140e6);
        assertEq(basket.markNow(), 100e6);
        assertEq(basket.treasuryShares(), 0);
    }

    function testFuzz_cashExitMatchesActualPostExitLimit(uint64 kRaw, uint64 sharesRaw) public {
        uint256 id = _cashDebt();
        uint256 nextK = bound(kRaw, 1, 1e18);
        uint256 out = bound(sharesRaw, 1, 99_999_999);
        k.setK(nextK);
        bool allowed = Math.mulDiv(100e6 - out, nextK, 1e18) >= 40e6;
        if (!allowed) vm.expectRevert(BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        vm.prank(ALICE);
        basket.redeemShares(id, out, 0);
        assertEq(basket.lien(), 40e6);
        assertEq(buck.totalIssued(), 1040e6);
        assertEq(basket.totalShares(), allowed ? 100e6 - out : 100e6);
        assertEq(buck.balanceOf(address(basket)), allowed ? 140e6 - out : 140e6);
    }

    function _poolDebt() private returns (EqToken asset, uint256 id) {
        b = CompoundBasketHarness(address(basket));
        _bind(address(this), false, address(this));
        _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE); buck.transfer(address(this), 1_000_000e6);
        asset = new EqToken("Asset", "AST", 18);
        asset.mint(address(this), 2_000_000e18);
        asset.mint(ALICE, 10_000e18);
        vm.prank(GOV);
        address pool = basket.addBasketToken(address(asset), 18, 1e6, 10_000, 3000);
        _bind(pool, true, address(this));
        (,,,,,, int24 lo, int24 hi,,,) = basket.constituents(0);
        IPool(pool).mint(address(this), lo, hi, 1e17, "");
        vm.warp(t0 + 601);
        k.setK(0.75e18);
        vm.startPrank(ALICE);
        asset.approve(address(basket), type(uint256).max);
        id = basket.deposit(address(asset), 1000e18, 0);
        vm.stopPrank();
        vm.startPrank(GOV);
        basket.setNetMark(true);
        b.openAccountingCredit(address(credit), 1e18);
        vm.stopPrank();
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        assertGt(basket.liquidityOf(0), 0);
        assertGt(basket.lien(), 0);
    }

    function test_realPoolExitRetainsDebtShareAndLpProceedsRemainCash() public {
        (EqToken asset, uint256 id) = _poolDebt();
        uint256 principal = basket.lien();
        uint256 supply = buck.totalIssued();
        uint256 price = basket.sharePrice();
        uint256 cash = buck.netBalanceScaled(address(basket));
        uint256 userTokens = asset.balanceOf(ALICE);
        uint128 liquidity = basket.liquidityOf(0);
        (uint128 shares,) = basket.holdings(id);
        assertGt(basket.valueOf(id) / 10, basket.liquidity());
        vm.prank(ALICE);
        (, uint256 paid, uint256 retired, uint256 remaining) = basket.redeemShares(id, shares / 10, 0);
        assertGt(retired, 0);
        assertGt(remaining, 0);
        assertGt(asset.balanceOf(ALICE), userTokens);
        assertLt(basket.liquidityOf(0), liquidity);
        assertGt(buck.netBalanceScaled(address(basket)) + paid * Q, cash);
        assertEq(basket.lien(), principal);
        assertEq(buck.totalIssued(), supply);
        assertApproxEqAbs(basket.sharePrice(), price, 3e10);
        assertGe(basket.headroom(), 0);
    }

    function test_lowKPostcheckRollsBackRealLpBurnAndTokenPayout() public {
        (EqToken asset, uint256 id) = _poolDebt();
        k.setK(0.000001e18);
        b.refreshMark();
        assertGe(basket.headroom(), 0); // Held LP BUCK still backs the opening debt.
        uint128 liquidity = basket.liquidityOf(0);
        uint256 principal = basket.lien();
        uint256 cash = buck.netBalanceScaled(address(basket));
        uint256 userTokens = asset.balanceOf(ALICE);
        uint256 mark = basket.markNow();
        uint256 S = basket.totalShares();
        assertGt(basket.valueOf(id) / 2, basket.liquidity());
        vm.expectRevert(BuckBasketEquityStorage.CreditShortfallIncreased.selector);
        vm.prank(ALICE); basket.redeemShares(id, S / 2, 0);
        assertEq(basket.liquidityOf(0), liquidity);
        assertEq(basket.totalShares(), S);
        assertEq(basket.lien(), principal);
        assertEq(buck.netBalanceScaled(address(basket)), cash);
        assertEq(asset.balanceOf(ALICE), userTokens);
        assertEq(basket.markNow(), mark);
    }

    function test_fullInKindReceiptAndTreasuryExitAfterExplicitDebtRetirement() public {
        (EqToken asset, uint256 id) = _poolDebt();
        uint256 principal = basket.lien();
        vm.prank(ALICE); buck.transfer(address(basket), principal);
        assertEq(basket.lien(), principal); // A transfer is not repayment.
        vm.prank(GOV);
        basket.retireCredit(principal, principal, 0);
        assertEq(basket.lien(), 0);
        uint256 userTokens = asset.balanceOf(ALICE);
        (uint128 shares,) = basket.holdings(id);
        vm.prank(ALICE);
        (uint256 remainingId,, uint256 retired, uint256 remaining) =
            basket.redeemShares(id, shares, 0);
        assertEq(remainingId, 0);
        assertEq(retired, shares);
        assertEq(remaining, 0);
        assertGt(asset.balanceOf(ALICE), userTokens);
        assertGt(basket.treasuryShares(), 0);
        vm.prank(GOV);
        basket.redeemTreasury(10_000, ALICE);
        assertEq(basket.totalShares(), 0);
        assertEq(basket.treasuryShares(), 0);
        assertEq(basket.liquidityOf(0), 0);
        assertEq(basket.lien(), 0);
        assertEq(buck.totalIssued(), 2_000_000e6);
    }

    function test_fundRedemptionUsesItsFinalReceiptForTheMutualClaimAndMark() public {
        _cashDebt();
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(19e6);
        vm.prank(ALICE); buck.transfer(address(basket), 50e6);
        (uint128 shares,) = basket.holdings(id);
        uint256 supply = buck.totalIssued();
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](0);
        vm.prank(GOV);
        assertGt(fund.redeemScheduled(shares / 2, 1, routes), 0);
        (uint128 remaining,) = basket.holdings(id);
        assertEq(remaining, shares - shares / 2);
        assertEq(fund.positionId(), id);
        assertGt(basket.treasuryShares(), 0);
        (uint256 marked,,) = fund.previewRelief(basket.creditId(), 40e6);
        assertEq(basket.reliefAccrued(), marked / Q);
        assertEq(basket.markNow(), buck.netBalanceOf(address(basket)) - 40e6);
        assertEq(basket.lien(), 40e6);
        assertEq(buck.totalIssued(), supply);
        assertGe(basket.headroom(), 0);
    }

    function test_realPoolFundExitConvertsTokensAndPreservesTheDebtorClaim() public {
        (EqToken asset,) = _poolDebt();
        vm.warp(t0 + YEAR + 601);
        (uint256 id,,) = _invest(100e6);
        vm.startPrank(GOV);
        // Ask for a wholly liquid reserve so a small real portfolio sale is
        // eligible without making the fund dominate this basket's ownership.
        fund.setReserveParameters(10_000, 100);
        b.buyFromCashForAccounting(0, basket.liquidity());
        vm.stopPrank();
        (uint128 shares,) = basket.holdings(id);
        assertGt(basket.valueOf(id) / 2, basket.liquidity());
        uint256 principal = basket.lien();
        uint256 supply = buck.totalIssued();
        (,,,,,,,, bool buckIs0,,) = basket.constituents(0);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](1);
        routes[0] = JubileeFund.SaleRoute({
            sqrtPriceLimitX96: buckIs0 ? MAX_SQRT - 1 : MIN_SQRT + 1,
            minimumNetBuck: 1
        });
        vm.prank(GOV);
        assertGt(fund.redeemScheduled(shares / 2, 1, routes), 0);
        assertEq(asset.balanceOf(address(fund)), 0);
        (uint128 remaining,) = basket.holdings(id);
        assertEq(remaining, shares - shares / 2);
        assertEq(fund.positionId(), id);
        (uint256 marked,,) = fund.previewRelief(basket.creditId(), principal);
        assertEq(basket.reliefAccrued(), marked / Q);
        assertEq(basket.lien(), principal);
        assertEq(buck.totalIssued(), supply);
        b.refreshMark();
        assertGe(basket.headroom(), 0);
    }

    function test_zeroEquityCannotDestroyAnUnpaidReceipt() public {
        uint256 id = _cashDebt();
        vm.startPrank(GOV);
        basket.retireCredit(40e6, 40e6, 0);
        b.sendCash(ALICE, 100e6);
        vm.stopPrank();
        assertEq(basket.equity(), 0);
        vm.expectRevert(BuckBasketStorage.Underwater.selector);
        vm.prank(ALICE); basket.redeem(id, 10_000);
        assertEq(basket.receipt().ownerOf(id), ALICE);
        assertEq(basket.totalShares(), 100e6);
    }
}
