// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeEquityFixture} from "./JubileeEquityIntegration.t.sol";
import {CompoundBasketHarness} from "./BuckCompoundBasket.t.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {JubileeFund, IJubileeValuationWheel} from "../../src/JubileeFund.sol";
import {Buck} from "../../src/Buck.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";

/// @dev A registered valuation-only source tries all three financial entries
///      before reading the real fund's complete position mark. No fake values
///      or successful nested mutations can make this fixture pass.
contract ReenterCompoundValuation is IJubileeValuationWheel {
    address public immutable valuationSource;
    BuckBasketEquity public immutable basket;
    bool public armed;
    uint256 public rejected;
    bool public readDuringCredit;
    uint256 private mark;
    bytes32 private key;

    constructor(JubileeFund fund, BuckBasketEquity basket_) {
        valuationSource = address(fund);
        basket = basket_;
    }

    function arm() external { armed = true; }

    function refreshValuation() external returns (uint256, bytes32) {
        if (armed) {
            _mustReject(abi.encodeCall(basket.retireCredit, (1, type(uint256).max, 0)));
            _mustReject(abi.encodeCall(basket.deposit, (address(0), 0, 0)));
            _mustReject(abi.encodeCall(IBasketUpkeep.wheelStep, (0, 0)));
            _mustReject(abi.encodeCall(IBasketUpkeep.initializeEquityCredit, (address(0), 0)));
        }
        (mark, key) = JubileeFund(valuationSource).wheelValuation();
        if (armed) readDuringCredit = mark > 0;
        return (mark, key);
    }

    function _mustReject(bytes memory data) private {
        (bool ok, bytes memory reason) = address(basket).call(data);
        require(!ok && reason.length >= 4 && bytes4(reason) == BuckBasketEquity.PositionReentrant.selector,
            "nested financial entry not rejected by basket lock");
        ++rejected;
    }

    function publishedValuation() external view returns (address, uint256, bytes32, uint256, uint256, bool) {
        return (valuationSource, mark, key, block.number, block.timestamp, key != bytes32(0));
    }
}

contract BuckCompoundBasketRepaymentTest is JubileeEquityFixture {
    CompoundBasketHarness b;

    function _makeBasket(address factory) internal override returns (BuckBasketEquity) {
        return new CompoundBasketHarness(address(buck), address(k), factory, GOV);
    }

    function _debt() private {
        _credit(ALICE, 1000e6);
        vm.startPrank(ALICE);
        buck.approve(address(basket), type(uint256).max);
        basket.deposit(address(buck), 100e6, 0);
        vm.stopPrank();
        b = CompoundBasketHarness(address(basket));
        vm.startPrank(GOV);
        b.openAccountingCredit(address(credit), 1e12);
        b.fundForAccounting(140e6);
        vm.stopPrank();
        vm.warp(t0 + YEAR);
    }

    function _assertNoAuthorization() private view {
        (uint256 principal, uint256 minimum, uint64 deadline) = buck.reductionAuthorization(basket.creditId());
        assertEq(principal, 0);
        assertEq(minimum, 0);
        assertEq(deadline, 0);
    }

    function test_partialAndFullSettlementBurnActualSupplyThenAllowANewDraw() public {
        _debt();
        uint256 id = basket.creditId();
        (uint256 quoted,, bool limited) = fund.previewRelief(id, 5e6);
        assertFalse(limited);
        uint256 supply = buck.totalIssued();
        (, uint256 cash) = buck.accountBalancesScaled(address(basket));
        (, uint256 fundCash) = buck.fundBalancesScaled();
        // This fixture contract is the authorized wheel.
        (uint256 relief, uint256 cost) = basket.retireCredit(5e6, 5e6, quoted);
        assertEq(relief, quoted);
        assertEq(cost, 5e6 * Q - relief);
        assertEq(basket.lien(), 35e6);
        assertEq(buck.totalIssued(), supply - 5e6);
        (, uint256 afterCash) = buck.accountBalancesScaled(address(basket));
        (, uint256 afterFund) = buck.fundBalancesScaled();
        assertEq(cash - afterCash, cost);
        assertEq(fundCash - afterFund, relief);
        assertTrue(basket.creditLive());
        _assertNoAuthorization();

        vm.prank(GOV);
        basket.retireCredit(35e6, type(uint256).max, 0);
        assertEq(basket.lien(), 0);
        assertEq(buck.totalIssued(), supply - 40e6);
        assertFalse(basket.creditLive());
        // Settlement deactivates coverage with the principal; the self-insured backing is
        // activated again at once (no cash, no principal, no claim age).
        (uint256 face, uint256 active,) = credit.creditInfo(id);
        assertEq(active, face);
        assertEq(credit.ownerOf(id), address(basket));
        uint256 beforeDraw = buck.balanceOf(address(basket));
        vm.prank(GOV);
        b.fundForAccounting(beforeDraw + 10e6);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 10e6);
        assertEq(seconds_, 0);
        assertTrue(basket.creditLive());
    }

    function test_walletCostLimitRollsBackAnOtherwiseSuccessfulSettlement() public {
        _debt();
        uint256 supply = buck.totalIssued();
        uint256 totalSeconds = buck.claimSeconds();
        uint256 mark = basket.markNow();
        uint256 budget = buck.availableReliefScaled();
        (, uint256 cash) = buck.accountBalancesScaled(address(basket));
        (uint256 fundGross, uint256 fundNet) = buck.fundBalancesScaled();
        vm.expectRevert(BuckBasketEquity.CreditCashLimit.selector);
        basket.retireCredit(1e6, 0, 0);
        assertEq(basket.lien(), 40e6);
        assertEq(buck.totalIssued(), supply);
        assertEq(buck.claimSeconds(), totalSeconds);
        assertEq(basket.markNow(), mark);
        assertEq(buck.availableReliefScaled(), budget);
        (, uint256 afterCash) = buck.accountBalancesScaled(address(basket));
        (uint256 afterGross, uint256 afterNet) = buck.fundBalancesScaled();
        assertEq(afterCash, cash);
        assertEq(afterGross, fundGross);
        assertEq(afterNet, fundNet);
        _assertNoAuthorization();
    }

    function test_minimumReliefUsesFullScaledNetUnitsAndRollsBackOnFailure() public {
        _debt();
        (uint256 quoted,,) = fund.previewRelief(basket.creditId(), 1e6);
        vm.expectRevert(Buck.BadAuthorization.selector);
        basket.retireCredit(1e6, 1e6, quoted + 1);
        assertEq(basket.lien(), 40e6);
        _assertNoAuthorization();
        (uint256 relief,) = basket.retireCredit(1e6, 1e6, quoted);
        assertEq(relief, quoted);
    }

    function test_tightCashBoundChecksTheActualFractionalNetDebit() public {
        _debt();
        (uint256 quoted,,) = fund.previewRelief(basket.creditId(), 1e6);
        uint256 exactCost = 1e6 * Q - quoted;
        uint256 floorCost = exactCost / Q;
        assertGt(exactCost % Q, 0);
        vm.expectRevert(BuckBasketEquity.CreditCashLimit.selector);
        basket.retireCredit(1e6, floorCost, quoted);
        assertEq(basket.lien(), 40e6);
        _assertNoAuthorization();
        (, uint256 paid) = basket.retireCredit(1e6, floorCost + 1, quoted);
        assertEq(paid, exactCost);
    }

    function test_liquidityRefusalLeavesPositionsAndAuthorizationUntouched() public {
        _debt();
        (uint256 id,,) = _invest(18e6);
        (uint128 shares, uint128 basis) = basket.holdings(id);
        (,, bool limited) = fund.previewRelief(basket.creditId(), 40e6);
        assertTrue(limited);
        uint256 supply = buck.totalIssued();
        (, uint256 cash) = buck.fundBalancesScaled();
        vm.expectRevert(JubileeFund.ReliefLiquidityLimit.selector);
        basket.retireCredit(40e6, type(uint256).max, 0);
        assertEq(basket.lien(), 40e6);
        assertEq(buck.totalIssued(), supply);
        (, uint256 afterCash) = buck.fundBalancesScaled();
        assertEq(afterCash, cash);
        (uint128 afterShares, uint128 afterBasis) = basket.holdings(id);
        assertEq(afterShares, shares);
        assertEq(afterBasis, basis);
        assertEq(fund.positionId(), id);
        _assertNoAuthorization();
    }

    function test_repeatedCallsShareOneBlockBudgetAndResumeInALaterBlock() public {
        _debt();
        (uint256 id,,) = _invest(18e6);
        (uint128 shares,) = basket.holdings(id);
        (, uint256 initialCash) = buck.fundBalancesScaled();
        uint256 paid;
        uint256 count;
        bool stopped;
        for (; count < 40; ++count) {
            (,, bool limited) = fund.previewRelief(basket.creditId(), 1e6);
            if (limited) {
                vm.expectRevert(JubileeFund.ReliefLiquidityLimit.selector);
                basket.retireCredit(1e6, 1e6, 0);
                stopped = true;
                break;
            }
            (uint256 relief,) = basket.retireCredit(1e6, 1e6, 0);
            paid += relief;
        }
        assertTrue(stopped);
        assertGt(count, 0);
        assertLe(paid, initialCash / 10);
        assertEq(basket.lien(), (40 - count) * 1e6);
        _assertNoAuthorization();
        vm.roll(block.number + 1);
        basket.retireCredit(1e6, 1e6, 0);
        assertEq(basket.lien(), (39 - count) * 1e6);
        (uint128 afterShares,) = basket.holdings(id);
        assertEq(afterShares, shares);
    }

    function test_lowKRetirementImprovesHeadroomWithoutPretendingCashRepaidDebt() public {
        _debt();
        k.setK(0.1e18);
        b.refreshMark();
        int256 beforeRoom = basket.headroom();
        assertLt(beforeRoom, 0);
        basket.retireCredit(10e6, 10e6, 0);
        assertEq(basket.lien(), 30e6);
        assertGt(basket.headroom(), beforeRoom);
        assertLt(basket.headroom(), 0); // There is still an actual shortfall.
    }

    function test_onlyAuthorizedCallersCanRetireAndZeroCannotCloseAnNft() public {
        _debt();
        vm.expectRevert(bytes4(keccak256("NotWheel()")));
        vm.prank(BOB);
        basket.retireCredit(1e6, 1e6, 0);
        vm.expectRevert(bytes4(keccak256("Amount0()")));
        basket.retireCredit(0, type(uint256).max, 0);
        assertEq(basket.lien(), 40e6);
        assertEq(credit.ownerOf(basket.creditId()), address(basket));
        _assertNoAuthorization();
    }

    function test_creditSettlementCannotRunInsideAnActivePositionOperation() public {
        _debt();
        vm.expectRevert(BuckBasketEquity.PositionReentrant.selector);
        vm.prank(GOV);
        b.retireInsidePositionForAccounting(1e6);
        assertEq(basket.lien(), 40e6);
        _assertNoAuthorization();
        basket.retireCredit(1e6, 1e6, 0); // The reverted lock did not stick.
        assertEq(basket.lien(), 39e6);
    }

    function test_valuationRefreshCanReadButCannotReenterAnyFinancialOperation() public {
        _debt();
        (uint256 id,,) = _invest(1e6);
        ReenterCompoundValuation source = new ReenterCompoundValuation(fund, basket);
        vm.startPrank(GOV);
        (bool ok,) = address(basket).call(abi.encodeWithSignature("setWheel(address)", address(source)));
        require(ok);
        fund.setValuationWheel(source);
        vm.stopPrank();
        source.refreshValuation(); // Publish an initial quote before arming.
        source.arm();
        (uint128 beforeShares,) = basket.holdings(id);
        vm.prank(GOV);
        basket.retireCredit(1e6, 1e6, 0);
        assertEq(source.rejected(), 4);
        assertTrue(source.readDuringCredit());
        assertEq(basket.lien(), 39e6);
        (uint128 afterShares,) = basket.holdings(id);
        assertEq(afterShares, beforeShares);
    }
}
