// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice The parity reference: the reference basket's value in net BUCK by
///         each TOKEN's best route, 1e18 at parity, with its freshness and day
///         count; and the price book behind it (BasketReference). K, the parity
///         signal and the desk policy read it; the wheel samples it.
interface IBasketReference {
    /// @notice The basket's value in USDC by each TOKEN's best route, as last
    ///         sampled; 0 unless every TOKEN has a USDC route.
    function valueUsdc() external view returns (uint256);
    /// @notice USDC a full-value BUCK fetches, and a net BUCK costs, as last sampled.
    function buckBid() external view returns (uint256);
    function buckAsk() external view returns (uint256);
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
