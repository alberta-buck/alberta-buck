// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckNet} from "../src/BuckNet.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {Vm} from "forge-std/Vm.sol";
import {JubileeFund, JubileeFundTest, JubileeInsurerPoolMock} from "./JubileeFund.t.sol";
import {TestWheel} from "./wheel/WorkWheel.t.sol";

/// @dev Exercises the original burn entry-point shapes against actual ordinary
///      wallet balances, a real fund and per-insurer Carrying pools.
contract BuckNetBurnCompatibilityTest is JubileeFundTest {
    function test_burnReducesTheActualBalanceAndNftPrincipal() public {
        uint256 id = _credit(ALICE, 100e6, 0);
        _authorize(ALICE, id, 3e6, 0);
        vm.prank(ALICE);
        buck.burn(10e6);
        assertEq(buck.balanceOf(ALICE), 90e6);
        assertEq(buck.totalIssued(), 90e6);
        assertEq(buck.mintsBacked(id), 90e6);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 90e6);
        (uint256 authorized,,) = buck.reductionAuthorization(id);
        assertEq(authorized, 0); // an older exact-reduction consent is no longer valid
    }

    function test_burnIncludesMarkedReliefAndTheAgedInsurerRefund() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 id = _credit(ALICE, 1000e6, 100);
        _credit(BOB, 1000e6, 0);
        vm.warp(t0 + YEAR);
        uint256 beforeBalance = buck.balanceOf(ALICE);
        (, uint256 beforeFund) = buck.fundBalancesScaled();
        vm.prank(ALICE);
        buck.burn(100e6);
        assertGe(beforeBalance - buck.balanceOf(ALICE), 100e6);
        (uint256 remaining,) = credit.issuanceOf(id);
        assertGt(1000e6 - remaining, 100e6);
        assertLt(buck.balanceOf(address(pool)), 100e6);
        (, uint256 afterFund) = buck.fundBalancesScaled();
        assertLt(afterFund, beforeFund);
        assertEq(pool.calls(), 0); // no insurer callback under the token guard
    }

    function test_burnMatchesTheSameAuthorizedFundReduction() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 id = _credit(ALICE, 1000e6, 100);
        _credit(BOB, 1000e6, 0);
        vm.warp(t0 + YEAR);
        uint256 snapshot = vm.snapshotState();
        vm.recordLogs();
        vm.prank(ALICE);
        buck.burn(100e6);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 signature = keccak256("FacilityReduced(address,uint256,uint256,uint256,uint256,uint256)");
        uint256 retired;
        uint256 relief;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter == address(buck) && logs[i].topics[0] == signature) {
                assertEq(retired, 0);
                (retired, relief,,) = abi.decode(logs[i].data, (uint256, uint256, uint256, uint256));
            }
        }
        assertGt(retired, 100e6);
        uint256 actualBalance = buck.balanceOf(ALICE);
        uint256 actualPool = buck.balanceOf(address(pool));
        uint256 actualIssued = buck.totalIssued();
        (, uint256 actualFund) = buck.fundBalancesScaled();
        assertTrue(vm.revertToState(snapshot));
        (uint256 quoted,, bool exceeds) = fund.previewRelief(id, retired);
        assertFalse(exceeds);
        assertEq(quoted, relief);
        _authorize(ALICE, id, retired, relief);
        vm.prank(ALICE);
        fund.reduce(id, retired);
        assertEq(buck.balanceOf(ALICE), actualBalance);
        assertEq(buck.balanceOf(address(pool)), actualPool);
        assertEq(buck.totalIssued(), actualIssued);
        (, uint256 directFund) = buck.fundBalancesScaled();
        assertEq(directFund, actualFund);
    }

    function test_burnUsesMostExpensiveFirstOrTheExplicitOrder() public {
        _pool();
        uint256 expensive = _credit(ALICE, 100e6, 200);
        uint256 cheap = _credit(ALICE, 100e6, 100);
        uint256 snapshot = vm.snapshotState();
        vm.prank(ALICE);
        buck.burn(50e6);
        (uint256 expensiveLeft,) = credit.issuanceOf(expensive);
        (uint256 cheapLeft,) = credit.issuanceOf(cheap);
        assertLt(expensiveLeft, 100e6);
        assertEq(cheapLeft, 100e6);
        assertTrue(vm.revertToState(snapshot));

        uint256[] memory ids = new uint256[](2);
        ids[0] = cheap;
        ids[1] = expensive;
        vm.prank(ALICE);
        buck.burn(50e6, ids);
        (expensiveLeft,) = credit.issuanceOf(expensive);
        (cheapLeft,) = credit.issuanceOf(cheap);
        assertEq(expensiveLeft, 100e6);
        assertLt(cheapLeft, 100e6);
    }

    function test_burnCanSpanMultipleFacilities() public {
        _pool();
        uint256 expensive = _credit(ALICE, 100e6, 200);
        uint256 cheap = _credit(ALICE, 100e6, 100);
        uint256 beforeBalance = buck.balanceOf(ALICE);
        vm.prank(ALICE);
        buck.burn(120e6);
        assertGe(beforeBalance - buck.balanceOf(ALICE), 120e6);
        (uint256 expensiveLeft,) = credit.issuanceOf(expensive);
        (uint256 cheapLeft,) = credit.issuanceOf(cheap);
        assertLt(expensiveLeft, 100e6);
        assertLt(cheapLeft, 100e6);
    }

    function test_agedMultiFacilityBurnMatchesSequentialFundReductions() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 first = _credit(ALICE, 100e6, 200);
        uint256 second = _credit(ALICE, 100e6, 100);
        _credit(BOB, 2000e6, 0);
        vm.warp(t0 + YEAR);
        uint256 snapshot = vm.snapshotState();
        vm.recordLogs();
        vm.prank(ALICE);
        buck.burn(120e6);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bytes32 signature = keccak256("FacilityReduced(address,uint256,uint256,uint256,uint256,uint256)");
        uint256[2] memory retired;
        uint256[2] memory relief;
        uint256 count;
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter != address(buck) || logs[i].topics[0] != signature) continue;
            assertLt(count, 2);
            assertEq(uint256(logs[i].topics[2]), count == 0 ? first : second);
            (retired[count], relief[count],,) = abi.decode(logs[i].data, (uint256, uint256, uint256, uint256));
            ++count;
        }
        assertEq(count, 2);
        uint256 actualBalance = buck.balanceOf(ALICE);
        uint256 actualPool = buck.balanceOf(address(pool));
        uint256 actualPoolNet = buck.netBalanceOf(address(pool));
        uint256 actualIssued = buck.totalIssued();
        (, uint256 actualFund) = buck.fundBalancesScaled();
        assertTrue(vm.revertToState(snapshot));
        _authorize(ALICE, first, retired[0], relief[0]);
        vm.prank(ALICE);
        fund.reduce(first, retired[0]);
        _authorize(ALICE, second, retired[1], relief[1]);
        vm.prank(ALICE);
        fund.reduce(second, retired[1]);
        assertEq(buck.balanceOf(ALICE), actualBalance);
        assertEq(buck.balanceOf(address(pool)), actualPool);
        assertEq(buck.netBalanceOf(address(pool)), actualPoolNet);
        assertEq(buck.totalIssued(), actualIssued);
        (, uint256 directFund) = buck.fundBalancesScaled();
        assertEq(directFund, actualFund);
    }

    function test_burnRollsBackEarlierFacilitiesAndWheelPublicationOnShortfall() public {
        uint256 first = _credit(ALICE, 10e6, 0);
        uint256 second = _credit(ALICE, 10e6, 0);
        _credit(BOB, 200e6, 0);
        vm.warp(t0 + YEAR);
        _invest(1e6);
        TestWheel wheel = _valuationWheel();
        vm.prank(BOB);
        buck.transfer(ALICE, 50e6);
        _authorize(ALICE, first, 1e6, 0);
        uint256 beforeBalance = buck.balanceOf(ALICE);
        uint256 beforeIssued = buck.totalIssued();
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        buck.burn(25e6);
        assertEq(buck.balanceOf(ALICE), beforeBalance);
        assertEq(buck.totalIssued(), beforeIssued);
        (uint256 firstPrincipal,) = credit.issuanceOf(first);
        (uint256 secondPrincipal,) = credit.issuanceOf(second);
        assertEq(firstPrincipal, 10e6);
        assertEq(secondPrincipal, 10e6);
        (uint256 authorized,,) = buck.reductionAuthorization(first);
        assertEq(authorized, 1e6);
        assertFalse(wheel.valuationReady());
    }

    function test_burnRevertsAtomicallyWhenTheInsurerPoolIsIlliquid() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 id = _credit(ALICE, 100e6, 200);
        pool.invest(20e6);
        uint256 beforeBalance = buck.balanceOf(ALICE);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        buck.burn(10e6);
        assertEq(buck.balanceOf(ALICE), beforeBalance);
        assertEq(buck.totalIssued(), 100e6);
        assertEq(buck.mintsBacked(id), 100e6);
        assertEq(buck.premiumPrincipal(id), 20e6);
        assertEq(pool.calls(), 0);
    }

    function test_burnRejectsExcessDuplicateAndForeignSelections() public {
        uint256 own = _credit(ALICE, 100e6, 0);
        uint256 foreign = _credit(BOB, 100e6, 0);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        buck.burn(type(uint256).max);
        uint256[] memory ids = new uint256[](2);
        ids[0] = own;
        ids[1] = own;
        vm.expectRevert(BuckNet.BadAmount.selector);
        vm.prank(ALICE);
        buck.burn(1, ids);
        ids[1] = foreign;
        vm.expectRevert(BuckNet.NotOwner.selector);
        vm.prank(ALICE);
        buck.burn(1, ids);
        assertEq(buck.totalIssued(), 200e6);
        assertEq(buck.balanceOf(ALICE), 100e6);
    }

    function test_burnRequiresVerifiedOrdinaryOwnerAndFund() public {
        vm.expectRevert(BuckNet.NotVerified.selector);
        vm.prank(address(0xCAFE));
        buck.burn(0);
        identity.setIsCarrying(BOB, true);
        vm.expectRevert(BuckNet.NotCarryingPool.selector);
        vm.prank(BOB);
        buck.burn(0);
    }

    function test_burnRefreshesTheWheelWithoutPerformingWork() public {
        _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        _invest(2e6);
        TestWheel wheel = _valuationWheel();
        wheel.setA(1);
        wheel.setAPending(0, 1);
        wheel.refreshValuation();
        uint256 stale = wheel.valuationMark();
        basket.realizeLoss(BOB, 500_000);
        uint256 beforeBalance = buck.balanceOf(ALICE);
        vm.prank(ALICE);
        buck.burn(1e6);
        assertGe(beforeBalance - buck.balanceOf(ALICE), 1e6);
        assertTrue(wheel.valuationReady());
        assertEq(wheel.valuationBlock(), block.number);
        assertLt(wheel.valuationMark(), stale);
        assertEq(wheel.aPending(0), 1);
        assertEq(wheel.cursor(), 0);
        assertEq(wheel.reserve(), 0);
    }

    function test_burnRejectsAnUnavailableWheelMarkAtomically() public {
        uint256 id = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        _invest(2e6);
        TestWheel wheel = _valuationWheel();
        vm.prank(GOV);
        wheel.setValuationSource(address(basket));
        uint256 beforeBalance = buck.balanceOf(ALICE);
        uint256 beforeIssued = buck.totalIssued();
        vm.expectRevert(JubileeFund.MarkUnavailable.selector);
        vm.prank(ALICE);
        buck.burn(1e6);
        assertEq(buck.balanceOf(ALICE), beforeBalance);
        assertEq(buck.totalIssued(), beforeIssued);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 100e6);
        assertFalse(wheel.valuationReady());
    }

    function test_burnSharesTheJubileeLimitAcrossTheBlock() public {
        _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        vm.prank(ALICE);
        buck.burn(10e6);
        uint256 beforeBob = buck.balanceOf(BOB);
        uint256 beforeIssued = buck.totalIssued();
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(BOB);
        buck.burn(10e6);
        assertEq(buck.balanceOf(BOB), beforeBob);
        assertEq(buck.totalIssued(), beforeIssued);
        vm.roll(block.number + 1);
        vm.prank(BOB);
        buck.burn(10e6);
        assertGe(beforeBob - buck.balanceOf(BOB), 10e6);
    }

    function test_burnSharesTheBlockLimitWithAnAuthorizedFundReduction() public {
        uint256 first = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        vm.warp(t0 + YEAR);
        _authorize(ALICE, first, 10e6, 0);
        vm.prank(ALICE);
        fund.reduce(first, 10e6);
        uint256 beforeBob = buck.balanceOf(BOB);
        uint256 beforeIssued = buck.totalIssued();
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(BOB);
        buck.burn(10e6);
        assertEq(buck.balanceOf(BOB), beforeBob);
        assertEq(buck.totalIssued(), beforeIssued);
        vm.roll(block.number + 1);
        vm.prank(BOB);
        buck.burn(10e6);
        assertGe(beforeBob - buck.balanceOf(BOB), 10e6);
    }

    function test_burnCanExceedTheTargetToPreserveTheKShortfallRule() public {
        _credit(BOB, 1000, 0);
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 5, 0, BuckCredit.DepreciationType.LINEAR,
                                         2000, uint48(t0), 0);
        vm.warp(t0 + YEAR);
        k.setK(0.8e18);
        vm.prank(ALICE);
        buck.issue(id, 5, 3, 0, address(0));
        vm.prank(ALICE);
        buck.burn(2);
        assertEq(buck.balanceOf(ALICE), 0); // three, not two, raw units were needed
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 0);
    }

    function test_burnWorkLimitRevertsWithoutPretendingTheFacilityCannotClose() public {
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 138648, 0, BuckCredit.DepreciationType.LINEAR,
                                         10000, uint48(t0), 0);
        uint256 elapsed = (113889 * YEAR + 138648 - 1) / 138648;
        vm.warp(t0 + elapsed);
        assertEq(credit.depreciatedFaceValue(id), 24759);
        k.setK(0.4999e18);
        vm.prank(ALICE);
        buck.issue(id, 38527, 3438, 0, address(0));
        vm.expectRevert(BuckNet.AllocationWorkLimit.selector);
        vm.prank(ALICE);
        buck.burn(2779);
        assertEq(buck.balanceOf(ALICE), 3438);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, 3438);
        _authorize(ALICE, id, 3438, 0);
        vm.prank(ALICE);
        fund.reduce(id, 3438);
        assertEq(buck.balanceOf(ALICE), 0);
    }

    function test_maturedFacilityCanStillReleaseItsLienAfterAnOrdinaryBurn() public {
        uint256 id = _credit(ALICE, 100e6, 0);
        vm.warp(t0 + 50 * YEAR);
        buck.processMaturities(1);
        vm.prank(ALICE);
        buck.burn(1e6);
        uint256 beforeClose = buck.balanceOf(ALICE);
        (uint256 remaining,) = credit.issuanceOf(id);
        assertGt(remaining, 0);
        vm.prank(ALICE);
        assertEq(fund.closeMatured(id), remaining);
        assertEq(buck.balanceOf(ALICE), beforeClose);
        assertEq(buck.claimBook().deferredPrincipal(), remaining);
        vm.expectRevert();
        credit.ownerOf(id);
    }
}
