// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBasketView} from "./IBasketView.sol";
import {IBasketSignals} from "./IBasketSignals.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";

/// @notice The parity reference: a fast validated raw value of the reference
///         basket in BUCKs. No positions, targets or K. Each publication is
///         one coherent all-pool observation; a missing pool invalidates it.
///
///         It still carries the daily common/differential EMA ladders, which
///         only the inherited response plan reads and which retire with it.
///         Their historical levels are supplied explicitly at bootstrap, never
///         inferred from a first live price. Gaps preserve levels and discard
///         velocity evidence.
contract BasketReference is IBasketSignals {
    IBasketView public immutable basket;
    uint32 public immutable sampleInterval;
    uint32 public immutable maxAge;
    uint16 public immutable maxDeviationBp;
    uint64 public lastDay;
    uint64 public lastSampleAt;
    uint256 public lastAttemptBlock;
    uint256 public sampledConstituents;
    uint32 public consecutiveDays;
    bool public valid;
    bool public historyInitialized;
    uint256 public historyConstituents;
    bytes32 public historyDigest;
    int256 public rawValue;
    uint16[7] public windows = [5, 10, 20, 40, 80, 160, 1280];
    uint16[7] public commonWindows = [5, 10, 20, 40, 80, 160, 320];
    int256[7] public common;
    mapping(uint256 => int256[7]) private _ema;
    mapping(uint256 => int256[7]) private _velocity;
    mapping(uint256 => int256) public deviation;

    error InvalidObservation();
    error InvalidHistory();
    error NotGovernance();
    event Observed(uint64 indexed at, uint64 indexed day, int256 raw, bool restarted);
    event ObservationUnavailable(uint256 indexed at);
    event HistoryInitialized(bytes32 indexed digest, uint64 throughDay, uint32 samples, uint256 constituents);

    constructor(address basket_, uint32 interval_, uint32 maxAge_, uint16 deviationBp_) {
        require(basket_ != address(0) && interval_ > 0 && interval_ <= 1 days
                && maxAge_ >= interval_ && maxAge_ <= 1 days
                && deviationBp_ > 0 && deviationBp_ <= 1000, "signal params");
        basket = IBasketView(basket_);
        sampleInterval = interval_;
        maxAge = maxAge_;
        maxDeviationBp = deviationBp_;
    }

    function fresh() public view returns (bool) {
        return valid && block.timestamp <= uint256(lastSampleAt) + maxAge
            && sampledConstituents == basket.constituentsLength();
    }

    function isDue() public view returns (bool) {
        return lastAttemptBlock != block.number
            && (!fresh() || block.timestamp >= uint256(lastSampleAt) + sampleInterval);
    }

    /// @notice Install governance-accepted, pre-run historical EMA state.
    ///         This imports history, not a live oracle publication: it does
    ///         not freshen prices or count toward the desk's persistence.
    ///         The digest identifies the off-chain source and calculation;
    ///         this contract does not verify their historical authenticity.
    function initializeHistory(
        int256[7][] calldata levels,
        int256[7] calldata commonLevels,
        uint64 throughDay,
        uint32 samples,
        bytes32 digest
    ) external {
        if (msg.sender != basket.governance()) revert NotGovernance();
        uint256 n = basket.constituentsLength();
        if (historyInitialized || lastSampleAt != 0 || n == 0 || levels.length != n
            || samples == 0 || digest == bytes32(0) || throughDay >= block.timestamp / 1 days)
            revert InvalidHistory();
        for (uint256 i; i < n; ++i) {
            for (uint256 k; k < 7; ++k) {
                // A centered pair of valid V3 ticks cannot exceed this range.
                if (levels[i][k] < -1774544e18 || levels[i][k] > 1774544e18) revert InvalidHistory();
                _ema[i][k] = levels[i][k];
            }
        }
        for (uint256 k; k < 7; ++k) {
            if (commonLevels[k] < -1e18 || commonLevels[k] > 1e36) revert InvalidHistory();
            common[k] = commonLevels[k];
        }
        historyInitialized = true;
        historyConstituents = n;
        historyDigest = digest;
        lastDay = throughDay;
        emit HistoryInitialized(digest, throughDay, samples, n);
    }

    function historyReady() public view returns (bool) {
        return historyInitialized && historyConstituents == basket.constituentsLength();
    }

    /// @notice Strict live reference for K or an execution-time direction check.
    ///         No EMA or inventory offset is added to the parity error.
    function basketValueInBuck() external view returns (int256 raw) {
        (raw,) = readPrices();
    }

    function readPrices() public view returns (int256 raw, int256[] memory ticks) {
        (raw, ticks,) = _readMarket();
    }

    function marketValues() external view returns (int256 twap, int256 spot) {
        (twap,, spot) = _readMarket();
    }

    function _readMarket() private view returns (int256 raw, int256[] memory ticks, int256 spotValue) {
        uint256 n = basket.constituentsLength();
        uint32 window = basket.twapWindow();
        if (n == 0 || window == 0) revert InvalidObservation();
        ticks = new int256[](n);
        int256 sum;
        for (uint256 i; i < n; ++i) {
            (address token, uint8 dec, uint256 amount,,, address pool,,, bool b0,,) = basket.constituents(i);
            if (IUniswapV3Pool(pool).liquidity() == 0) revert InvalidObservation();
            int24 tick = UniswapV3OracleLib.consult(pool, window);
            (, int24 spot,,,,,) = IUniswapV3Pool(pool).slot0();
            uint256 p = UniswapV3OracleLib.getQuoteAtTick(tick, uint128(10 ** dec), token, _buck(pool, b0));
            uint256 ps = UniswapV3OracleLib.getQuoteAtTick(spot, uint128(10 ** dec), token, _buck(pool, b0));
            uint256 delta = p > ps ? p - ps : ps - p;
            if (p == 0 || delta * 10000 > p * maxDeviationBp) revert InvalidObservation();
            raw += int256(UniswapV3OracleLib.mulDiv(amount, p, 1e18));
            spotValue += int256(UniswapV3OracleLib.mulDiv(amount, ps, 1e18));
            ticks[i] = (b0 ? -int256(tick) : int256(tick)) * 1e18;
            sum += ticks[i];
        }
        if (raw <= 0) revert InvalidObservation();
        int256 mean = sum / int256(n);
        for (uint256 i; i < n; ++i) ticks[i] -= mean;
    }

    function _buck(address pool, bool b0) private view returns (address) {
        return b0 ? IUniswapV3Pool(pool).token0() : IUniswapV3Pool(pool).token1();
    }

    function observe() external {
        if (!isDue()) return;
        lastAttemptBlock = block.number;
        try this.readPrices() returns (int256 raw, int256[] memory ticks) {
            uint64 today = uint64(block.timestamp / 1 days);
            bool restart = !valid || sampledConstituents != ticks.length || today > lastDay + 1;
            if (restart || today != lastDay) {
                bool advance = today != lastDay;
                bool velocityKnown = today == lastDay + 1 && (valid || lastSampleAt == 0);
                bool ready = historyReady();
                for (uint256 i; i < ticks.length; ++i) {
                    deviation[i] = ticks[i];
                    for (uint256 k; k < 7; ++k) {
                        int256 step = !ready || !advance ? int256(0)
                            : (ticks[i] - _ema[i][k]) * 2 / int256(uint256(windows[k]) + 1);
                        _ema[i][k] += step;
                        _velocity[i][k] = velocityKnown ? step : int256(0);
                    }
                }
                if (ready && advance) {
                    for (uint256 k; k < 7; ++k) {
                        int256 x = raw - 1e18;
                        common[k] += (x - common[k]) * 2 / int256(uint256(commonWindows[k]) + 1);
                    }
                }
                consecutiveDays = restart ? 1 : consecutiveDays + 1;
                lastDay = today;
            }
            rawValue = raw;
            sampledConstituents = ticks.length;
            lastSampleAt = uint64(block.timestamp);
            valid = true;
            emit Observed(lastSampleAt, today, raw, restart);
        } catch {
            valid = false;
            consecutiveDays = 0;
            emit ObservationUnavailable(block.timestamp);
        }
    }

    function ladder(uint256 i) external view returns (int256[7] memory, int256[7] memory) {
        return (_ema[i], _velocity[i]);
    }

    function excursion(uint256 i) public view returns (int256) {
        return fresh() && historyReady() ? deviation[i] - _ema[i][6] : int256(0);
    }

    function votes(uint256 i) public view returns (uint256 count) {
        int256 e = excursion(i);
        for (uint256 k; k < 6; ++k) {
            int256 v = _velocity[i][k];
            if ((e > 0 && v < 0) || (e < 0 && v > 0)) ++count;
        }
    }

    function turned(uint256 i) public view returns (bool) { return votes(i) >= 4; }

    function running(uint256 i, int256 side) external view returns (bool) {
        int256 e = excursion(i);
        return (side > 0 ? e > 0 : e < 0) && !turned(i);
    }
}
