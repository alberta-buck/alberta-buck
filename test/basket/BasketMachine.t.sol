// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityTest, IBasketUpkeep, EqToken, IPool} from "./BuckBasketEquity.t.sol";
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

    // The upkeep facet under a policy that asks for nothing: what is left is
    // pairing what waits, the leash and the ceiling.
    function _replace(bool withSignal) internal {
        ControlledPairIntent intent = new ControlledPairIntent(address(b));
        intent.setRate(0);
        vm.startPrank(GOV);
        b.setVenue(address(new BasketVenueFacet()));
        vm.stopPrank();
        if (withSignal) {
            signals = new BasketReference(address(b), 1800, 3600, 500);
            signals.observe();
        }
        vm.startPrank(GOV);
        b.setEquityWheel(address(new BasketUpkeepFacet(address(intent), address(signals), 0)));
        b.setNetMark(true);
        vm.stopPrank();
    }

    function _response() internal returns (uint256) {
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1);
        if (address(signals) != address(0)) signals.observe();
        return IBasketUpkeep(address(b)).wheelStep(2, 0);
    }

    // The credit is marked net: at the equity, plus the positions' BUCK side over K.
    function _freshMark() internal view {
        int256 net = int256(b.grossAt(2)) + buck.signedBalanceOf(address(b)) + int256(buck.reliefOf(address(b)));
        uint256 held;
        for (uint256 i; i < 3; ++i) held += IBuckBasketVenue(address(b)).marks(i, b.liquidityOf(i)).posLow / 2;
        uint256 mark = (net > 0 ? uint256(net) : 0) + (ctrl.k() == 0 ? 0 : held * 1e18 / ctrl.k());
        assertEq(b.markNow(), mark, "post-trade mark matches the new book");
        for (uint256 i; i < 3; ++i) assertGe(tok[i].balanceOf(address(b)), b.idleToken(i), "wallet accounted");
    }

    function test_machine_cashOnlyDepositBootstrapsEmptyLegs() public {
        _replace(false);
        _deposit(alice, address(buck), 300_000 * B);
        for (uint256 j; j < 20; ++j) { _response(); for (uint256 i; i < 3; ++i) _repin(i); }
        for (uint256 i; i < 3; ++i) assertGt(b.liquidityOf(i), 0, "a holding of nothing is far beyond its leash, and is bought");
        _response(); _freshMark();
    }

    function test_machine_aLoneDepositIsPairedAndTheBookBroughtTowardItsWeights() public {
        _replace(false);
        _deposit(alice, address(tok[0]), 100_000e18);
        assertEq(_response(), 1);
        assertGt(b.liquidityOf(0), 0, "what arrived is paired where it arrived");
        assertLt(b.idleToken(0), 100e18, "and none of it waits");
        assertGt(b.liquidityOf(1), 0, "the empty legs, beyond their leashes, are bought on the line");
        assertGt(b.liquidityOf(2), 0);
        _freshMark();
    }

    function test_machine_balancedDepositsArePairedWithNoSwap() public {
        _replace(false);
        for (uint256 i; i < 3; ++i) _deposit(alice, address(tok[i]), 30_000e18 / (i + 1));
        uint256[] memory weights = b.responseWeightsBp();
        assertEq(weights[0] + weights[1] + weights[2], 10000);
        assertEq(_response(), 1);
        for (uint256 i; i < 3; ++i) {
            assertGt(b.liquidityOf(i), 0);
            (uint160 spot,,,,,,) = IPool(pools[i]).slot0();
            assertEq(spot, ref[i], "no price moved");
        }
        _freshMark();
    }

    function test_machine_underwaterResponseNeverDrawsNewDebt() public {
        _placed(); _replace(false);
        ctrl.setK(0);
        uint256 lien = _lien();
        assertGt(lien, 0);
        _response();
        assertLe(_lien(), lien, "with no K nothing more is issued");
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
