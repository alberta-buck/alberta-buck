// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BasketReference} from "../../src/basket/BasketReference.sol";

contract SignalHistoryBasket {
    address public governance = address(0xA0);
    uint256 public constituentsLength = 3;
    function grow() external { ++constituentsLength; }
}

contract BasketSignalHistoryTest is Test {
    address constant GOV = address(0xA0);
    SignalHistoryBasket basket;
    BasketReference signals;

    function setUp() public {
        vm.warp(100 days);
        basket = new SignalHistoryBasket();
        signals = new BasketReference(address(basket), 1800, 3600, 500);
        _prices();
    }

    function _prices() private {
        int256[] memory ticks = new int256[](3);
        ticks[0] = 1000e18; ticks[1] = -500e18; ticks[2] = -500e18;
        vm.mockCall(address(signals), abi.encodeWithSelector(signals.readPrices.selector), abi.encode(int256(1.1e18), ticks));
    }

    function _seed() private {
        int256[7][] memory levels = new int256[7][](3);
        int256[7] memory common;
        vm.prank(GOV);
        signals.initializeHistory(levels, common, 99, 1280, keccak256("pre-run history"));
    }

    function test_historyIsNotAFreshPriceOrPersistentPolicyObservation() public {
        _seed();
        assertTrue(signals.historyReady());
        assertFalse(signals.fresh());
        assertEq(signals.lastSampleAt(), 0);
        assertEq(signals.consecutiveDays(), 0);
        signals.observe();
        assertTrue(signals.fresh());
        assertEq(signals.consecutiveDays(), 1);
        assertGt(signals.excursion(0), 990e18, "first price is compared with history, not made its own anchor");
    }

    function test_noFirstPriceAnchorWithoutHistory() public {
        signals.observe();
        assertTrue(signals.fresh(), "raw reference remains available");
        assertFalse(signals.historyReady());
        assertEq(signals.excursion(0), 0, "unknown trend is not fabricated");
        assertEq(signals.votes(0), 0);
    }

    function test_historyCannotBeOverwrittenOrInjectedAfterLivePublication() public {
        int256[7][] memory levels = new int256[7][](3);
        int256[7] memory common;
        vm.expectRevert(BasketReference.NotGovernance.selector);
        signals.initializeHistory(levels, common, 99, 1280, bytes32(uint256(1)));
        _seed();
        vm.prank(GOV); vm.expectRevert(BasketReference.InvalidHistory.selector);
        signals.initializeHistory(levels, common, 99, 1280, bytes32(uint256(1)));
        BasketReference live = new BasketReference(address(basket), 1800, 3600, 500);
        vm.mockCall(address(live), abi.encodeWithSelector(live.readPrices.selector), abi.encode(int256(1e18), new int256[](3)));
        live.observe();
        vm.prank(GOV); vm.expectRevert(BasketReference.InvalidHistory.selector);
        live.initializeHistory(levels, common, 99, 1280, bytes32(uint256(1)));
    }

    function test_futureAndIncompleteHistoryAreRejected() public {
        int256[7][] memory levels = new int256[7][](3);
        int256[7] memory common;
        vm.prank(GOV); vm.expectRevert(BasketReference.InvalidHistory.selector);
        signals.initializeHistory(levels, common, 100, 1280, bytes32(uint256(1)));
        vm.prank(GOV); vm.expectRevert(BasketReference.InvalidHistory.selector);
        signals.initializeHistory(new int256[7][](2), common, 99, 1280, bytes32(uint256(1)));
    }

    function test_rejectedBootstrapSampleCannotReplaceHistoricalAnchor() public {
        _seed();
        vm.mockCallRevert(address(signals), abi.encodeWithSelector(signals.readPrices.selector), bytes("out of band"));
        signals.observe();
        assertFalse(signals.valid());
        assertTrue(signals.historyReady());
        vm.clearMockedCalls(); _prices();
        vm.roll(vm.getBlockNumber() + 1);
        signals.observe();
        assertGt(signals.excursion(0), 990e18);
    }

    function test_gapUpdatesOnceAndClearsTurnEvidenceWithoutReplacingAnchor() public {
        _seed(); signals.observe();
        (int256[7] memory before,) = signals.ladder(0);
        vm.warp(vm.getBlockTimestamp() + 8 days); vm.roll(vm.getBlockNumber() + 1);
        signals.observe();
        (int256[7] memory afterLevels, int256[7] memory velocities) = signals.ladder(0);
        for (uint256 k; k < 7; ++k) {
            assertEq(afterLevels[k], before[k] + (1000e18 - before[k]) * 2 / int256(uint256(signals.windows(k)) + 1));
            assertEq(velocities[k], 0);
        }
        assertEq(signals.consecutiveDays(), 1);
        assertGt(signals.excursion(0), 990e18);
    }

    function test_sameDayRecoveryCannotDoubleAdvanceFilters() public {
        _seed(); signals.observe();
        (int256[7] memory before,) = signals.ladder(0);
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1);
        vm.mockCallRevert(address(signals), abi.encodeWithSelector(signals.readPrices.selector), bytes("out of band"));
        signals.observe();
        vm.clearMockedCalls(); _prices(); vm.roll(vm.getBlockNumber() + 1);
        signals.observe();
        (int256[7] memory afterLevels, int256[7] memory velocities) = signals.ladder(0);
        for (uint256 k; k < 7; ++k) {
            assertEq(afterLevels[k], before[k]);
            assertEq(velocities[k], 0);
        }
    }

    function test_changedConstituentsCannotReuseAnOldHistoricalVector() public {
        _seed(); signals.observe(); basket.grow();
        assertFalse(signals.historyReady());
        assertFalse(signals.fresh());
        assertEq(signals.excursion(0), 0);
    }
}
