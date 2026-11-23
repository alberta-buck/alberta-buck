// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {PairSignalsTest} from "./PairSignals.t.sol";
import {PairBookView} from "./PairRebalancePolicy.t.sol";
import {BasketUpkeepSignalSizing} from "../harness/BasketUpkeepSignalSizing.sol";
import {PairRebalancePolicy} from "../../src/basket/PairRebalancePolicy.sol";

contract SignalSizingProbe is BasketUpkeepSignalSizing {
    constructor(address policy, SignalMode mode, uint256 rate)
        BasketUpkeepSignalSizing(policy, address(0), 0, 0, mode, rate, 0.02e18, 0.005e18)
    {
        constituents.push().targetWeightBp = 3334;
        constituents.push().targetWeightBp = 3333;
        constituents.push().targetWeightBp = 3333;
    }

    function rateFor(uint256 i, bool sold, uint256 gap) external view returns (uint256) {
        return _rate(i, sold, constituents.length, gap);
    }

    function pairShareFor(uint256 i, uint256 rate) external view returns (uint256) {
        return _pairShare(i, rate);
    }
}

contract BasketUpkeepSignalSizingTest is PairSignalsTest {
    function _policy() private returns (PairRebalancePolicy) {
        int128[] memory anchors = new int128[](3);
        return new PairRebalancePolicy(
            address(s), address(new PairBookView(s)), anchors,
            PairRebalancePolicy.Sizing(0, 0, 0, 0, 0.7e18, 1e18, 0.0025e18, 0.02e18)
        );
    }

    function test_signalSizing_excursionRateIsIndependentOfTheObserverAndCapsAtFullStrength() public {
        PairRebalancePolicy p = _policy();
        SignalSizingProbe excursion = new SignalSizingProbe(
            address(p), BasketUpkeepSignalSizing.SignalMode.Excursion, 0.6e18
        );
        SignalSizingProbe slower = new SignalSizingProbe(
            address(p), BasketUpkeepSignalSizing.SignalMode.Excursion, 0.3e18
        );
        assertEq(s.readyMask(), 0);
        assertEq(excursion.rateFor(0, true, 0), 0);
        assertEq(excursion.rateFor(0, true, 0.005e18), 0.15e18);
        assertEq(excursion.rateFor(0, false, 0.01e18), 0.3e18);
        assertEq(excursion.rateFor(0, true, 0.03e18), 0.6e18);
        assertEq(slower.rateFor(0, true, 0.03e18), 0.3e18);
        assertEq(p.maxRate(), 0.7e18, "discretionary tuning leaves the mandatory pace fixed");
        vm.expectRevert(bytes("signal rate exceeds maxRate"));
        new SignalSizingProbe(address(p), BasketUpkeepSignalSizing.SignalMode.Excursion, 0.8e18);
    }

    function test_signalSizing_velocityRequiresAReversalAndBothAlsoScalesByExcursion() public {
        PairRebalancePolicy p = _policy();
        SignalSizingProbe velocity = new SignalSizingProbe(
            address(p), BasketUpkeepSignalSizing.SignalMode.Velocity, 0.6e18
        );
        SignalSizingProbe both = new SignalSizingProbe(address(p), BasketUpkeepSignalSizing.SignalMode.Both, 0.6e18);
        setTicks(3000, 0, 0, 0);
        p.observe();
        assertEq(velocity.rateFor(0, true, 0.02e18), 0, "unready");
        warm();
        p.observe();
        assertEq(both.rateFor(0, true, 0.02e18), 0, "quiet");
        for (uint256 k = 1; k <= 40; ++k) {
            setTicks(3000 - int24(int256(k)), 0, 0, 0);
            step(3600);
            p.observe();
        }
        uint256 closing = velocity.closingVelocity(0, 1);
        assertGt(closing, 0);
        assertLt(closing, velocity.signalVelocity());
        uint256 release = velocity.rateFor(0, true, 0.01e18);
        assertGt(release, 0);
        assertLt(release, velocity.signalRate());
        assertEq(velocity.rateFor(0, false, 0.01e18), 0, "only the dear TOKEN sells");
        assertEq(velocity.rateFor(0, true, 0.02e18), release, "velocity ignores gap in its rate");
        assertApproxEqAbs(both.rateFor(0, true, 0.01e18), release / 2, 1);
        assertEq(both.rateFor(0, true, 0.02e18), release);

        vm.prank(p.basket());
        p.recordImpact(0, 0, 4000);
        assertEq(velocity.closingVelocity(0, 1), 0, "own impact can absorb the measured closure");
        assertEq(both.rateFor(0, true, 0.02e18), 0);
    }

    function test_signalSizing_zeroEvidenceCannotSpendAccruedAllowance() public {
        PairRebalancePolicy p = _policy();
        SignalSizingProbe velocity = new SignalSizingProbe(
            address(p), BasketUpkeepSignalSizing.SignalMode.Velocity, 0.6e18
        );
        uint256[] memory rates = new uint256[](3);
        rates[0] = p.maxRate();
        vm.warp(vm.getBlockTimestamp() + 1 days);
        vm.prank(p.basket());
        p.recordRates(rates);
        assertGt(p.share(0, 0), 0, "an earlier book resize can leave accrued allowance");
        assertEq(velocity.pairShareFor(0, 0), 0, "zero discretionary rate suppresses that release");
        assertEq(velocity.pairShareFor(0, velocity.signalRate()), p.share(0, velocity.signalRate()));
    }
}
