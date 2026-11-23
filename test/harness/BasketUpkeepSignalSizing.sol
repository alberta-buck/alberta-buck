// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketUpkeepSignalTrace} from "./BasketUpkeepSignalTrace.sol";
import {IPairSignals} from "../../src/basket/IPairSignals.sol";
import {PairRebalancePolicy} from "../../src/basket/PairRebalancePolicy.sol";
import {PortfolioMath as PM} from "../../src/basket/PortfolioMath.sol";

/// @notice Research facet for sizing an eligible discretionary leg by relative
/// reversal velocity, the distance to its allocation target, or their product.
contract BasketUpkeepSignalSizing is BasketUpkeepSignalTrace {
    uint256 private constant WAD = 1e18;
    uint256 private constant LN_TICK = 99995000333297;

    enum SignalMode { Velocity, Excursion, Both }

    SignalMode public immutable signalMode;
    uint256 public immutable signalRate;       // fraction-rate per day at full strength
    uint256 public immutable signalExcursion;  // share of the book at full excursion strength
    uint256 public immutable signalVelocity;   // closing log return per day at full velocity strength

    constructor(
        address policy,
        address parity_,
        uint256 parityGain_,
        uint256 parityGainDear_,
        SignalMode signalMode_,
        uint256 signalRate_,
        uint256 signalExcursion_,
        uint256 signalVelocity_
    ) BasketUpkeepSignalTrace(policy, parity_, parityGain_, parityGainDear_) {
        require(signalRate_ <= pairPolicy.maxRate(), "signal rate exceeds maxRate");
        require(signalExcursion_ != 0 && signalVelocity_ != 0, "signal scale is zero");
        signalMode = signalMode_;
        signalRate = signalRate_;
        signalExcursion = signalExcursion_;
        signalVelocity = signalVelocity_;
    }

    /// @notice The daily closing log return beyond the quote and own-impact
    /// allowance in the sell-a, buy-b direction. Uses the production policy's
    /// separation guard, including its live check on every quote route.
    function closingVelocity(uint256 a, uint256 b) public view returns (uint256) {
        PairRebalancePolicy policy = PairRebalancePolicy(address(pairPolicy));
        IPairSignals observer = policy.signals();
        IPairSignals.Pair memory p = observer.pair(a, b);
        uint256 allRoutes = (uint256(1) << observer.venueCount()) - 1;
        if (p.readyMask != allRoutes || policy.readyMask() != allRoutes
            || policy.sampledAt() != observer.lastSampleAt() || p.high >= 0 || !policy.separated(a, b)) return 0;
        uint256 own = (policy.remainingImpact(a) + policy.remainingImpact(b))
            * policy.impactShare() / WAD / observer.responseTime();
        if (uint256(-p.high) <= own) return 0;
        return (uint256(-p.high) - own) * LN_TICK * 1 days / WAD;
    }

    function _rate(uint256 i, bool sold, uint256 n, uint256 excursion) internal view override returns (uint256) {
        uint256 strength = WAD;
        if (signalMode != SignalMode.Velocity) {
            strength = PM.min(excursion, signalExcursion) * WAD / signalExcursion;
        }
        if (signalMode != SignalMode.Excursion) {
            uint256 sum;
            uint256 weights;
            for (uint256 j; j < n; ++j) {
                if (j == i) continue;
                uint256 weight = constituents[j].targetWeightBp;
                uint256 velocity = sold ? closingVelocity(i, j) : closingVelocity(j, i);
                sum += weight * PM.min(velocity, signalVelocity) * WAD / signalVelocity;
                weights += weight;
            }
            if (weights == 0) return 0;
            strength = strength * (sum / weights) / WAD;
        }
        return signalRate * strength / WAD;
    }

    function _pairShare(uint256 i, uint256 rate) internal view override returns (uint256) {
        return rate == 0 ? 0 : super._pairShare(i, rate);
    }
}
