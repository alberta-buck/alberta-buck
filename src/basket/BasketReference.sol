// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBasketView} from "./IBasketView.sol";
import {IBasketReference} from "./IBasketReference.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";

/// @notice The parity reference: a fast validated raw value of the reference
///         basket in BUCKs. No positions, targets or K. Each publication is
///         one coherent all-pool observation; a missing pool invalidates it.
///         It counts the consecutive days it has published; a missed day, a
///         failed read or a changed constituent list starts the count again.
contract BasketReference is IBasketReference {
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
    int256 public rawValue;

    error InvalidObservation();
    event Observed(uint64 indexed at, uint64 indexed day, int256 raw, bool restarted);
    event ObservationUnavailable(uint256 indexed at);

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

    /// @notice Strict live reference for K or an execution-time direction check.
    ///         No inventory offset is added to the parity error.
    function basketValueInBuck() external view returns (int256 raw) {
        (raw,) = marketValues();
    }

    /// @notice The reference basket at each pool's TWAP and at its spot. Any
    ///         pool that is empty, or whose spot has left its TWAP by more
    ///         than the allowed deviation, refuses the whole reading.
    function marketValues() public view returns (int256 twap, int256 spot) {
        uint256 n = basket.constituentsLength();
        uint32 window = basket.twapWindow();
        if (n == 0 || window == 0) revert InvalidObservation();
        for (uint256 i; i < n; ++i) {
            (address token, uint8 dec, uint256 amount,,, address pool,,, bool b0,,) = basket.constituents(i);
            if (IUniswapV3Pool(pool).liquidity() == 0) revert InvalidObservation();
            int24 tick = UniswapV3OracleLib.consult(pool, window);
            (, int24 spotTick,,,,,) = IUniswapV3Pool(pool).slot0();
            uint256 p = UniswapV3OracleLib.getQuoteAtTick(tick, uint128(10 ** dec), token, _buck(pool, b0));
            uint256 ps = UniswapV3OracleLib.getQuoteAtTick(spotTick, uint128(10 ** dec), token, _buck(pool, b0));
            uint256 delta = p > ps ? p - ps : ps - p;
            if (p == 0 || delta * 10000 > p * maxDeviationBp) revert InvalidObservation();
            twap += int256(UniswapV3OracleLib.mulDiv(amount, p, 1e18));
            spot += int256(UniswapV3OracleLib.mulDiv(amount, ps, 1e18));
        }
        if (twap <= 0) revert InvalidObservation();
    }

    function _buck(address pool, bool b0) private view returns (address) {
        return b0 ? IUniswapV3Pool(pool).token0() : IUniswapV3Pool(pool).token1();
    }

    function observe() external {
        if (!isDue()) return;
        lastAttemptBlock = block.number;
        try this.basketValueInBuck() returns (int256 raw) {
            uint64 today = uint64(block.timestamp / 1 days);
            uint256 n = basket.constituentsLength();
            bool restart = !valid || sampledConstituents != n || today > lastDay + 1;
            if (restart || today != lastDay) {
                consecutiveDays = restart ? 1 : consecutiveDays + 1;
                lastDay = today;
            }
            rawValue = raw;
            sampledConstituents = n;
            lastSampleAt = uint64(block.timestamp);
            valid = true;
            emit Observed(lastSampleAt, today, raw, restart);
        } catch {
            valid = false;
            consecutiveDays = 0;
            emit ObservationUnavailable(block.timestamp);
        }
    }
}
