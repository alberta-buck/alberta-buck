// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice The parity reference: the reference basket's cost in BUCKs from
///         the pools, 1e18 at parity, with its freshness and day count. K and
///         the desk policy read it; the wheel samples it.
interface IBasketReference {
    function observe() external;
    function isDue() external view returns (bool);
    function fresh() external view returns (bool);
    function lastDay() external view returns (uint64);
    function lastSampleAt() external view returns (uint64);
    function consecutiveDays() external view returns (uint32);
    function rawValue() external view returns (int256);
    function basketValueInBuck() external view returns (int256);
    function marketValues() external view returns (int256 twap, int256 spot);
}
