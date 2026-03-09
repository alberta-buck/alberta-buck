// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "../../src/BuckKController.sol";

/// @notice Test harness: overrides _getBuckPrice() with a settable value.
contract BuckKHarness is BuckKController {
    int256 private _mockBuckPrice;

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _buckUsdcPool, uint32 _twapInterval,
        address _governance
    ) BuckKController(_Kp, _Ki, _Kd, _dT, _buckKMin, _buckKMax, _buckK, _buckUsdcPool, _twapInterval, _governance) {
        _mockBuckPrice = int256(int256(1e18)); // Default: $1.00
    }

    function setBuckPrice(int256 price) external {
        _mockBuckPrice = price;
    }

    function _getBuckPrice() internal view override returns (int256) {
        return _mockBuckPrice;
    }

    /// @notice Expose basket cost for test assertions.
    function getBasketCost() external view returns (int256) {
        return _getBasketCost();
    }
}
