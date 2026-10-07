// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {PairBasketEquityResponse} from "../../src/basket/PairBasketEquityResponse.sol";
import {BlockPairSignals} from "../../src/basket/BlockPairSignals.sol";
import {IBasketSignals} from "../../src/basket/BasketSignals.sol";

/// @notice Lab only: emit the actual pre-response book, intent and allocation.
/// The inherited response executes unchanged. Reverted responses also revert
/// their trace; the replay audit checks coverage and economic equality.
contract PairBasketDecisionTrace is PairBasketEquityResponse {
    struct Decision {
        uint256 at;
        uint256 lastExecutionAt;
        uint256 grain;
        uint256 band;
        uint256 spend;
        uint256 targetLiquidity;
        uint256 lien;
        uint256 creditLimit;
        uint256 stepBp;
        uint256[] values;
        uint256[] lpValues;
        uint256[] targets;
        uint256[] swapCaps;
        uint256[] desire;
        int256[] level;
        int256[] rate;
        int256[] change;
        int256[] high;
        uint256 readyMask;
    }
    event PairDecision(Decision input, Allocation proposed);

    constructor(address policy) PairBasketEquityResponse(policy) {}

    function _respond() internal override returns (uint256) {
        // These calls duplicate the parent's idempotent observation at the
        // same native block/timestamp, never manufacture an extra sample.
        pairPolicy.observe();
        if (responseSignals != address(0)) { try IBasketSignals(responseSignals).observe() {} catch {} }
        Snap memory s = _snap();
        uint256 n = s.m.length;
        Decision memory d;
        d.at = block.timestamp; d.lastExecutionAt = pairPolicy.lastExecutionAt();
        d.grain = _grainS(s); d.band = _grossS(s, MARK_TWAP) * eq.weightBandBp / 10000;
        d.spend = s.spend; d.targetLiquidity = _targetS(s); d.lien = s.lien;
        d.creditLimit = s.k * _equityS(s, MARK_LOW) / 1e18;
        d.stepBp = eq.stepBp;
        d.values = new uint256[](n); d.lpValues = new uint256[](n);
        d.targets = new uint256[](n); d.swapCaps = new uint256[](n);
        d.desire = new uint256[](n*n); d.level = new int256[](n*n);
        d.rate = new int256[](n*n); d.change = new int256[](n*n); d.high = new int256[](n*n);
        BlockPairSignals observer = pairPolicy.signals();
        d.readyMask = observer.readyMask();
        uint256 total = _grossS(s, MARK_TWAP);
        for (uint256 i; i < n; ++i) {
            d.lpValues[i] = s.m[i].posTwap;
            d.values[i] = s.m[i].posTwap + _tokenValue(s, i);
            d.targets[i] = total * constituents[i].targetWeightBp / 10000;
            d.swapCaps[i] = _swapCap(s.m[i]);
            for (uint256 j; j < n; ++j) {
                if (i == j) continue;
                uint256 k = i*n+j;
                BlockPairSignals.Pair memory p = observer.pair(i, j);
                d.level[k] = p.level; d.rate[k] = p.rate;
                d.desire[k] = pairPolicy.desire(i, j);
                (, int128 change) = pairPolicy.motion(observer.pairIndex(i, j));
                d.change[k] = i < j ? int256(change) : -int256(change);
                d.high[k] = p.high + int256((pairPolicy.remainingImpact(i) + pairPolicy.remainingImpact(j)) / observer.responseTime());
            }
        }
        emit PairDecision(d, _allocation(s));
        return super._respond();
    }
}
