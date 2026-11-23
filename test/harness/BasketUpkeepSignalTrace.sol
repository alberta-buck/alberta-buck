// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";
import {PairRebalancePolicy} from "../../src/basket/PairRebalancePolicy.sol";


/// @notice Research facet. Observe the decision after the wheel pairs idle inventory
/// and samples its signals, before it records the new rates or executes either leg.
contract BasketUpkeepSignalTrace is BasketUpkeepFacet {
    constructor(address policy, address parity_, uint256 parityGain_, uint256 parityGainDear_)
        BasketUpkeepFacet(policy, parity_, parityGain_, parityGainDear_) {}

    struct Decision {
        int256 gap;
        uint256 urgency;
        bool eligible;
        bool leash;
        uint256 pairRate;
        uint256 pairAmount;
        uint256 bookAmount;
        uint256 ceilingAmount;
        uint256 buyStepCap;
        uint256 sellStepCap;
        uint256 depthCap;
        uint256 grain;
        uint256 plannedBuy;
        uint256 plannedSell;
        uint256 planWithoutPairBuy;
        uint256 planWithoutPairSell;
    }
    event SignalDecision(uint256 indexed token, Decision detail);

    function _afterPlan(Snap memory s, Book memory b, Plan memory p) internal override {
        Plan memory control = _plan(s, b, false);
        uint256 held;
        for (uint256 i; i < b.token.length; ++i) held += b.token[i];
        uint256 excess = b.issued > int256(b.ceiling) ? uint256(b.issued - int256(b.ceiling)) : 0;
        uint256[] memory move = _move(b, held, excess);
        uint256 mask = _eligible(b, p.gaps);
        for (uint256 i; i < b.token.length; ++i) {
            Decision memory d;
            d.gap = p.gaps[i];
            d.urgency = PairRebalancePolicy(address(pairPolicy)).urgencyOf(i, d.gap);
            d.eligible = ((mask >> i) & 1) != 0;
            uint256 declared = b.total * constituents[i].targetWeightBp / 10000;
            uint256 deviation = b.token[i] > declared ? b.token[i] - declared : declared - b.token[i];
            d.leash = _beyondLeash(deviation, b.total, constituents[i].targetWeightBp);
            bool over = b.token[i] > b.target[i];
            uint256 gap = over ? b.token[i] - b.target[i] : b.target[i] - b.token[i];
            if (s.m[i].depth != 0 && gap != 0) {
                if (!d.leash && d.eligible) {
                    uint256 excursion = uint256(d.gap < 0 ? -d.gap : d.gap);
                    d.pairRate = _rate(i, over, b.token.length, excursion);
                    d.pairAmount = gap * _pairShare(i, d.pairRate) / 1e18;
                }
                if (move[i] != 0) {
                    uint256 pace = pairPolicy.maxRate();
                    if (d.pairRate > pace) pace = d.pairRate;
                    d.bookAmount = move[i] * pairPolicy.share(i, pace) / 1e18;
                }
            }
            if (held != 0) d.ceilingAmount = excess * b.token[i] / held;
            d.plannedBuy = p.buys[i];
            d.plannedSell = p.sells[i];
            d.planWithoutPairBuy = control.buys[i];
            d.planWithoutPairSell = control.sells[i];
            d.buyStepCap = b.target[i] * eq.stepBp / 10000;
            d.sellStepCap = b.token[i] * eq.stepBp / 10000;
            d.depthCap = _swapCap(s.m[i]);
            d.grain = b.grain;
            emit SignalDecision(i, d);
        }
    }
}
