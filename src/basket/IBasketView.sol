// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice What the basket's services read from the shell: who governs it,
///         its equity, and the constituents whose pools they observe or trade.
interface IBasketView {
    function governance() external view returns (address);
    function equity() external view returns (uint256);
    function twapWindow() external view returns (uint32);
    function constituentsLength() external view returns (uint256);
    function constituents(uint256 i) external view returns (
        address token, uint8 decimals, uint256 basketAmount, uint256 initialPriceInBuck,
        uint24 feeTier, address pool, int24 tickLower, int24 tickUpper, bool buckIsToken0,
        uint256 targetWeightBp, uint128 treasuryLiquidity);
}
