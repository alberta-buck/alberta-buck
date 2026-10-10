// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice The pair observer: how each TOKEN is moving against each other,
///         from prices alone. Levels are whole-token log prices in fractional
///         ticks (1e18 per tick); rates are fractional ticks per second.
interface IPairSignals {
    /// @notice One ordered pair across every ready quote route. The mask must
    ///         be inspected: zeros with no route ready are unavailable, never
    ///         evidence of no motion.
    struct Pair {
        int256 level;
        int256 rate;
        int256 low;
        int256 high;
        uint256 priceError;
        uint256 levelSpread;
        uint256 rateSpread;
        uint256 readyMask;
    }

    function tokenCount() external view returns (uint256);
    function venueCount() external view returns (uint256);
    function responseTime() external view returns (uint32);
    function maxGap() external view returns (uint32);
    function rateFloor() external view returns (uint128);
    function tokens(uint256 i) external view returns (address);
    function routes(uint256 index) external view returns (address pool, bool token0, int128 decimalOffset);
    function venues(uint256 q) external view returns (
        uint64 endpointAt, uint64 midpoint2, uint64 started2, uint32 samples, bool readable, uint24 elapsed2);
    function lastSampleAt() external view returns (uint64);
    function isDue() external view returns (bool);
    function observe() external;
    function readyMask() external view returns (uint256 mask);
    function pairIndex(uint256 i, uint256 j) external view returns (uint256);
    function pair(uint256 i, uint256 j) external view returns (Pair memory);
}
