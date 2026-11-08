// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeEquityFixture} from "./JubileeEquityIntegration.t.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {CompoundBasketHarness} from "./BuckCompoundBasket.t.sol";

/// @dev Same basket implementation, canonical Carrying identity and actual
/// net cash. Ordinary fixtures remain comparison/regression coverage.
contract BuckCompoundCarryingBasketTest is JubileeEquityFixture {
    function _carryingBasket() internal pure override returns (bool) { return true; }

    function _makeBasket(address factory) internal override returns (BuckBasketEquity) {
        return new CompoundBasketHarness(address(buck), address(k), factory, GOV);
    }

    function _deposit(uint256 amount) private returns (uint256 id) {
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        id = basket.deposit(address(buck), amount, amount);
        vm.stopPrank();
    }

    function test_nominalCashIsStableButShareValueAndLiquidityAreNet() public {
        _credit(ALICE, 1000e6);
        uint256 id = _deposit(100e6);
        vm.warp(t0 + 10 * YEAR);
        uint256 net = buck.netBalanceOf(address(basket));
        assertEq(buck.balanceOf(address(basket)), 100e6);
        assertLt(net, 100e6);
        assertEq(basket.equity(), net);
        assertEq(basket.valueOf(id), net);
        assertEq(basket.liquidity(), net);
        assertTrue(identity.isCarrying(address(basket)));
        assertTrue(buck.approvedCarryingIssuer(address(basket)));
    }

    function test_agedRedemptionGrossesUpNominalPaymentToDeliverQuotedNetValue() public {
        _credit(ALICE, 1000e6);
        uint256 id = _deposit(100e6);
        vm.warp(t0 + 10 * YEAR);
        uint256 before = buck.netBalanceOf(ALICE);
        uint256 quoted = basket.valueOf(id);
        (uint128 shares,) = basket.holdings(id);
        vm.prank(ALICE);
        (, uint256 paid,,) = basket.redeemShares(id, shares, quoted);
        assertGe(buck.netBalanceOf(ALICE) - before, quoted);
        assertEq(paid, quoted);
        assertEq(basket.totalShares(), 0);
    }

    function testFuzz_agedPartialRedemptionHonorsItsNetQuote(uint32 elapsed, uint16 fraction) public {
        _credit(ALICE, 1000e6);
        uint256 id = _deposit(100e6);
        vm.warp(t0 + bound(uint256(elapsed), 1, 100 * YEAR));
        uint256 bp = bound(uint256(fraction), 1, 10_000);
        (uint128 shares,) = basket.holdings(id);
        uint256 out = uint256(shares) * bp / 10_000;
        uint256 before = buck.netBalanceOf(ALICE);
        vm.prank(ALICE);
        (, uint256 paid,,) = basket.redeemShares(id, out, 1);
        uint256 received = buck.netBalanceOf(ALICE) - before;
        assertGe(received, paid);
        assertLe(received, paid + 1);
        assertEq(basket.lien(), 0);
    }

    function test_freshEntryDoesNotCreditNominalCashAsNetWealth() public {
        _credit(ALICE, 1000e6);
        uint256 incumbent = _deposit(100e6);
        vm.warp(t0 + 10 * YEAR);
        uint256 before = basket.valueOf(incumbent);
        uint256 entrant = _deposit(10e6);
        assertApproxEqAbs(basket.valueOf(incumbent), before, 1);
        assertApproxEqAbs(basket.valueOf(entrant), 10e6, 1);
        (, uint128 basis) = basket.holdings(entrant);
        assertEq(basis, 10e6);
    }

    function test_approvedCarryingBasketIssuesActualPrincipalAndRevocationOnlyStopsNewCredit() public {
        _credit(ALICE, 1000e6);
        uint256 id = _deposit(100e6);
        CompoundBasketHarness b = CompoundBasketHarness(address(basket));
        vm.startPrank(GOV);
        b.openAccountingCredit(address(credit), 1e12);
        b.issueForAccounting(40e6);
        vm.stopPrank();
        assertEq(basket.lien(), 40e6);
        // Change wallet fee basis without changing NFT issuance age. This
        // prevents the homogeneous closed-cash relief from cancelling the
        // wallet's demurrage exactly in the following nominal debit.
        vm.warp(t0 + YEAR + YEAR / 2);
        assertEq(buck.balanceOf(address(basket)), 140e6);
        assertLt(buck.netBalanceOf(address(basket)), 140e6);
        vm.prank(ALICE); buck.transfer(address(basket), 7e6);
        vm.prank(GOV); buck.setApprovedCarryingIssuer(address(basket), false);
        vm.expectRevert(bytes4(keccak256("CarryingIssuerNotApproved()")));
        vm.prank(GOV); b.issueForAccounting(1e6);
        vm.prank(GOV); b.retireForAccounting(1e6);
        assertEq(basket.lien(), 39e6);
        (uint256 nominal,) = buck.accountBalancesScaled(address(basket));
        assertNotEq(nominal % Q, 0, "exact-net retirement leaves fractional nominal basis");
        (uint128 shares,) = basket.holdings(id);
        uint256 before = buck.netBalanceOf(ALICE);
        vm.prank(ALICE);
        (, uint256 paid,,) = basket.redeemShares(id, shares / 4, 1);
        assertGe(buck.netBalanceOf(ALICE) - before, paid);
    }

    function test_jubileeReceiptCreditsOnlyNetDepositedAndRedeemsFullValue() public {
        _credit(ALICE, 10_000e6);
        _deposit(100e6);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(10e6);
        (, uint128 basis) = basket.holdings(id);
        assertGe(basis, 10e6);
        assertLe(basis, 10e6 + 1);
        vm.warp(t0 + 2 * YEAR);
        (uint128 shares,) = basket.holdings(id);
        (, uint256 before) = buck.fundBalancesScaled();
        vm.prank(address(fund));
        (, uint256 paid,,) = basket.redeemShares(id, shares, 1);
        (, uint256 afterValue) = buck.fundBalancesScaled();
        assertGe((afterValue - before) / Q, paid);
    }
}
