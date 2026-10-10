// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {IPairSignals} from "./IPairSignals.sol";

/// @notice A price-only observer, callable directly or through SampleKind.
/// One call observes all registered routes; it never trades or values a book.
/// Levels are whole-token log prices in fractional ticks (1e18 per tick),
/// rates are fractional ticks/second. Error allowances are empirical, not
/// statistical confidence bounds. Configuration is immutable for continuity.
contract PairSignals is IPairSignals {
    int256 private constant WAD = 1e18;
    // ln(10) / ln(1.0001), rounded to 18 decimal places.
    int256 private constant TICKS_PER_DECIMAL = 23027002203299704104435;
    uint256 public immutable tokenCount;
    uint256 public immutable venueCount;
    uint32 public immutable responseTime;
    uint32 public immutable errorTime;
    uint32 public immutable maxGap;
    uint32 public immutable warmupTime;
    uint128 public immutable rateFloor;
    address[] public tokens;
    address[] public quotes;

    struct Route {
        address pool;
        bool token0;
        int128 decimalOffset;
    }
    struct Sample {
        int56 cumulative;
        int24 tick;
        uint160 sqrtPriceX96;
        uint128 liquidity;
    }
    struct Track {
        int128 level;
        int128 rate;
    }
    struct Error {
        uint128 price;
        uint128 acceleration; // EWMA |delta rate / elapsed seconds|, not a forecast state
    }
    struct Venue {
        uint64 endpointAt;
        uint64 midpoint2;
        uint64 started2;
        uint32 samples;
        bool readable;
        uint24 elapsed2; // Twice the last midpoint interval; entire Venue fits one slot.
    }

    // Venue-major flat arrays: q * tokenCount + i, or q * pairs + pairIndex.
    Route[] public routes;
    Sample[] public spots;
    Track[] public tracks;
    Venue[] public venues;
    mapping(uint256 => Error) public errors;
    uint256 public lastAttemptBlock;
    uint256 public epoch;
    uint64 public lastSampleAt;
    uint256 public availableMask;
    event Observed(uint256 indexed epoch, uint256 indexed blockId, uint256 availableMask, uint256 readyMask);

    constructor(
        address[] memory tokens_, address[] memory quotes_, address[] memory pools_,
        uint32 response_, uint32 error_, uint32 maxGap_, uint32 warmup_, uint128 floor_
    ) {
        uint256 n = tokens_.length;
        uint256 qn = quotes_.length;
        require(n >= 2 && n <= 8 && qn > 0 && qn <= 3 && pools_.length == n * qn, "dimensions");
        require(response_ >= 60 && response_ <= 30 days && error_ >= 60 && error_ <= 30 days
            && maxGap_ > 0 && maxGap_ <= 1 days && warmup_ >= response_ && warmup_ <= 120 days
            && floor_ > 0 && floor_ <= 1e24, "timing/floor");
        tokenCount = n; venueCount = qn;
        responseTime = response_; errorTime = error_; maxGap = maxGap_;
        warmupTime = warmup_; rateFloor = floor_;
        tokens = tokens_; quotes = quotes_;
        for (uint256 i; i < n; ++i) {
            require(tokens_[i] != address(0), "token");
            for (uint256 j; j < i; ++j) require(tokens_[i] != tokens_[j], "duplicate token");
        }
        for (uint256 q; q < qn; ++q) {
            require(quotes_[q] != address(0), "quote");
            for (uint256 r; r < q; ++r) require(quotes_[q] != quotes_[r], "duplicate quote");
            uint8 qd = IERC20Metadata(quotes_[q]).decimals();
            require(qd <= 36, "decimals");
            venues.push();
            for (uint256 i; i < n; ++i) {
                address pool = pools_[q * n + i];
                address t0 = IUniswapV3Pool(pool).token0();
                address t1 = IUniswapV3Pool(pool).token1();
                require((t0 == tokens_[i] && t1 == quotes_[q])
                    || (t1 == tokens_[i] && t0 == quotes_[q]), "pool tokens");
                uint8 td = IERC20Metadata(tokens_[i]).decimals();
                require(td <= 36, "decimals");
                routes.push(Route(pool, t0 == tokens_[i], int128((int256(uint256(td)) - int256(uint256(qd))) * TICKS_PER_DECIMAL)));
                spots.push(); tracks.push();
            }
        }
    }

    /// @dev Override for a chain whose native block ID differs from block.number.
    function _clock() internal view virtual returns (uint256) { return block.number; }
    function isDue() public view returns (bool) { return epoch == 0 || lastAttemptBlock != _clock(); }

    /// @notice One complete route read succeeds or fails as a unit. There is
    /// no outlier rejection: a valid dislocation remains visible in spots.
    function readVenue(uint256 q) external view returns (Sample[] memory samples) {
        require(q < venueCount, "venue");
        samples = new Sample[](tokenCount);
        uint32[] memory ago = new uint32[](1);
        for (uint256 i; i < tokenCount; ++i) {
            IUniswapV3Pool pool = IUniswapV3Pool(routes[q * tokenCount + i].pool);
            (int56[] memory cumulatives,) = pool.observe(ago);
            (uint160 sqrtPrice, int24 tick,,,,,) = pool.slot0();
            uint128 liquidity = pool.liquidity();
            require(liquidity > 0 && sqrtPrice > 0, "empty pool");
            samples[i] = Sample(cumulatives[0], tick, sqrtPrice, liquidity);
        }
    }

    function observe() external {
        if (!isDue()) return;
        lastAttemptBlock = _clock();
        uint256 mask;
        for (uint256 q; q < venueCount; ++q) {
            // A bad pool cannot use the entire caller budget or prevent
            // another complete venue from publishing. Pools are registered.
            try this.readVenue{gas: 100_000 * tokenCount}(q) returns (Sample[] memory samples) {
                _accept(q, samples);
                mask |= 1 << q;
            } catch {
                // Require a new common endpoint and warmup after recovery.
                delete venues[q];
            }
        }
        availableMask = mask;
        if (mask != 0) lastSampleAt = uint64(block.timestamp);
        ++epoch;
        emit Observed(epoch, lastAttemptBlock, mask, readyMask());
    }

    function _accept(uint256 q, Sample[] memory samples) private {
        Venue storage venue = venues[q];
        uint64 nowAt = uint64(block.timestamp);
        uint256 previousAt = venue.endpointAt;
        bool reset = !venue.readable || nowAt - previousAt > maxGap;
        uint256 n = tokenCount;
        int256[] memory observations = new int256[](n);
        int256 sum;
        for (uint256 i; i < n; ++i) {
            uint256 index = q * n + i;
            int56 previousCumulative = spots[index].cumulative;
            spots[index] = samples[i];
            if (!reset && nowAt > previousAt) {
                int56 delta;
                // V3's signed 56-bit cumulative wraps; subtract at that width.
                unchecked { delta = samples[i].cumulative - previousCumulative; }
                int256 value = int256(delta) * WAD / int256(nowAt - previousAt);
                if (!routes[index].token0) value = -value;
                observations[i] = value + routes[index].decimalOffset;
                sum += observations[i];
            }
        }
        venue.endpointAt = nowAt;
        venue.readable = true;
        if (reset) {
            venue.samples = 0; venue.midpoint2 = 0; venue.started2 = 0; venue.elapsed2 = 0;
            return;
        }
        if (nowAt == previousAt) return; // A new block need not mean new time.
        uint64 midpoint2 = uint64(previousAt + nowAt);
        int256 dt2 = int256(uint256(midpoint2 - venue.midpoint2));
        bool seed = venue.samples == 0;
        int256 k = dt2 * WAD / (2 * int256(uint256(responseTime)) + dt2);
        int256 beta = k * k / WAD;
        int256 alpha = 2 * k - beta;
        int256 g = dt2 * WAD / (2 * int256(uint256(errorTime)) + dt2);
        int256[] memory residuals = new int256[](n);
        int256[] memory rateChanges = new int256[](n);
        for (uint256 i; i < n; ++i) {
            Track storage track = tracks[q * n + i];
            int256 value = observations[i] - sum / int256(n);
            int256 residual = seed ? int256(0) : value - (int256(track.level) + int256(track.rate) * dt2 / 2);
            int256 change = seed ? int256(0) : beta * residual / WAD * 2 / dt2;
            int256 level = seed ? value : int256(track.level) + int256(track.rate) * dt2 / 2 + alpha * residual / WAD;
            int256 rate = seed ? int256(0) : int256(track.rate) + change;
            require(level >= type(int128).min && level <= type(int128).max
                && rate >= type(int128).min && rate <= type(int128).max, "track overflow");
            track.level = int128(level); track.rate = int128(rate);
            residuals[i] = residual; rateChanges[i] = change;
        }
        uint256 p;
        uint256 count = n * (n - 1) / 2;
        for (uint256 i; i < n; ++i) for (uint256 j = i + 1; j < n; ++j) {
            Error storage err = errors[q * count + p++];
            if (seed) { delete err.price; delete err.acceleration; }
            else {
                err.price = _ewma(err.price, _abs(residuals[i] - residuals[j]), g);
                err.acceleration = _ewma(err.acceleration, _abs(rateChanges[i] - rateChanges[j]) * 2 / uint256(dt2), g);
            }
        }
        if (seed) venue.started2 = midpoint2;
        venue.midpoint2 = midpoint2;
        // Both adjoining intervals are <= maxGap, so dt2 <= 2 days fits uint24.
        venue.elapsed2 = seed ? 0 : uint24(uint256(dt2));
        if (venue.samples != type(uint32).max) ++venue.samples;
    }

    function readyMask() public view returns (uint256 mask) {
        for (uint256 q; q < venueCount; ++q) {
            Venue memory v = venues[q];
            if (v.readable && block.timestamp <= uint256(v.endpointAt) + maxGap && v.samples >= 3
                && v.midpoint2 - v.started2 >= uint256(warmupTime) * 2) mask |= 1 << q;
        }
    }

    function pairIndex(uint256 i, uint256 j) public view returns (uint256) {
        require(i < tokenCount && j < tokenCount && i != j, "pair");
        if (i > j) (i, j) = (j, i);
        return i * (2 * tokenCount - i - 1) / 2 + j - i - 1;
    }

    function routePair(uint256 q, uint256 i, uint256 j)
        public view returns (int256 level, int256 rate, uint256 allowance, uint256 predictionError)
    {
        require(q < venueCount, "venue");
        uint256 index = pairIndex(i, j);
        Track memory a = tracks[q * tokenCount + i];
        Track memory b = tracks[q * tokenCount + j];
        Error memory err = errors[q * tokenCount * (tokenCount - 1) / 2 + index];
        // For constant acceleration and cadence, the rate lag of these gains
        // is exactly (2*T + dt/2) * acceleration. A one-sample rate change
        // undercounts this lag. This empirical allowance remains uncalibrated.
        uint256 lag = uint256(err.acceleration) * (8 * uint256(responseTime) + venues[q].elapsed2) / 4;
        return (int256(a.level) - b.level, int256(a.rate) - b.rate,
            uint256(rateFloor) + uint256(err.price) / responseTime + lag, err.price);
    }

    /// @notice Only ready routes enter consensus. The mask must be inspected:
    /// zero outputs with mask=0 are unavailable, never evidence of zero motion.
    function pair(uint256 i, uint256 j) external view returns (Pair memory out) {
        pairIndex(i, j);
        out.readyMask = readyMask();
        uint256 count;
        int256 minLevel; int256 maxLevel; int256 minRate; int256 maxRate;
        for (uint256 q; q < venueCount; ++q) {
            if (out.readyMask & (1 << q) == 0) continue;
            (int256 level, int256 rate, uint256 allowance, uint256 error) = routePair(q, i, j);
            int256 lo = rate - int256(allowance); int256 hi = rate + int256(allowance);
            if (count == 0) {
                minLevel = level; maxLevel = level; minRate = rate; maxRate = rate;
                out.low = lo; out.high = hi;
            } else {
                if (level < minLevel) minLevel = level;
                if (level > maxLevel) maxLevel = level;
                if (rate < minRate) minRate = rate;
                if (rate > maxRate) maxRate = rate;
                if (lo < out.low) out.low = lo;
                if (hi > out.high) out.high = hi;
            }
            out.level += level; out.rate += rate; out.priceError += error; ++count;
        }
        if (count != 0) {
            out.level /= int256(count); out.rate /= int256(count); out.priceError /= count;
            out.levelSpread = uint256(maxLevel - minLevel); out.rateSpread = uint256(maxRate - minRate);
        }
    }

    function _abs(int256 x) private pure returns (uint256) { return uint256(x < 0 ? -x : x); }
    function _ewma(uint128 old, uint256 sample, int256 gain) private pure returns (uint128) {
        int256 value = int256(uint256(old)) + gain * (int256(sample) - int256(uint256(old))) / WAD;
        require(value >= 0 && uint256(value) <= type(uint128).max, "error overflow");
        return uint128(uint256(value));
    }
}
