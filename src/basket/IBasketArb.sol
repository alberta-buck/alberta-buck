// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice Funded basket arbitrage, dispatched to the existing upkeep facet.
/// Input amounts use nominal BUCK; returns use raw full-value BUCK.
/// LP fees are part of lpReturn,
/// not an extra amount to add to totalReturn. Quotes are bounded estimates;
/// execution refreshes them and independently checks actual portfolio value.
interface IBasketArb {
    struct ArbPlan {
        uint256 amount;
        uint8 direction;
        int256 cashReturn;
        int256 lpReturn;
        uint256 lpFees;
        int256 totalReturn;
    }

    function basketArbPlan(uint256 route) external view returns (ArbPlan memory);
    function executeBasketArb(uint256 route, uint8 direction, uint256 amount, uint256 minimumGain)
        external returns (ArbPlan memory);

    event BasketArbitrage(uint256 indexed route, uint8 direction, uint256 input,
                         int256 cashReturn, int256 lpReturn, uint256 lpFees, int256 totalReturn);
    /// @notice Ratios are net/nominal, scaled 1e36. Net transport and all
    /// pre/post fee bases are observable without assigning selectable BUCK lots.
    event ArbBuckFlow(address indexed pool, bool buckInput, uint256 nominal, uint256 netScaled,
                      uint256 basketRatioBefore, uint256 basketRatioAfter,
                      uint256 poolRatioBefore, uint256 poolRatioAfter);
}
