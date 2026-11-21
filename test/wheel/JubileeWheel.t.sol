// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeEquityFixture} from "../basket/JubileeEquityIntegration.t.sol";
import {CompoundBasketHarness} from "../basket/BuckCompoundBasket.t.sol";
import {BuckBasketEquity} from "../../src/basket/BuckBasketEquity.sol";
import {BuckBasketEquityStorage} from "../../src/basket/BuckBasketEquityStorage.sol";
import {JubileeFund, IJubileeValuationWheel} from "../../src/JubileeFund.sol";
import {WorkWheel} from "../../src/wheel/WorkWheel.sol";
import {JubileeKind} from "../../src/wheel/JubileeKind.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";
import {IBasketUpkeep} from "../../src/basket/IBasketUpkeep.sol";
import {EqToken, IPool} from "../basket/BuckBasketEquity.t.sol";
import {MachineEquityMock} from "./BasketMachineWheel.t.sol";

/// @dev Canonical Buck, exact claim book, one fund receipt, actual equity
///      basket and real V3 pools. No inherited standalone test repetitions.
abstract contract JubileeWheelFixture is JubileeEquityFixture {
    function _makeBasket(address factory) internal override returns (BuckBasketEquity) {
        return new CompoundBasketHarness(address(buck), address(k), factory, GOV);
    }

    function _policy() internal pure virtual returns (JubileeKind.JubileePolicy memory) {
        return JubileeKind.JubileePolicy(1, 1000, 100, 5e6, 0, 0);
    }

    function _attach() internal returns (JubileeKind w) {
        w = JubileeKind(address(new BasketMachineWheel(address(buck), address(2), GOV, 0, 0)));
        vm.startPrank(GOV);
        (bool ok,) = address(basket).call(abi.encodeWithSignature("setWheel(address)", address(w)));
        require(ok);
        w.setValuationSource(address(fund));
        fund.setValuationWheel(IJubileeValuationWheel(address(w)));
        fund.setInvestmentKeeper(address(w));
        w.setJubilee(fund, _policy());
        vm.stopPrank();
    }
}

contract JubileeWheelTest is JubileeWheelFixture {
    event JubileeAttempted(uint8 indexed job, bool succeeded, uint256 work, bytes4 reason);

    function test_governanceAuthenticatesWiringAndBoundsBeforeEnabling() public {
        JubileeKind w = JubileeKind(address(new BasketMachineWheel(address(buck), address(2), GOV, 0, 0)));
        JubileeKind.JubileePolicy memory p = _policy();
        vm.expectRevert(WorkWheel.NotGovernance.selector);
        w.setJubilee(fund, p);
        vm.expectRevert(JubileeKind.BadJubileeConfiguration.selector);
        vm.prank(GOV); w.setJubilee(fund, p);
        w = _attach();
        assertEq(w.slotCount(), 3);
        p.slippageBp = 501;
        vm.expectRevert(JubileeKind.BadJubileeConfiguration.selector);
        vm.prank(GOV); w.setJubilee(fund, p);
        p = _policy(); p.maturitySteps = 65;
        vm.expectRevert(JubileeKind.BadJubileeConfiguration.selector);
        vm.prank(GOV); w.setJubilee(fund, p);
        vm.expectRevert(JubileeKind.NotWheelSelf.selector);
        w.runJubileeJob(2);
        vm.prank(GOV); w.setJubilee(JubileeFund(address(0)), p);
        assertEq(w.slotCount(), 0);
    }

    function test_idleCashCheckpointEarnsNothingThenMaturesIntoOneReceipt() public {
        _credit(ALICE, 1_000_000e6);
        JubileeKind w = _attach();
        vm.warp(t0 + YEAR);
        (uint256 amount,) = fund.previewInvestment();
        assertEq(amount, 0, "unobserved cash must not be backdated");
        (uint256 work, uint256 pay) = w.tick(3, 0);
        assertEq(work, 0); assertEq(pay, 0); assertEq(fund.positionId(), 0);
        assertGt(fund.observedExcessScaled(), 0);
        vm.warp(vm.getBlockTimestamp() + 1 days); vm.roll(vm.getBlockNumber() + 1);
        uint256 nominal;
        (amount, nominal) = fund.previewInvestment();
        assertGt(amount, 0); assertGt(nominal, amount);
        (, uint256 beforeCash) = buck.fundBalancesScaled();
        (work,) = w.tick(3, 0);
        assertEq(work, 1);
        uint256 id = fund.positionId();
        assertGt(id, 0); assertEq(basket.receipt().ownerOf(id), address(fund));
        (uint128 shares,) = basket.holdings(id);
        assertGe(shares, amount * 9900 / 10_000);
        (, uint256 afterCash) = buck.fundBalancesScaled();
        assertGe(beforeCash - afterCash, amount * Q);
        assertEq(buck.totalIssued(), 1_000_000e6);
        vm.warp(vm.getBlockTimestamp() + 1 days); vm.roll(vm.getBlockNumber() + 1);
        (work,) = w.tick(3, 0);
        assertEq(work, 1); assertEq(fund.positionId(), id);
        (uint128 increased,) = basket.holdings(id);
        assertGt(increased, shares);
    }

    function test_failedEntryShareBoundPreservesCashBudgetAndReceiptThenRetriesNextBlock() public {
        _credit(ALICE, 1_000_000e6);
        JubileeKind w = _attach();
        vm.warp(t0 + YEAR); w.tick(3, 0);
        vm.warp(vm.getBlockTimestamp() + 1 days); vm.roll(vm.getBlockNumber() + 1);
        (, uint256 cash) = buck.fundBalancesScaled();
        uint256 checkpoint = fund.reserveCheckpoint();
        uint256 budget = fund.reserveBudgetScaled();
        // Inject an impossible advisory share price; the actual entry still
        // computes fair shares and must reject the wheel's positive minimum.
        vm.mockCall(address(basket), abi.encodeWithSignature("sharePrice()"), abi.encode(uint256(1)));
        vm.expectEmit(true, false, false, true, address(w));
        emit JubileeAttempted(2, false, 0, BuckBasketEquityStorage.MinOut.selector);
        (uint256 work,) = w.tick(3, 0);
        assertEq(work, 0); assertEq(fund.positionId(), 0);
        assertEq(fund.reserveCheckpoint(), checkpoint); assertEq(fund.reserveBudgetScaled(), budget);
        (, uint256 afterCash) = buck.fundBalancesScaled(); assertEq(afterCash, cash);
        vm.clearMockedCalls();
        w.rearm(); (work,) = w.tick(3, 0); assertEq(work, 0);
        vm.roll(vm.getBlockNumber() + 1); (work,) = w.tick(3, 0);
        assertEq(work, 1); assertGt(fund.positionId(), 0);
    }

    function test_machineWheelRestoresCashFromTheSameReceipt() public {
        _credit(ALICE, 1_000_000e6);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(19_000e6);
        JubileeKind w = _attach();
        (uint128 shares,) = basket.holdings(id);
        (, uint256 beforeCash) = buck.fundBalancesScaled();
        (uint256 target,,) = fund.previewReserve();
        assertLt(beforeCash, target);
        (uint256 work,) = w.tick(3, 0);
        assertEq(work, 1);
        (uint128 left,) = basket.holdings(id);
        assertLt(left, shares); assertGe(left, uint256(shares) * 9000 / 10_000);
        (, uint256 afterCash) = buck.fundBalancesScaled();
        assertGt(afterCash, beforeCash); assertGe(afterCash + Q, target);
        assertEq(fund.positionId(), id); assertEq(buck.totalIssued(), 1_000_000e6);
        w.rearm(); (work,) = w.tick(3, 0);
        assertEq(work, 0, "rearm cannot run this block's reserve job twice");
    }

    function test_revokedKeeperDoesNotBlockUnrelatedBasketWork() public {
        _credit(ALICE, 1_000_000e6);
        vm.warp(t0 + YEAR); _invest(19_000e6);
        JubileeKind w = _attach();
        vm.prank(GOV); fund.setInvestmentKeeper(address(0));
        MachineEquityMock other = new MachineEquityMock();
        vm.prank(GOV); BasketMachineWheel(address(w)).setEquity(address(other));
        uint256 id = fund.positionId();
        (uint128 shares,) = basket.holdings(id);
        (uint256 work,) = w.tick(10, 0);
        assertEq(work, 1); assertEq(other.calls(), 1);
        (uint128 afterShares,) = basket.holdings(id);
        assertEq(afterShares, shares);
        vm.prank(GOV); fund.setInvestmentKeeper(address(w));
        w.rearm(); (work,) = w.tick(3, 0);
        assertEq(work, 2); assertEq(other.calls(), 2);
    }

    function test_maturityBacklogAdvancesInBoundedBatchesWithoutClosingOwnersNfts() public {
        uint256 a = _credit(ALICE, 100e6);
        uint256 b = _credit(BOB, 100e6);
        JubileeKind w = _attach();
        vm.warp(t0 + 50 * YEAR);
        assertTrue(buck.claimBook().pending());
        (uint256 work,) = w.tick(3, 0);
        assertEq(work, 1); assertTrue(buck.claimBook().pending());
        assertEq(credit.ownerOf(a), ALICE); assertEq(credit.ownerOf(b), BOB);
        w.rearm(); (work,) = w.tick(3, 0);
        assertEq(work, 0); assertTrue(buck.claimBook().pending());
        vm.roll(vm.getBlockNumber() + 1); (work,) = w.tick(3, 0);
        assertEq(work, 1); assertFalse(buck.claimBook().pending());
        assertEq(buck.claimSeconds(), 200e6 * 50 * YEAR);
        assertEq(buck.totalIssued(), 200e6);
    }

    function test_deferredBurnWaitsForSharedBudgetAndResumesWithGovernedCap() public {
        uint256 a = _credit(ALICE, 1000e6);
        _credit(BOB, 1000e6);
        JubileeKind w = _attach();
        vm.warp(t0 + 50 * YEAR); buck.processMaturities(10);
        vm.prank(ALICE); fund.closeMatured(a);
        assertEq(buck.claimBook().deferredPrincipal(), 1000e6);
        uint256 available = buck.availableReliefScaled() / Q;
        vm.prank(GOV); fund.burnDeferred(available);
        uint256 supply = buck.totalIssued();
        (uint256 work,) = w.tick(3, 0);
        assertEq(work, 0); assertEq(buck.totalIssued(), supply);
        assertEq(fund.positionId(), 0, "do not invest while deferred principal is owed");
        vm.roll(vm.getBlockNumber() + 1); (work,) = w.tick(3, 0);
        assertEq(work, 1); assertEq(buck.totalIssued(), supply - 5e6);
        assertEq(buck.claimBook().deferredPrincipal(), 1000e6 - available - 5e6);
        w.rearm(); (work,) = w.tick(3, 0);
        assertEq(work, 0); assertEq(buck.totalIssued(), supply - 5e6);
    }

    function test_lowKRefusesFundSaleWithoutChangingItsReceiptOrOtherWork() public {
        _credit(ALICE, 1_000_000e6);
        vm.warp(t0 + YEAR);
        (uint256 id,,) = _invest(19_000e6);
        CompoundBasketHarness b = CompoundBasketHarness(address(basket));
        vm.startPrank(GOV);
        b.openAccountingCredit(address(credit), 1e14);
        b.issueForAccounting(5_000e6);
        vm.stopPrank();
        JubileeKind w = _attach();
        k.setK(0.01e18);
        assertLt(basket.headroom(), 0);
        MachineEquityMock other = new MachineEquityMock();
        vm.prank(GOV); BasketMachineWheel(address(w)).setEquity(address(other));
        (uint128 shares,) = basket.holdings(id);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint256 work,) = w.tick(10, 0);
        assertEq(work, 1); assertEq(other.calls(), 1);
        (uint128 left,) = basket.holdings(id);
        assertEq(left, shares); assertEq(basket.lien(), 5_000e6);
        (, uint256 afterCash) = buck.fundBalancesScaled();
        assertEq(afterCash, cash);
        assertEq(w.jubileeAttemptClock(2), vm.getBlockNumber() + 1);
    }

    function test_valuationPublicationNeverRunsDuePortfolioWork() public {
        _credit(ALICE, 1_000_000e6);
        vm.warp(t0 + YEAR); (uint256 id,,) = _invest(19_000e6);
        JubileeKind w = _attach();
        assertTrue(w.jubileeDue(2));
        uint256 cursor = w.cursor();
        (uint128 shares,) = basket.holdings(id);
        (, uint256 cash) = buck.fundBalancesScaled();
        w.refreshValuation();
        assertEq(w.cursor(), cursor); assertEq(w.jubileeAttemptClock(2), 0);
        (uint128 left,) = basket.holdings(id);
        assertEq(left, shares);
        (, uint256 afterCash) = buck.fundBalancesScaled(); assertEq(afterCash, cash);
        assertTrue(w.valuationReady());
    }

    function _realPoolSale(bool buckIs0, bool tight) private {
        _bind(address(this), false, address(this));
        _credit(ALICE, 2_000_000e6);
        vm.prank(ALICE); buck.transfer(address(this), 1_500_000e6);
        EqToken template = new EqToken("Asset", "AST", 18);
        address assetAddress = buckIs0 ? address(type(uint160).max - 1) : address(0x100000);
        vm.etch(assetAddress, address(template).code);
        EqToken asset = EqToken(assetAddress);
        asset.mint(address(this), 2_000_000e18); asset.mint(ALICE, 100_000e18);
        vm.prank(GOV);
        address pool = basket.addBasketToken(assetAddress, 18, 1e6, 10_000, 3000);
        _bind(pool, true, address(this));
        (,,,,,, int24 lower, int24 upper, bool order,,) = basket.constituents(0);
        assertEq(order, buckIs0);
        IPool(pool).mint(address(this), lower, upper, 1e18, "");
        vm.warp(t0 + YEAR);
        vm.startPrank(ALICE);
        asset.approve(address(basket), type(uint256).max);
        basket.deposit(assetAddress, 100_000e18, 0);
        vm.stopPrank();
        (uint256 id,,) = _invest(39_000e6);
        assertEq(IBasketUpkeep(address(basket)).wheelStep(2, 0), 1);
        vm.warp(vm.getBlockTimestamp() + 601); vm.roll(vm.getBlockNumber() + 1);
        JubileeKind w = _attach();
        if (tight) {
            JubileeKind.JubileePolicy memory p = _policy(); p.slippageBp = 2;
            vm.prank(GOV); w.setJubilee(fund, p);
        }
        asset.mint(address(fund), 777);
        (uint128 shares,) = basket.holdings(id);
        (, uint256 cash) = buck.fundBalancesScaled();
        uint128 liquidity = basket.liquidityOf(0);
        (uint256 work,) = w.tick(3, 0);
        (uint128 left,) = basket.holdings(id);
        (, uint256 afterCash) = buck.fundBalancesScaled();
        assertEq(asset.balanceOf(address(fund)), 777, "sell no pre-existing TOKEN");
        assertEq(buck.totalIssued(), 2_000_000e6);
        assertEq(w.jubileeAttemptClock(2), vm.getBlockNumber() + 1);
        if (tight) {
            assertEq(work, 0); assertEq(left, shares); assertEq(afterCash, cash);
            assertEq(basket.liquidityOf(0), liquidity);
            w.rearm(); (work,) = w.tick(3, 0); assertEq(work, 0);
        } else {
            assertEq(work, 1); assertLt(left, shares); assertGt(afterCash, cash);
            assertLt(basket.liquidityOf(0), liquidity);
        }
    }

    function test_realInKindSaleThroughCanonicalPoolBuckToken0() public { _realPoolSale(true, false); }
    function test_realInKindSaleThroughCanonicalPoolBuckToken1() public { _realPoolSale(false, false); }
    function test_failedPositiveSaleBoundRollsBackLiquidityCashAndReceipt() public { _realPoolSale(true, true); }
}
