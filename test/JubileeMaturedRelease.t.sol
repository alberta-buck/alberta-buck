// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckNet} from "../src/BuckNet.sol";
import {JubileeFund, IJubileeBasket} from "../src/JubileeFund.sol";
import {JubileeClaimBook} from "../src/JubileeClaimBook.sol";
import {NetK} from "./BuckNet.t.sol";
import {JubileeBasketMock, JubileeInsurerPoolMock} from "./JubileeFund.t.sol";

contract JubileeGasHungryInsurerPool {
    IERC20 private immutable token;
    constructor(IERC20 token_) { token = token_; }
    function approveToken() external { token.approve(address(token), type(uint256).max); }
    function empty(address recipient) external { token.transfer(recipient, token.balanceOf(address(this))); }
    function prepareRefund(uint256, uint256) external view {
        while (gasleft() > 1000) { assembly ("memory-safe") { pop(keccak256(0, 0)) } }
        revert("no liquidity");
    }
}

/// @notice Full prototype integration of the 50-year asset release path.
///         Basket and insurer strategy are controllable mocks.
contract JubileeMaturedReleaseTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    uint256 constant Q = 1e36;
    address constant GOV = address(0xA0);
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant INSURER = address(0x1551E1);
    IdentityRegistryHarness identity;
    BuckCredit credit;
    BuckNet buck;
    JubileeFund fund;
    JubileeBasketMock basket;
    uint256 t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        identity = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        NetK k = new NetK();
        fund = new JubileeFund(GOV);
        buck = new BuckNet(address(credit), address(k), address(identity), address(fund));
        basket = new JubileeBasketMock(address(buck));
        credit.setBuck(address(buck));
        vm.prank(GOV);
        identity.setBuck(address(buck));
        _bind(ALICE, false, address(this));
        _bind(BOB, false, address(this));
        _bind(INSURER, false, address(this));
        _bind(address(fund), true, address(this));
        _bind(address(basket), false, address(this));
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
        vm.prank(GOV);
        fund.configure(buck, IJubileeBasket(address(basket)));
    }

    function _bind(address target, bool carrying, address binder) private {
        if (target.code.length == 0) vm.etch(target, hex"60006000fd");
        vm.prank(binder);
        identity.bindContract(target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    function _credit(uint256 face, uint256 spendable, uint32 rate) private returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(ALICE, 0, face, face, BuckCredit.DepreciationType.NONE, 0, 0, rate);
        address expectedPool = buck.insurerPool(INSURER);
        vm.prank(ALICE);
        buck.issue(id, face, spendable, type(uint256).max, expectedPool);
    }

    function _pool() private returns (JubileeInsurerPoolMock pool) {
        pool = new JubileeInsurerPoolMock(buck);
        _bind(address(pool), true, INSURER);
        _bind(address(pool.strategy()), true, INSURER);
        vm.prank(INSURER);
        buck.setInsurerPool(address(pool));
        pool.approveToken();
    }

    function test_spentOutOwnerReleasesNftWithoutSellingOrBurningAndFundBurnsLater() public {
        uint256 id = _credit(1_000_000e6, 1_000_000e6, 0);
        vm.prank(ALICE);
        buck.transfer(BOB, 1_000_000e6);
        vm.warp(t0 + YEAR);
        vm.prank(GOV);
        (uint256 receipt,,) = fund.invest(10_000e6, type(uint256).max, 0, 1);
        (uint128 sharesBefore,) = basket.holdings(receipt);
        basket.setMode(4); // Any attempt to redeem the basket would revert.
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        uint256 bobBefore = buck.netBalanceOf(BOB);
        uint256 benchmarkBefore = buck.claimSeconds();
        assertEq(buck.balanceOf(ALICE), 0);
        vm.prank(ALICE);
        assertEq(fund.closeMatured(id), 1_000_000e6);
        vm.expectRevert();
        credit.ownerOf(id);
        assertEq(buck.balanceOf(ALICE), 0);
        assertEq(buck.netBalanceOf(BOB), bobBefore);
        assertEq(buck.totalSupply(), 1_000_000e6);
        assertEq(buck.claimBook().deferredPrincipal(), 1_000_000e6);
        assertEq(buck.claimSeconds(), benchmarkBefore);
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        (uint128 sharesAfter,) = basket.holdings(receipt);
        assertEq(cashAfter, cashBefore);
        assertEq(sharesAfter, sharesBefore);
        assertEq(fund.positionId(), receipt);

        uint256 limit = cashBefore / (10 * Q);
        vm.expectRevert(JubileeFund.NotInvestmentKeeper.selector);
        vm.prank(BOB);
        fund.burnDeferred(limit);
        vm.expectRevert(BuckNet.ReliefLiquidityLimit.selector);
        vm.prank(GOV);
        fund.burnDeferred(limit + 1);
        vm.prank(GOV);
        fund.burnDeferred(limit);
        assertEq(buck.totalSupply(), 1_000_000e6 - limit);
        assertEq(buck.claimBook().deferredPrincipal(), 1_000_000e6 - limit);
        assertEq(buck.claimSeconds(), (1_000_000e6 - limit) * 50 * YEAR);
        (, cashAfter) = buck.fundBalancesScaled();
        assertEq(cashAfter, cashBefore - limit * Q);
        assertEq(buck.netBalanceOf(BOB), bobBefore);
    }

    function test_releaseRequiresOwnerAndPooledAverageAgeOfFiftyYears() public {
        uint256 id = _credit(2_000_000e6, 1_000_000e6, 0);
        vm.expectRevert(JubileeFund.NotOwner.selector);
        vm.prank(BOB);
        fund.closeMatured(id);
        vm.expectRevert(BuckNet.NotJubilee.selector);
        vm.prank(ALICE);
        buck.matureFromJubilee(ALICE, id);
        vm.warp(t0 + 49 * YEAR);
        vm.prank(ALICE);
        buck.issue(id, 0, 1_000_000e6, type(uint256).max, address(0));
        vm.warp(t0 + 74 * YEAR);
        vm.expectRevert(JubileeClaimBook.NotMature.selector);
        vm.prank(ALICE);
        fund.closeMatured(id);
        assertEq(credit.ownerOf(id), ALICE);
        vm.warp(t0 + 74 * YEAR + YEAR / 2);
        buck.processMaturities(1);
        uint256 ownerBalance = buck.balanceOf(ALICE);
        vm.prank(ALICE);
        assertEq(fund.closeMatured(id), 2_000_000e6);
        assertEq(buck.balanceOf(ALICE), ownerBalance);
        assertEq(buck.claimBook().deferredPrincipal(), 2_000_000e6);
    }

    function test_releaseRefundsTheOriginalPoolNominalDepositWithItsCarriedFee() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 id = _credit(1_000_000e6, 900_000e6, 100);
        vm.prank(ALICE);
        buck.transfer(BOB, 900_000e6);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        uint256 poolNet = buck.netBalanceOf(address(pool));
        assertEq(buck.balanceOf(address(pool)), 100_000e6);
        vm.prank(ALICE);
        fund.closeMatured(id);
        assertApproxEqAbs(buck.balanceOf(ALICE), poolNet, 1);
        assertEq(buck.balanceOf(address(pool)), 0);
        (,, uint256 pending) = buck.claimBook().premiumRefunds(id);
        assertEq(pending, 0);
        assertEq(buck.claimBook().deferredPrincipal(), 1_000_000e6);
    }

    function test_illiquidInsurerCannotTrapLienAndItsOriginalRefundIsRetryable() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 id = _credit(1_000_000e6, 900_000e6, 100);
        vm.prank(ALICE);
        buck.transfer(BOB, 900_000e6);
        pool.invest(100_000e6);
        pool.setFail(true);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        vm.prank(ALICE);
        fund.closeMatured(id);
        vm.expectRevert();
        credit.ownerOf(id);
        assertEq(buck.balanceOf(ALICE), 0);
        (address owner, address refundPool, uint256 pending) = buck.claimBook().premiumRefunds(id);
        assertEq(owner, ALICE);
        assertEq(refundPool, address(pool));
        assertEq(pending, 100_000e6);
        vm.expectRevert();
        fund.claimMaturedPremium(id);
        (,, pending) = buck.claimBook().premiumRefunds(id);
        assertEq(pending, 100_000e6);
        pool.setFail(false);
        vm.prank(BOB); // The beneficiary remains the recorded owner.
        assertEq(fund.claimMaturedPremium(id), 100_000e6);
        assertApproxEqAbs(buck.balanceOf(ALICE), 36_787_944_117, 2);
        (,, pending) = buck.claimBook().premiumRefunds(id);
        assertEq(pending, 0);
        assertEq(buck.totalSupply(), 1_000_000e6);
    }

    function test_gasHungryInsurerCannotBlockTheMaturedRelease() public {
        JubileeGasHungryInsurerPool pool = new JubileeGasHungryInsurerPool(buck);
        _bind(address(pool), true, INSURER);
        vm.prank(INSURER);
        buck.setInsurerPool(address(pool));
        pool.approveToken();
        uint256 id = _credit(1_000_000e6, 900_000e6, 100);
        vm.prank(ALICE);
        buck.transfer(BOB, 900_000e6);
        pool.empty(BOB);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        vm.prank(ALICE);
        fund.closeMatured(id);
        vm.expectRevert();
        credit.ownerOf(id);
        (address owner, address refundPool, uint256 pending) = buck.claimBook().premiumRefunds(id);
        assertEq(owner, ALICE);
        assertEq(refundPool, address(pool));
        assertEq(pending, 100_000e6);
        assertEq(buck.claimBook().deferredPrincipal(), 1_000_000e6);
    }

    function test_deferredDebtKeepsTheReserveTargetAndCanBeReplenishedLater() public {
        uint256 id = _credit(1_000_000e6, 1_000_000e6, 0);
        vm.prank(ALICE);
        buck.transfer(BOB, 1_000_000e6);
        vm.warp(t0 + 49 * YEAR);
        vm.prank(GOV);
        (uint256 receipt,,) = fund.invest(615_000e6, type(uint256).max, 0, 1);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        (uint256 targetBefore,,) = fund.previewReserve();
        (, uint256 cashBefore) = buck.fundBalancesScaled();
        assertLt(cashBefore, targetBefore);
        vm.prank(ALICE);
        fund.closeMatured(id);
        (uint256 targetAfter,,) = fund.previewReserve();
        assertEq(targetAfter, targetBefore);
        JubileeFund.SaleRoute[] memory routes = new JubileeFund.SaleRoute[](0);
        vm.prank(GOV);
        uint256 proceeds = fund.redeemScheduled(1_000e6, 1, routes);
        assertGt(proceeds, 0);
        assertEq(fund.positionId(), receipt);
        (, uint256 cashAfter) = buck.fundBalancesScaled();
        assertEq(cashAfter, cashBefore + proceeds);
        assertEq(buck.claimBook().deferredPrincipal(), 1_000_000e6);
    }
}
