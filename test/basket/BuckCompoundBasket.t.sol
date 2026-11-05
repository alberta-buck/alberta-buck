// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeEquityFixture} from "./JubileeEquityIntegration.t.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {BasketJubileeMath} from "../../src/basket/BasketJubileeMath.sol";
import {Buck} from "../../src/Buck.sol";
import {JubileeFund} from "../../src/JubileeFund.sol";

/// @dev Test-only access to a real facility while the production opening gate
///      remains shut pending explicit wheel spend/repayment/exit integration.
contract CompoundBasketHarness is BuckBasketEquity {
    constructor(address buck_, address k_, address factory_, address gov_)
        BuckBasketEquity(buck_, k_, factory_, gov_, 3000, 600, 64, 500, 1e3) {}

    function openAccountingCredit(address credit_, uint256 face) external onlyGov {
        _openCredit(credit_, face);
        _markS(_snap());
    }

    function issueForAccounting(uint256 amount) external onlyGov {
        uint256[] memory ids = new uint256[](1);
        ids[0] = creditId;
        _bk().mint(amount, ids);
    }

    function sendCash(address to, uint256 amount) external onlyGov {
        Buck(address(buck)).transfer(to, amount);
    }

    function refreshMark() external { _markS(_snap()); }

    function retireForAccounting(uint256 principal) external onlyGov returns (uint256 relief) {
        Buck token = Buck(address(buck));
        token.authorizeReduction(creditId, principal, 0, uint64(block.timestamp + 1 days));
        (relief,) = JubileeFund(token.jubilee()).reduce(creditId, principal);
    }
}

contract BuckCompoundBasketTest is JubileeEquityFixture {
    function test_basketAndValuationLibraryFitEvmRuntimeLimit() public pure {
        assertLe(type(BuckBasketEquity).runtimeCode.length, 24_576);
        assertLe(type(BasketJubileeMath).runtimeCode.length, 24_576);
    }

    function _makeBasket(address factory) internal override returns (BuckBasketEquity) {
        return new CompoundBasketHarness(address(buck), address(k), factory, GOV);
    }

    function _leveraged() private returns (CompoundBasketHarness b, uint256 receiptId) {
        _credit(ALICE, 1000e6);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        receiptId = basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        b = CompoundBasketHarness(address(basket));
        vm.startPrank(GOV);
        b.openAccountingCredit(address(credit), 1e12);
        b.issueForAccounting(40e6);
        vm.stopPrank();
    }

    function test_actualPrincipalIsSeparateFromCashAndReceivingDoesNotRepay() public {
        (CompoundBasketHarness b,) = _leveraged();
        assertEq(basket.lien(), 40e6);
        assertEq(buck.balanceOf(address(basket)), 140e6);
        assertEq(basket.equity(), 100e6);
        assertEq(basket.reliefAccrued(), 0);
        vm.prank(ALICE);
        buck.transfer(address(basket), 10e6);
        assertEq(basket.lien(), 40e6);
        assertEq(basket.equity(), 110e6);
        vm.prank(GOV);
        b.sendCash(ALICE, 15e6);
        assertEq(basket.lien(), 40e6);
        assertEq(basket.equity(), 95e6);
        b.refreshMark();
        assertEq(basket.markNow(), 95e6);
        assertEq(basket.headroom(), int256(55e6));
    }

    function test_mutualClaimMatchesFundButNeverIncreasesCreditCollateral() public {
        (CompoundBasketHarness b,) = _leveraged();
        vm.warp(t0 + YEAR);
        _invest(10e6);
        (uint256 claim,,) = fund.previewRelief(basket.creditId(), 40e6);
        assertGt(claim, 0);
        assertEq(basket.reliefAccrued(), claim / Q);
        uint256 bare = buck.netBalanceOf(address(basket)) - 40e6;
        assertEq(basket.equity(), bare + claim / Q);
        b.refreshMark();
        assertEq(basket.markNow(), bare);
        assertEq(basket.headroom(), int256(bare) - 40e6);
    }

    function test_claimPricedEntryPreservesIncumbentsAndCreditsOnlyDeliveredCash() public {
        (CompoundBasketHarness b, uint256 incumbent) = _leveraged();
        vm.warp(t0 + YEAR);
        _invest(10e6);
        // Realized outside income puts the fund's receipt above basis. Fair
        // entry must include its claim AFTER the existing fee on that gain.
        vm.prank(ALICE);
        buck.transfer(address(basket), 100e6);
        uint256 beforeWorth = basket.valueOf(incumbent);
        uint256 beforeFundWorth = basket.valueOf(fund.positionId());
        (, uint128 fundBasis) = basket.holdings(fund.positionId());
        assertGt(beforeFundWorth, fundBasis);
        (uint256 markedClaim,,) = fund.previewRelief(basket.creditId(), 40e6);
        assertEq(basket.reliefAccrued(), markedClaim / Q);
        vm.startPrank(ALICE);
        uint256 entrant = basket.deposit(address(buck), 10e6, 0);
        vm.stopPrank();
        (, uint128 basis) = basket.holdings(entrant);
        assertEq(basis, 10e6);
        assertApproxEqAbs(basket.valueOf(incumbent), beforeWorth, 2);
        assertApproxEqAbs(basket.valueOf(fund.positionId()), beforeFundWorth, 2);
        assertApproxEqAbs(basket.valueOf(entrant), 10e6, 2);
        assertEq(basket.lien(), 40e6);
        assertEq(basket.markNow(), buck.netBalanceOf(address(basket)) - 40e6);
        b.refreshMark();
    }

    function test_explicitReductionPaysTheMarkedClaimWithoutSellingTheFundReceipt() public {
        (CompoundBasketHarness b,) = _leveraged();
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(10e6);
        (uint128 beforeShares,) = basket.holdings(id);
        (uint256 quoted,, bool limited) = fund.previewRelief(basket.creditId(), 1e6);
        assertFalse(limited);
        uint256 cashBefore = buck.netBalanceOf(address(basket));
        vm.prank(GOV);
        uint256 paid = b.retireForAccounting(1e6);
        assertEq(paid, quoted);
        assertEq(basket.lien(), 39e6);
        assertApproxEqAbs(buck.netBalanceOf(address(basket)), cashBefore - 1e6 + paid / Q, 1);
        (uint128 afterShares,) = basket.holdings(id);
        assertEq(afterShares, beforeShares);
        assertEq(fund.positionId(), id);
    }

    function test_KCutChangesHeadroomWithoutRetiringPrincipalOrCountingClaimAsCash() public {
        (CompoundBasketHarness b,) = _leveraged();
        vm.warp(t0 + YEAR);
        _invest(10e6);
        b.refreshMark();
        k.setK(1e17);
        assertLt(basket.headroom(), 0);
        assertEq(basket.lien(), 40e6);
        assertEq(basket.liquidity(), buck.balanceOf(address(basket)));
        assertGt(basket.reliefAccrued(), 0);
    }
}
