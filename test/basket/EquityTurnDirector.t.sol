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

    function test_theDirectorBuysNoFallingKnife() public {
        _withDirector();
        _placed();
        _wheel(5, true);
        for (uint256 d = 0; d < 20; d++) { _move(0, -10_000); _wheel(1, true); }
        uint128 l0 = b.liquidityOf(0);
        _deposit(bob, address(buck), 100_000e18);          // new money to place
        for (uint256 d = 0; d < 20; d++) { _move(0, -10_000); _wheel(1, true); }
        assertLe(b.liquidityOf(0), l0 + l0 / 200, "nothing bought while it falls");
        assertGt(b.liquidityOf(1), 0);
    }
}
