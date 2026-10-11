// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityTest, IBasketUpkeep, EqToken} from "./BuckBasketEquity.t.sol";
import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {ControlledPairIntent} from "./BasketUpkeepFacet.t.sol";
import {BasketVenueFacet} from "../../src/basket/BasketVenueFacet.sol";
import {BasketReference} from "../../src/basket/BasketReference.sol";
import {OuterBandDeskPolicy} from "../../src/basket/OuterBandDeskPolicy.sol";
import {IBuckBasketVenue} from "../../src/basket/IBuckBasketVenue.sol";
import {BuckKControllerDirect} from "../../src/BuckKControllerDirect.sol";

contract BasketMachineTest is BuckBasketEquityTest {
    BasketReference internal signals;

    function _assertReferenceNormalization() internal view {
        uint256 sum;
        for (uint256 i; i < b.constituentsLength(); ++i) {
            (,, uint256 amount, uint256 initialPrice,,,,,, uint256 weight,) = b.constituents(i);
            uint256 value = amount * initialPrice / 1e18;
            assertApproxEqAbs(value, weight * 1e14, 1, "quantity matches rounded declared weight");
            sum += value;
        }
        assertApproxEqAbs(sum, 1e18, b.constituentsLength(), "reference at its anchor prices is one BUCK");
    }

    function test_machine_referenceWeightsDoNotAddACommonModeOffset() public view {
        _assertReferenceNormalization();
    }

    function test_machine_addingAConstituentPreservesOriginalPriceAnchors() public {
        _push(0, true, 50_000 * B);
        EqToken fourth = new EqToken("Fourth", "D", 18);
        vm.prank(GOV);
        b.addBasketToken(address(fourth), 18, 4 * B, 1750, 3000);
        (,,, uint256 initialPrice,,,,,,,) = b.constituents(0);
        assertEq(initialPrice, price[0], "current market price is not a new anchor");
        _assertReferenceNormalization();
    }

    // The upkeep facet under a policy that wants no transfer: what is left
    // is the safeguard plan and the placement of TOKEN already held.
    function _replace(bool withSignal) internal {
        ControlledPairIntent intent = new ControlledPairIntent(address(b));
        intent.setRate(0);
        vm.startPrank(GOV);
        b.setVenue(address(new BasketVenueFacet()));
        b.setEquityWheel(address(new BasketUpkeepFacet(address(intent))));
        vm.stopPrank();
        if (withSignal) {
            signals = new BasketReference(address(b), 1800, 3600, 500);
            signals.observe();
        }
    }

    function _response() internal returns (uint256) {
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1);
        if (address(signals) != address(0)) signals.observe();
        return IBasketUpkeep(address(b)).wheelStep(2, 0);
    }

    function _freshMark() internal view {
        int256 net = int256(b.grossAt(2)) + buck.signedBalanceOf(address(b)) + int256(buck.reliefOf(address(b)));
        assertEq(b.markNow(), net > 0 ? uint256(net) : 0, "post-trade mark matches the new book");
        for (uint256 i; i < 3; ++i) assertGe(tok[i].balanceOf(address(b)), b.idleToken(i), "wallet accounted");
    }

    function test_machine_cashOnlyDepositBootstrapsEmptyLegs() public {
        _replace(false);
        _deposit(alice, address(buck), 300_000 * B);
        for (uint256 j; j < 20; ++j) { _response(); for (uint256 i; i < 3; ++i) _repin(i); }
        for (uint256 i; i < 3; ++i) assertGt(b.liquidityOf(i), 0, "an empty leg is reachable");
        _response(); _freshMark();
    }

    function test_machine_waitingDepositDoesNotBlockOtherLegs() public {
        _replace(false);
        _deposit(alice, address(tok[0]), 100_000e18);
        uint256 idle = b.idleToken(0);
        assertEq(_response(), 1);
        assertLt(b.idleToken(0), idle, "sell idle first");
        assertGt(b.liquidityOf(1) + b.liquidityOf(2), 0, "proceeds buy an empty underweight leg");
        assertEq(b.liquidityOf(0), 0, "arrival pool is not privileged");
        _freshMark();
    }

    function test_machine_balancedIdleCanBecomeLpWithoutASecondSwap() public {
        _replace(false);
        for (uint256 i; i < 3; ++i) _deposit(alice, address(tok[i]), 30_000e18 / (i + 1));
        uint256[] memory weights = b.responseWeightsBp();
        assertEq(weights[0] + weights[1] + weights[2], 10000);
        assertEq(_response(), 1);
        assertGt(b.liquidityOf(0) + b.liquidityOf(1) + b.liquidityOf(2), 0);
        _freshMark();
    }

    function test_machine_underwaterResponseNeverDrawsNewDebt() public {
        _placed(); _replace(false);
        ctrl.setK(0);
        int256 account = buck.signedBalanceOf(address(b));
        assertLt(account, 0);
        assertEq(_response(), 1);
        assertGt(buck.signedBalanceOf(address(b)), account, "cash need reduces balanced book's debt");
        _freshMark();
    }

    function test_machine_responseIsPacedAndOldKindsAreRetired() public {
        _replace(false); _deposit(alice, address(tok[0]), 100_000e18);
        assertEq(_response(), 1);
        assertEq(IBasketUpkeep(address(b)).wheelStep(2, 0), 0);
        assertFalse(IBasketUpkeep(address(b)).wheelDue(2, 0));
        assertEq(IBasketUpkeep(address(b)).wheelStep(3, 0), 0);
        assertEq(IBasketUpkeep(address(b)).wheelStep(4, 0), 0);
    }

    function test_machine_cashNeedSellsTheMostOverweightConstituent() public {
        _replace(false);
        _deposit(alice, address(tok[0]), 30_000e18);
        _deposit(alice, address(tok[1]), 18_000e18);
        _deposit(alice, address(tok[2]), 10_000e18);
        ctrl.setK(0);
        uint256 first = b.idleToken(0); uint256 heavy = b.idleToken(1); uint256 last = b.idleToken(2);
        assertEq(_response(), 1);
        assertLt(b.idleToken(1), heavy, "the constituent furthest above its weight supplies the cash");
        assertEq(b.idleToken(0), first); assertEq(b.idleToken(2), last);
        assertEq(b.liquidityOf(0) + b.liquidityOf(1) + b.liquidityOf(2), 0, "nothing is bought while cash is needed");
        _freshMark();
    }

    function test_machine_signalDoesNotInventSamplesAcrossGap() public {
        _replace(true);
        assertTrue(signals.fresh());
        assertApproxEqAbs(signals.rawValue(), 1e18, 0.001e18);
        vm.warp(vm.getBlockTimestamp() + 8 days); vm.roll(vm.getBlockNumber() + 1);
        signals.observe();
        assertEq(signals.consecutiveDays(), 1, "a missed week starts the day count again");
        vm.warp(vm.getBlockTimestamp() + 1 days); vm.roll(vm.getBlockNumber() + 1);
        signals.observe();
        assertEq(signals.consecutiveDays(), 2);
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1);
        signals.observe();
        assertEq(signals.consecutiveDays(), 2, "a second sample in a day does not count twice");
    }

    function test_machine_signalInvalidatesOnManipulatedSpot() public {
        _replace(true);
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1);
        _push(0, true, 200_000 * B);
        signals.observe();
        assertFalse(signals.fresh());
        assertFalse(signals.valid());
        assertEq(signals.consecutiveDays(), 0);
    }

    function test_machine_missingHistoryNeverFallsBackToSpot() public {
        _replace(true);
        int256 oldRaw = signals.rawValue();
        uint64 sampled = signals.lastSampleAt();
        vm.mockCallRevert(pools[1], abi.encodeWithSelector(bytes4(keccak256("observe(uint32[])"))), bytes("no history"));
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1);
        signals.observe();
        assertFalse(signals.fresh());
        assertEq(signals.rawValue(), oldRaw, "no partly refreshed publication");
        assertEq(signals.lastSampleAt(), sampled, "failed read does not freshen the old price");
        vm.expectRevert(); IBuckBasketVenue(address(b)).basketValueInBuck();
    }

    function test_machine_policyWillNotExecuteOldDirectionAfterSpotReverts() public {
        _replace(true);
        OuterBandDeskPolicy policy = new OuterBandDeskPolicy(address(signals), 300, 200, 2500, 40, 3);
        for (uint256 i; i < 3; ++i) _push(i, true, 25_000 * B);
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1); policy.observe();
        (int32 e,) = policy.monetaryEffort(); assertLt(e, 0);
        for (uint256 i; i < 3; ++i) _repin(i);
        (e,) = policy.monetaryEffort();
        assertEq(e, 0, "live spot vetoes lagged advice");
    }

    function test_machine_outerBandLeavesPersistentInnerErrorForK() public {
        _replace(true);
        // All prices about 1% higher: beyond parity, inside the desk's 3% entry.
        for (uint256 i; i < 3; ++i) _push(i, true, 5_000 * B);
        _age();
        OuterBandDeskPolicy policy = new OuterBandDeskPolicy(address(signals), 300, 200, 2500, 40, 3);
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1); policy.observe();
        (int32 effort,) = policy.monetaryEffort();
        assertEq(effort, 0, "desk leaves the inner error visible");
        BuckKControllerDirect k = new BuckKControllerDirect(1e7, 100, 0, 1800, 0, 0.95e18, 0.75e18, address(this));
        k.setBasket(address(signals));
        vm.warp(vm.getBlockTimestamp() + 1800);
        k.compute();
        assertLt(k.P(), 0, "raw inflation error");
        assertLt(k.I(), 0, "integral counters persistent inflation");
    }

    function test_machine_policyReversalAndGapResetPersistence() public {
        _replace(true);
        OuterBandDeskPolicy policy = new OuterBandDeskPolicy(address(signals), 300, 200, 2500, 40, 3);
        for (uint256 i; i < 3; ++i) _push(i, true, 25_000 * B);
        _age();
        for (uint256 day; day < 3; ++day) {
            vm.warp(vm.getBlockTimestamp() + 1 days); vm.roll(vm.getBlockNumber() + 1); policy.observe();
        }
        (int32 e, bool outright) = policy.monetaryEffort();
        assertLt(e, 0); assertTrue(outright);
        uint256 daysSeen = policy.overDays(); policy.observe(); assertEq(policy.overDays(), daysSeen);
        vm.warp(vm.getBlockTimestamp() + 3 days); vm.roll(vm.getBlockNumber() + 1); policy.observe();
        (, outright) = policy.monetaryEffort(); assertFalse(outright); assertEq(policy.overDays(), 1);
        for (uint256 i; i < 3; ++i) { _repin(i); _push(i, false, 30_000e18 / (i + 1)); }
        _age(); vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1); policy.observe();
        (e, outright) = policy.monetaryEffort(); assertGt(e, 0); assertFalse(outright); assertEq(policy.overDays(), 1);
    }

    function test_machine_policyCannotReusePersistenceAfterIndependentSensorRecovery() public {
        _replace(true);
        OuterBandDeskPolicy policy = new OuterBandDeskPolicy(address(signals), 300, 200, 2500, 40, 3);
        for (uint256 i; i < 3; ++i) _push(i, true, 25_000 * B);
        for (uint256 day; day < 3; ++day) {
            vm.warp(vm.getBlockTimestamp() + 1 days); vm.roll(vm.getBlockNumber() + 1); policy.observe();
        }
        (, bool outright) = policy.monetaryEffort(); assertTrue(outright);
        // Another caller samples the shared service while the adapter is
        // idle. A failed read and recovery cannot revive its old persistence.
        vm.mockCallRevert(pools[1], abi.encodeWithSelector(bytes4(keccak256("observe(uint32[])"))), bytes("no history"));
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1); signals.observe();
        assertFalse(signals.fresh());
        vm.clearMockedCalls();
        vm.roll(vm.getBlockNumber() + 1); signals.observe();
        assertTrue(signals.fresh()); assertEq(signals.consecutiveDays(), 1);
        (, outright) = policy.monetaryEffort(); assertFalse(outright);
        policy.observe(); policy.observe();
        assertEq(policy.overDays(), 1, "repeated same-timestamp calls do not invent days");
    }
}
