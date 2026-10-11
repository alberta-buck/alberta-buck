// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {IPairSignals} from "../../src/basket/IPairSignals.sol";
import {PairRebalancePolicy} from "../../src/basket/PairRebalancePolicy.sol";
import {PortfolioMath as PM} from "../../src/basket/PortfolioMath.sol";

/// @notice Lab only: a trial of a target that leans against price. For the
/// length of one response each constituent's target weight is its declared
/// weight times exp(-tilt x d), where d is how far its log price stands above
/// its accepted reference, relative to the others, as the pair observer has
/// it. The declared weights are put back before the response returns. A
/// liquidity position's share moves half as far as a wallet's would, so a
/// tilt of one half asks for what a wallet holder restoring the declared
/// weights would hold. Tilt zero is the facet itself.
///
/// A trial, not a design: the leash and the safeguard see the leaned weights
/// too, weights are whole basis points, and the wheel's view of what is due
/// keeps the declared ones.
contract BasketUpkeepLeanTrial is BasketUpkeepFacet {
    int256 private constant LN_TICK = 99995000333297;
    int256 public immutable tiltWad;

    constructor(address policy, int256 tiltWad_) BasketUpkeepFacet(policy) { tiltWad = tiltWad_; }

    function _respond() internal override returns (uint256 work) {
        if (tiltWad == 0) return super._respond();
        pairPolicy.observe();
        uint256 n = constituents.length;
        IPairSignals observer = pairPolicy.signals();
        if (observer.readyMask() != (uint256(1) << observer.venueCount()) - 1) return super._respond();
        PairRebalancePolicy policy = PairRebalancePolicy(address(pairPolicy));
        uint256[] memory declared = new uint256[](n);
        uint256[] memory raw = new uint256[](n);
        for (uint256 i; i < n; ++i) {
            declared[i] = constituents[i].targetWeightBp;
            int256 above;                       // fractional ticks above reference, against the mean of all
            for (uint256 j; j < n; ++j) {
                if (j == i) continue;
                above += observer.pair(i, j).level - (int256(policy.referenceLevels(i)) - policy.referenceLevels(j));
            }
            above /= int256(n);
            raw[i] = declared[i] * PM.expWad(-(above * LN_TICK / 1e18) * tiltWad / 1e18);
        }
        uint256[] memory leaned = PM.weights(raw);
        for (uint256 i; i < n; ++i) constituents[i].targetWeightBp = leaned[i];
        work = super._respond();
        for (uint256 i; i < n; ++i) constituents[i].targetWeightBp = declared[i];
    }
}
