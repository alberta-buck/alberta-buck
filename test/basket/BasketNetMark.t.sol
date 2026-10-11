// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquityTest, IBasketUpkeep} from "./BuckBasketEquity.t.sol";
import {ControlledPairIntent} from "./BasketUpkeepFacet.t.sol";
import {BasketVenueFacet} from "../../src/basket/BasketVenueFacet.sol";
import {BasketUpkeepNetTrial} from "../harness/BasketUpkeepNetTrial.sol";

/// A parity reference the test sets: the basket's cost in BUCK, and whether it is fresh.
contract ParityStub {
    int256 public rawValue = 1e18;
    bool public fresh = true;
    function set(int256 value, bool fresh_) external { rawValue = value; fresh = fresh_; }
}

/// The shell's net mark, with the trial upkeep written for it: K bounds the
/// BUCKs the basket has let out, not those it still holds beside its TOKEN.
contract BasketNetMarkTest is BuckBasketEquityTest {
    ControlledPairIntent internal intent;
    ParityStub internal parity;

    function _net(uint256 parityGain) internal {
        intent = new ControlledPairIntent(address(b));
        intent.setRate(0);
        parity = new ParityStub();
        vm.startPrank(GOV);
        b.setVenue(address(new BasketVenueFacet()));
        b.setEquityWheel(address(new BasketUpkeepNetTrial(address(intent), address(parity), parityGain)));
        b.setNetMark(true);
        vm.stopPrank();
    }

    function _response() internal returns (uint256) {
        vm.warp(vm.getBlockTimestamp() + 1800); vm.roll(vm.getBlockNumber() + 1);
        return IBasketUpkeep(address(b)).wheelStep(2, 0);
    }

    function _balanced() internal returns (uint256[3] memory ids) {
        for (uint256 i; i < 3; ++i) ids[i] = _deposit(alice, address(tok[i]), 30_000e18 / (i + 1));
    }

    function _positions() internal view returns (uint256 sum) {
        for (uint256 i; i < 3; ++i) sum += b.liquidityOf(i);
    }

    function test_net_onlyGovernanceSetsTheMark() public {
        vm.expectRevert();
        b.setNetMark(true);
        assertFalse(b.netMark());
    }

    function test_net_everyDepositIsPairedAtOnceAndTheLineIsWhole() public {
        _net(0);
        _balanced();
        assertEq(_response(), 1);
        for (uint256 i; i < 3; ++i) {
            assertGt(b.liquidityOf(i), 0, "placed");
            assertLt(b.idleToken(i) * price[i] / 1e18, b.equity() / 1000, "nothing left waiting");
        }
        // A BUCK was drawn for every BUCK of TOKEN, far beyond K x equity ...
        assertGt(_lien(), b.equity() * 9 / 10, "the whole book is paired");
        // ... and all of K x equity is still there to spend.
        assertApproxEqRel(b.liquidity(), ctrl.currentBuckK() * b.equity() / 1e18, 0.03e18, "the line is whole");
        assertEq(_response(), 0, "a paired book at its targets has nothing to do");
    }

    function test_net_aLargeExitIsPaidInBucksAndUnwoundAfterwards() public {
        _net(0);
        uint256[3] memory ids = _balanced();
        _response();
        uint256 positions = _positions();
        uint256 lien = _lien();
        uint256 held = buck.balanceOf(alice);
        // A third of the book leaves in one go.
        vm.prank(alice);
        uint256 paid = b.redeem(ids[0], 10000);
        assertEq(buck.balanceOf(alice) - held, paid, "paid in BUCK");
        assertEq(_positions(), positions, "no position was sold to pay it");
        assertEq(_lien(), lien + paid, "it was issued");
        // That is more than half of K x what stays, so the next turns sell it down, every constituent giving.
        uint128[3] memory before = [b.liquidityOf(0), b.liquidityOf(1), b.liquidityOf(2)];
        assertEq(_response(), 1);
        for (uint256 i; i < 3; ++i) assertLt(b.liquidityOf(i), before[i], "each gives its share");
        assertLt(_lien(), lien + paid);
    }

    function test_net_theBookLeansAgainstParity() public {
        _net(5e18);
        _balanced();
        _response();
        intent.setRate(1e18);
        uint256 positions = _positions();
        // The basket costs 1.02 BUCKs: the BUCK is cheap, so hold some. TOKEN is sold for it.
        parity.set(1.02e18, true);
        assertEq(_response(), 1);
        assertLt(_positions(), positions, "TOKEN sold for BUCKs");
        // It costs 0.98: the BUCK is dear, so issue some. TOKEN is bought with it.
        parity.set(0.98e18, true);
        for (uint256 i; i < 3; ++i) _repin(i);
        positions = _positions();
        assertEq(_response(), 1);
        assertGt(_positions(), positions, "TOKEN bought with issued BUCKs");
        // A reference that is not fresh leans nothing: the book stays as it is.
        parity.set(0.5e18, false);
        for (uint256 i; i < 3; ++i) _repin(i);
        positions = _positions();
        _response();
        assertApproxEqRel(_positions(), positions, 0.02e18);
    }
}
