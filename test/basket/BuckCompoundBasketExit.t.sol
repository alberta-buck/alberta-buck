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
import {Buck} from "../../src/Buck.sol";

/// @dev Exits always proceed (owner, 2026-11-14). A receipt is paid in BUCK by default: from the
///      basket's cash when it suffices, else its fraction of every position is withdrawn and its
///      TOKEN sold through each constituent's pool, the leaver bearing the slippage; in kind only on
///      request. The one gate is the lien: an exit retires as much of the basket's principal as
///      keeps its K shortfall from growing (none while it has room), and reverts only when the
///      leaver's assets cannot cover that or its own minimum. Unpaid relief is equity, not collateral.
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

    /// The basket's K shortfall, as the contract reckons it: lien less K x its marked collateral.
    function _shortfall() private view returns (uint256) {
        int256 room = basket.headroom();
        return room < 0 ? uint256(-room) : 0;
    }

    function test_underWaterCashExitProceedsAndRetiresWhatHoldsTheShortfall() public {
        uint256 id = _cashDebt();
        k.setK(0.1e18);
        b.refreshMark();
        assertEq(_shortfall(), 30e6);                     // 40 owed against 0.1 x 100 of collateral
        uint256 supply = buck.totalIssued();
        uint256 price = basket.sharePrice();
        vm.prank(ALICE);
        (uint256 remainingId, uint256 paid, uint256 retired, uint256 remaining) =
            basket.redeemShares(id, 50e6, 1);
        assertEq(remainingId, id);
        assertEq(paid, 50e6);
        assertEq(retired, 50e6);
        assertEq(remaining, 50e6);
        // Paying 50 leaves 50 of collateral (limit 5): the shortfall would be 35, so 5 is retired.
        assertEq(basket.lien(), 35e6);
        assertEq(buck.totalIssued(), supply - 5e6);
        assertEq(_shortfall(), 30e6);
        assertEq(basket.sharePrice(), price);
    }

    function test_exitThatWouldCreateAShortfallRetiresTheExcessInstead() public {
        uint256 id = _cashDebt();
        assertEq(_shortfall(), 0);
        uint256 shares = basket.totalShares();
        vm.prank(ALICE);
        (, uint256 paid,, uint256 remaining) = basket.redeemShares(id, shares * 3 / 4, 1);
        assertEq(paid, 75e6);
        assertEq(remaining, shares / 4);
        // 25 of equity left can carry 25 at K = 1: 15 of the 40 is retired, none beyond.
        assertEq(basket.lien(), 25e6);
        assertEq(basket.headroom(), 0);
        assertEq(buck.totalIssued(), 1040e6 - 15e6);
        assertEq(buck.balanceOf(address(basket)), 140e6 - 75e6 - 15e6);
    }

    function test_partialCashExitPreservesPrincipalAgeAndIncumbentPriceThenTheRestRetiresTheLien() public {
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
        assertEq(basket.lien(), 40e6);                    // room remained: nothing retired
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.claimSeconds(), age);
        assertEq(basket.treasuryShares(), 0); // No gain fee on a demurrage loss.
        assertGe(basket.headroom(), 0);

        // The rest leaves too: with nothing left to carry it, the whole lien is retired on the way.
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

    function test_unpaidClaimPricesTheReceiptButIsNotCollateralForItsExit() public {
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
        vm.prank(ALICE); basket.redeemShares(id, out, 1);
        assertEq(basket.totalShares(), stay);
        // Only bare collateral (equity less the unpaid claim) carries the lien: the exit retired the
        // difference rather than counting the claim.
        assertLt(basket.lien(), 40e6);
        assertEq(_shortfall(), 0);
    }

    function test_treasuryExitsUnderWaterLikeAnyHolder() public {
        uint256 id = _cashDebt();
        vm.prank(ALICE); buck.transfer(address(basket), 100e6);
        vm.prank(ALICE); basket.redeem(id, 5000);
        uint256 treasury = basket.treasuryShares();
        assertGt(treasury, 0);
        k.setK(0.1e18);
        b.refreshMark();
        uint256 short = _shortfall();
        assertGt(short, 0);
        vm.prank(GOV); basket.redeemTreasury(10_000, ALICE);
        assertEq(basket.treasuryShares(), 0);
        assertLe(_shortfall(), short);
        assertLt(basket.lien(), 40e6);
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

    function testFuzz_cashExitRetiresExactlyWhatHoldsTheShortfall(uint64 kRaw, uint64 sharesRaw) public {
        uint256 id = _cashDebt();
        uint256 nextK = bound(kRaw, 1, 1e18);
        uint256 out = bound(sharesRaw, 1, 99_999_999);
        k.setK(nextK);
        uint256 limitBefore = Math.mulDiv(100e6, nextK, 1e18);
        uint256 before = limitBefore >= 40e6 ? 0 : 40e6 - limitBefore;
        uint256 limitAfter = Math.mulDiv(100e6 - out, nextK, 1e18);
        uint256 afterPay = limitAfter >= 40e6 ? 0 : 40e6 - limitAfter;
        uint256 retire = afterPay > before ? afterPay - before : 0;
        vm.prank(ALICE);
        (, uint256 paid,,) = basket.redeemShares(id, out, 0);
        assertEq(paid, out);
        assertEq(basket.lien(), 40e6 - retire);
        assertEq(buck.totalIssued(), 1040e6 - retire);
        assertEq(basket.totalShares(), 100e6 - out);
        assertEq(buck.balanceOf(address(basket)), 140e6 - out - retire);
        assertLe(_shortfall(), before);
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

    function test_realPoolExitPaysBuckBySellingItsTokenAndKeepsTheDebtShare() public {
        (EqToken asset, uint256 id) = _poolDebt();
        uint256 principal = basket.lien();
        uint256 supply = buck.totalIssued();
        uint256 price = basket.sharePrice();
        uint256 userTokens = asset.balanceOf(ALICE);
        uint256 userCash = buck.netBalanceScaled(ALICE);
        uint128 liquidity = basket.liquidityOf(0);
        (uint128 shares,) = basket.holdings(id);
        assertGt(basket.valueOf(id) / 10, basket.liquidity());
        vm.prank(ALICE);
        (, uint256 paid, uint256 retired, uint256 remaining) = basket.redeemShares(id, shares / 10, 1);
        assertGt(paid, 0);
        assertGt(retired, 0);
        assertGt(remaining, 0);
        assertEq(asset.balanceOf(ALICE), userTokens, "BUCK by default: no TOKEN delivered");
        assertEq(buck.netBalanceScaled(ALICE) - userCash, paid * Q);
        assertLt(basket.liquidityOf(0), liquidity);
        assertEq(basket.lien(), principal);               // room remained: the debt share stays as cash
        assertEq(buck.totalIssued(), supply);
        // The leaver bore the sale's slippage; the holders who stay lost nothing at TWAP marks.
        assertGe(basket.sharePrice() + 3e10, price);
        assertGe(basket.headroom(), 0);
    }

    function test_realPoolExitInKindOnRequestDeliversTokenAndKeepsTheDebtShare() public {
        (EqToken asset, uint256 id) = _poolDebt();
        uint256 principal = basket.lien();
        uint256 supply = buck.totalIssued();
        uint256 price = basket.sharePrice();
        uint256 cash = buck.netBalanceScaled(address(basket));
        uint256 userTokens = asset.balanceOf(ALICE);
        uint128 liquidity = basket.liquidityOf(0);
        (uint128 shares,) = basket.holdings(id);
        vm.prank(ALICE);
        (, uint256 paid, uint256 retired, uint256 remaining) = basket.redeemInKind(id, shares / 10);
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

    function test_conversionMinimumRevertsTheBurnTheSaleAndTheBooks() public {
        (EqToken asset, uint256 id) = _poolDebt();
        uint128 liquidity = basket.liquidityOf(0);
        uint256 S = basket.totalShares();
        uint256 principal = basket.lien();
        uint256 cash = buck.netBalanceScaled(address(basket));
        uint256 userTokens = asset.balanceOf(ALICE);
        (uint128 shares,) = basket.holdings(id);
        vm.expectRevert(BuckBasketEquityStorage.MinOut.selector);
        vm.prank(ALICE); basket.redeemShares(id, shares / 10, type(uint256).max);
        assertEq(basket.liquidityOf(0), liquidity);
        assertEq(basket.totalShares(), S);
        assertEq(basket.lien(), principal);
        assertEq(buck.netBalanceScaled(address(basket)), cash);
        assertEq(asset.balanceOf(ALICE), userTokens);
    }

    function test_portfolioExitDrawsCreditAndKeepsItsLiquidity() public {
        (, uint256 id) = _poolDebt();
        vm.prank(GOV);
        basket.setPortfolioLimits(true);
        uint128 liquidity = basket.liquidityOf(0);
        uint256 debt = basket.lien();
        uint256 balance = buck.netBalanceOf(ALICE);
        (uint128 shares,) = basket.holdings(id);
        vm.prank(ALICE);
        (, uint256 paid,,) = basket.redeemShares(id, shares / 10, 1);
        assertGt(basket.lien(), debt);
        assertEq(buck.netBalanceOf(ALICE) - balance, paid);
        assertEq(basket.liquidityOf(0), liquidity);
        assertGe(basket.headroom(), 0);
    }

    function test_portfolioExitCrossingCreditLimitAlsoSells() public {
        (, uint256 id) = _poolDebt();
        vm.prank(GOV);
        basket.setPortfolioLimits(true);
        k.setK(0.1e18);
        uint128 liquidity = basket.liquidityOf(0);
        uint256 debt = basket.lien();
        (uint128 shares,) = basket.holdings(id);
        vm.prank(ALICE);
        (, uint256 paid,,) = basket.redeemShares(id, shares / 3, 1);
        assertGt(paid, 0);
        assertGt(basket.lien(), debt);
        assertLt(basket.liquidityOf(0), liquidity);
        assertGe(basket.headroom(), 0);
    }

    function test_lowKPoolExitProceedsAndHoldsTheShortfall() public {
        _lowKExit(0.05e18);
    }

    /// @dev KNOWN ISSUE (2026-11-20, for the owner): the machine pairs every TOKEN, so its lien is the BUCK
    ///      in its positions. As K nears zero an exit must retire the leaving share of that BUCK as
    ///      _creditValueS MARKS it (half the position's value at the low mark, here 502.41 BUCK), but it
    ///      recovers the BUCK the burn actually releases (499.99) and has paid the leaver first: the
    ///      retirement is short and the exit reverts. Under the earlier components facet the basket held
    ///      other cash, which hid it. Pinned until the owner decides the fix.
    function test_nearZeroKExitOfAFullyPairedBasketIsShortByTheMarkedHeldBuck() public {
        (, uint256 id) = _poolDebt();
        k.setK(0.000001e18);
        b.refreshMark();
        uint256 S = basket.totalShares();
        vm.expectRevert(Buck.InsufficientSpendableBalance.selector);
        vm.prank(ALICE);
        basket.redeemShares(id, S / 2, 1);
    }

    function _lowKExit(uint256 lowK) private {
        (EqToken asset, uint256 id) = _poolDebt();
        k.setK(lowK);
        b.refreshMark();
        uint256 short = _shortfall();
        uint256 principal = basket.lien();
        uint256 userTokens = asset.balanceOf(ALICE);
        uint256 S = basket.totalShares();
        vm.prank(ALICE);
        (, uint256 paid,,) = basket.redeemShares(id, S / 2, 1);
        assertGt(paid, 0);
        assertEq(asset.balanceOf(ALICE), userTokens);
        // What cash cannot pay of the residue stays with the leaver as shares (_exitInKind's `left`).
        assertGe(basket.totalShares(), S - S / 2);
        assertLe(basket.totalShares(), S - S / 2 + S / 1000);
        assertLe(_shortfall(), short);
        assertLe(basket.lien(), principal);
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
        (uint256 remainingId,, uint256 retired, uint256 remaining) = basket.redeemInKind(id, shares);
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
