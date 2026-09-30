// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityTest} from "./BuckBasketEquity.t.sol";
import {EquityTurnDirector} from "../../src/basket/EquityTurnDirector.sol";

/// @title The turn director on the equity basket (the Python prototype's
///        test_the_turn_director_*): let a run run, sell the turn, buy no
///        falling knife; and the lean.
contract EquityTurnDirectorTest is BuckBasketEquityTest {
    EquityTurnDirector internal dir;

    /// @dev The outside market moves pool i by `ppm` parts per million of
    ///      price (BUCK per TOKEN), and the pool follows.
    function _move(uint256 i, int256 ppm) internal {
        (,,,,,,,, bool b0,,) = b.constituents(i);
        uint256 f = _sqrt18(uint256(int256(1e18) + ppm * 1e12));   // sqrt of the factor, 1e9
        uint256 r = uint256(ref[i]);
        ref[i] = uint160(b0 ? r * 1e9 / f : r * f / 1e9);
        _repin(i);
    }

    function _withDirector() internal {
        dir = new EquityTurnDirector(address(b), GOV);
        vm.prank(GOV);
        b.setEquityDirector(address(dir));
    }

    /// @dev Day by day, T0 +1% a day for `up` days then -1% a day for `down`;
    ///      return the first day T0's liquidity fell (a trim), or type(max).
    function _firstTrim(uint256 up, uint256 down) internal returns (uint256) {
        for (uint256 d = 0; d < up + down; d++) {
            uint128 l0 = b.liquidityOf(0);
            _move(0, d < up ? int256(10_000) : int256(-10_000));
            _wheel(1, true);
            if (b.liquidityOf(0) < l0) return d;
        }
        return type(uint256).max;
    }

    function test_theBandSellsIntoARun() public {
        _placed();
        uint256 day = _firstTrim(40, 0);
        assertLt(day, 40, "the band trims a leg still running up");
    }

    function test_theDirectorLetsARunRunAndSellsTheTurn() public {
        _withDirector();
        _placed();
        _wheel(5, true);                                   // a few samples first
        uint256 day = _firstTrim(40, 40);
        assertGe(day, 40, "no trim while it runs up");
        assertLt(day, 80, "a trim after the turn");
    }

    function test_theDirectorLeansAgainstTheExcursion() public {
        _withDirector();
        _placed();
        _wheel(5, true);
        for (uint256 d = 0; d < 30; d++) { _move(0, 10_000); _wheel(1, true); }
        assertGt(dir.excursion(0), 0, "T0 rich against its anchor");
        uint256[] memory tg = dir.targetsBp();
        assertLt(tg[0], 3333, "its target leans down");
        assertGt(tg[1], 3333);
        assertEq(tg[0] + tg[1] + tg[2] <= 10000 && tg[0] + tg[1] + tg[2] >= 9997, true);
    }

    // ---- catch-up: a gap between samples ----------------------------------- //

    function _nextDay() internal {
        vm.warp(block.timestamp + 1 days);
        vm.roll(block.number + 1);
    }

    /// @dev Two directors on one basket share ten daily samples while T0
    ///      drifts; T0 then moves by `ppm` and holds.  `daily` samples each of
    ///      the next `n` days, `lazy` only the last: under sample-and-hold the
    ///      lazy one must land where the daily one did, window by window.
    function _twins(uint256 n, int256 ppm) internal {
        _placed();
        EquityTurnDirector daily = new EquityTurnDirector(address(b), GOV);
        EquityTurnDirector lazy  = new EquityTurnDirector(address(b), GOV);
        for (uint256 d = 0; d < 10; d++) {
            _move(0, 10_000);
            _nextDay();
            daily.observe();
            lazy.observe();
        }
        _move(0, ppm);
        for (uint256 d = 0; d < n; d++) { _nextDay(); daily.observe(); }
        lazy.observe();
        for (uint256 i = 0; i < 3; i++) {
            (int256[7] memory eD, int256[7] memory vD) = daily.ladder(i);
            (int256[7] memory eL, int256[7] memory vL) = lazy.ladder(i);
            for (uint256 k = 0; k < 7; k++) {
                assertApproxEqAbs(eL[k], eD[k], 1e9, "the EMA lands where daily samples left it");
                assertApproxEqAbs(vL[k], vD[k], 1e9, "and so does its last step");
            }
        }
    }

    /// Regression (2026-09-30): a sample after a gap advanced each EMA by one
    /// day's step, so a quiet week under-advanced the ladder -- the 5-day EMA
    /// moved 33% of the way where seven days move it 94%.
    function test_theDirectorCatchesUpAfterAGap() public {
        _twins(7, 50_000);
    }

    /// forge-config: default.fuzz.runs = 24
    function testFuzz_theDirectorCatchesUpAfterAnyGap(uint16 n, int32 ppm) public {
        _twins(bound(n, 1, 200), bound(ppm, -200_000, 200_000));
    }

    function test_theDirectorBuysNoFallingKnife() public {
        _withDirector();
        _placed();
        _wheel(5, true);
        for (uint256 d = 0; d < 20; d++) { _move(0, -10_000); _wheel(1, true); }
        uint128 l0 = b.liquidityOf(0);
        _deposit(bob, address(buck), 100_000 * B);         // new money to place
        for (uint256 d = 0; d < 20; d++) { _move(0, -10_000); _wheel(1, true); }
        assertLe(b.liquidityOf(0), l0 + l0 / 200, "nothing bought while it falls");
        assertGt(b.liquidityOf(1), 0);
    }
}
