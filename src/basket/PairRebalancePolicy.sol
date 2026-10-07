// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BlockPairSignals} from "./BlockPairSignals.sol";
import {IEquityBasketView} from "./EquityDesk.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

/// @notice Graded pair intent. It owns no assets and authorizes no spending.
/// The basket supplies its actual gaps and enforces its own risk/execution caps.
contract PairRebalancePolicy {
    int256 private constant WAD = 1e18;
    int256 private constant LN_TICK = 99995000333297;
    BlockPairSignals public immutable signals;
    address public immutable basket;
    uint256 public immutable count;
    int128[] public referenceLevels; // Accepted historical whole-TOKEN log-price means, fractional ticks.
    int256 public constant MIN_SEPARATION = 100e18; // ~1%: explicit first-experiment spread floor.
    // Fixed first-experiment parameters, in log returns per day / fractions.
    uint256 public constant RELEASE_SPEED = 2e15; // 20 log bp/day
    uint256 public constant RELEASE_RATE = 1e17; // at most 10% of gap/day before closure
    uint256 public constant CLOSURE_GAIN = 20;
    uint256 public constant IMPACT_MEMORY = 2 days;

    struct Motion { int128 rate; int128 change; }
    struct Impact { uint128 magnitude; uint64 at; }
    mapping(uint256 => Motion) public motion;
    mapping(uint256 => Impact) public impacts;
    uint64 public sampledAt;
    uint64 public sampledMidpoint2;
    uint256 public readyMask;
    uint64 public lastExecutionAt;
    event OwnImpact(uint256 indexed token, int256 tickChange);

    constructor(address signals_, address basket_, int128[] memory reference_) {
        signals = BlockPairSignals(signals_); basket = basket_;
        lastExecutionAt = uint64(block.timestamp);
        uint256 n = signals.tokenCount(); count = n;
        require(reference_.length == n, "reference size");
        for (uint256 i; i < n; ++i)
            require(reference_[i] >= -1774544e18 && reference_[i] <= 1774544e18, "reference range");
        referenceLevels = reference_;
        require(IEquityBasketView(basket_).constituentsLength() == n, "basket size");
        for (uint256 i; i < n; ++i) {
            (address token,,,,, address pool,,,,,) = IEquityBasketView(basket_).constituents(i);
            (address observed,,) = signals.routes(i); // BUCK is route zero for this consumer.
            require(token == signals.tokens(i) && pool == observed, "basket route");
        }
    }

    /// @notice Permissionless price observation; no book or budget changes.
    function observe() external {
        signals.observe();
        uint64 at = signals.lastSampleAt();
        (, uint64 midpoint2,,,,) = signals.venues(0);
        uint256 mask = signals.readyMask();
        if (at == sampledAt && mask == readyMask) return;
        bool continuous = readyMask == mask && mask != 0 && midpoint2 > sampledMidpoint2 && at > sampledAt
            && at - sampledAt <= signals.maxGap();
        for (uint256 i; i < count; ++i) for (uint256 j = i + 1; j < count; ++j) {
            uint256 p = signals.pairIndex(i, j);
            int256 rate = signals.pair(i, j).rate;
            // Compare braking across cadences on the observer's response
            // timescale, rather than awarding more evidence to slower ticks.
            int256 change = continuous ? (rate - motion[p].rate) * int256(uint256(signals.responseTime())) * 2
                / int256(uint256(midpoint2 - sampledMidpoint2)) : int256(0);
            // Pool ticks and configured gaps bound these far below int128.
            require(rate >= type(int128).min && rate <= type(int128).max
                && change >= type(int128).min && change <= type(int128).max, "motion range");
            motion[p] = Motion(int128(rate), int128(change));
        }
        sampledAt = at; sampledMidpoint2 = midpoint2; readyMask = mask;
    }

    /// @notice Record measured immediate impact, without claiming a known
    /// counterfactual decay path. Only this basket can report its executions.
    function recordImpact(uint256 i, int24 beforeTick, int24 afterTick) external {
        require(msg.sender == basket && i < count, "basket only");
        int256 change = (int256(afterTick) - beforeTick) * WAD;
        uint256 magnitude = uint256(change < 0 ? -change : change);
        if (magnitude == 0) return;
        magnitude += remainingImpact(i);
        require(magnitude <= type(uint128).max, "impact range");
        impacts[i] = Impact(uint128(magnitude), uint64(block.timestamp));
        emit OwnImpact(i, change);
    }

    function remainingImpact(uint256 i) public view returns (uint256) {
        Impact memory m = impacts[i];
        uint256 age = block.timestamp - m.at;
        if (age >= IMPACT_MEMORY) return 0;
        return uint256(m.magnitude) * (IMPACT_MEMORY - age) / IMPACT_MEMORY;
    }

    /// @notice Only an executed graded transfer consumes its elapsed budget.
    /// Price observations, credit marks and LP placement do not reset it.
    function recordExecution() external {
        require(msg.sender == basket, "basket only");
        lastExecutionAt = uint64(block.timestamp);
    }

    function _liveLevel(uint256 q, uint256 i) private view returns (int256) {
        (address pool, bool token0, int128 offset) = signals.routes(q * count + i);
        (, int24 tick,,,,,) = IUniswapV3Pool(pool).slot0();
        return (token0 ? int256(tick) : -int256(tick)) * WAD + offset;
    }

    /// @notice Reversion is oriented around accepted relative history. A
    /// negative velocity after crossing that reference is renewed departure,
    /// not permission to keep selling the now relatively cheaper TOKEN.
    function separated(uint256 a, uint256 b) public view returns (bool) {
        BlockPairSignals.Pair memory p = signals.pair(a, b);
        int256 anchor = int256(referenceLevels[a]) - referenceLevels[b];
        if (p.level - anchor <= MIN_SEPARATION) return false;
        // A cached filtered level can lag a crossing, including a pool swap
        // earlier in this same block. Recheck every live quote route.
        for (uint256 q; q < signals.venueCount(); ++q)
            if (_liveLevel(q, a) - _liveLevel(q, b) - anchor <= MIN_SEPARATION) return false;
        return true;
    }

    /// @return rateWad Fraction of an available allocation gap per day.
    /// Positive A/B velocity is outward for selling A to buy B.
    function desire(uint256 a, uint256 b) public view returns (uint256 rateWad) {
        BlockPairSignals.Pair memory p = signals.pair(a, b);
        uint256 allRoutes = (uint256(1) << signals.venueCount()) - 1;
        // The initial experiment requires every registered route. No vote is
        // manufactured on recovery or by observing twice at one timestamp.
        if (p.readyMask != allRoutes || readyMask != p.readyMask || sampledAt != signals.lastSampleAt()) return 0;
        if (!separated(a, b)) return 0;
        Motion memory m = motion[signals.pairIndex(a, b)];
        int256 change = a < b ? int256(m.change) : -int256(m.change);
        int256 own = int256((remainingImpact(a) + remainingImpact(b)) / signals.responseTime());
        int256 low = p.low - own; int256 high = p.high + own;
        uint256 uncertainty = uint256(p.rate - low > high - p.rate ? p.rate - low : high - p.rate);
        uint256 braking = change < 0 ? uint256(-change) * uint256(WAD) / (uncertainty + signals.rateFloor()) : 0;
        if (braking > uint256(WAD)) braking = uint256(WAD);
        uint256 outward = p.rate > 0 ? uint256(p.rate * LN_TICK / WAD) * 86400 : 0;
        uint256 release = RELEASE_SPEED * uint256(WAD) / (RELEASE_SPEED + outward);
        // A stationary, quiet ratio can release a small gap fraction too.
        if (p.rate >= -int256(uint256(signals.rateFloor())) && p.rate <= int256(uint256(signals.rateFloor())))
            braking = uint256(WAD) / 5;
        rateWad = RELEASE_RATE * release / uint256(WAD) * braking / uint256(WAD);
        if (high < 0) rateWad += uint256(-high * LN_TICK / WAD) * 86400 * CLOSURE_GAIN;
        if (rateWad > uint256(WAD)) rateWad = uint256(WAD); // one gap/day maximum
    }

    function fraction(uint256 rateWad, uint256 elapsed) external pure returns (uint256) {
        if (elapsed > 1 days) elapsed = 1 days; // bounded catch-up
        uint256 u = rateWad * elapsed / 1 days;
        return u * uint256(WAD) / (uint256(WAD) + u);
    }
}
