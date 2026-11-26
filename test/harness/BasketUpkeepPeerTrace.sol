// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketUpkeepSignalTrace} from "./BasketUpkeepSignalTrace.sol";
import {IPairSignals} from "../../src/basket/IPairSignals.sol";
import {PairRebalancePolicy} from "../../src/basket/PairRebalancePolicy.sol";

/// @notice Read-only diagnostics for the ordered peers behind a zero directional rate.
contract BasketUpkeepPeerTrace is BasketUpkeepSignalTrace {
    uint256 private immutable from;
    uint256 private immutable until;

    enum Gate { Unready, FilteredSpread, LiveSpread, ZeroRate, Released }

    event SignalPeer(
        uint256 indexed constituent,
        uint256 indexed peer,
        bool sell,
        Gate gate,
        uint256 readyMask,
        uint64 observedAt,
        uint64 policySampleAt,
        int256 relativeLevel,
        int256 rate,
        int256 low,
        int256 high,
        int128 change,
        uint256 intent
    );

    constructor(address policy, address parity_, uint256 parityGain_, uint256 parityGainDear_,
        uint256 from_, uint256 until_)
        BasketUpkeepSignalTrace(policy, parity_, parityGain_, parityGainDear_)
    {
        from = from_;
        until = until_;
    }

    function _afterPlan(Snap memory s, Book memory b, Plan memory p) internal override {
        super._afterPlan(s, b, p);
        if (block.timestamp < from || block.timestamp >= until) return;
        uint256 mask = _eligible(b, p.gaps);
        for (uint256 i; i < p.gaps.length; ++i) {
            if (p.gaps[i] == 0 || (mask >> i) & 1 == 0) continue;
            bool sell = p.gaps[i] > 0;
            for (uint256 j; j < p.gaps.length; ++j)
                if (i != j) _tracePeer(i, j, sell);
        }
    }

    function _tracePeer(uint256 i, uint256 j, bool sell) private {
        PairRebalancePolicy policy = PairRebalancePolicy(address(pairPolicy));
        IPairSignals signals = policy.signals();
        (uint256 a, uint256 b) = sell ? (i, j) : (j, i);
        IPairSignals.Pair memory pair = signals.pair(a, b);
        int256 level = pair.level - int256(policy.referenceLevels(a)) + int256(policy.referenceLevels(b));
        uint64 observed = signals.lastSampleAt();
        uint64 applied = policy.sampledAt();
        uint256 intent;
        Gate gate;
        if (pair.readyMask != (uint256(1) << signals.venueCount()) - 1
            || pair.readyMask != policy.readyMask() || observed != applied) {
            gate = Gate.Unready;
        } else if (level <= policy.MIN_SEPARATION()) {
            gate = Gate.FilteredSpread;
        } else if (!policy.separated(a, b)) {
            gate = Gate.LiveSpread;
        } else {
            intent = policy.desire(a, b);
            gate = intent == 0 ? Gate.ZeroRate : Gate.Released;
        }
        (, int128 change) = policy.motion(signals.pairIndex(a, b));
        emit SignalPeer(i, j, sell, gate, pair.readyMask, observed, applied,
            level, pair.rate, pair.low, pair.high, a < b ? change : -change, intent);
    }
}
