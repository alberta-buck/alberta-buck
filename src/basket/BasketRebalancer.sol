// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBasketRebalancer} from "./IBasketRebalancer.sol";

/// @title BasketRebalancer -- stub strategy (scaffold).
///
/// @notice Minimal first cut: every plan resolves to "use the internal
///         TOKEN/BUCK pool".  The FX-route registry and ISwapRouter multi-hop
///         planning land in a later pass; until then the core's internal-pool
///         routing is the fallback and this contract just satisfies the
///         interface so the wiring (`BuckBasket.setRebalancer`) is exercised.
///
/// @dev    Governance-owned so route registration can be added without
///         touching the core.
contract BasketRebalancer is IBasketRebalancer {

    address public governance;

    /// @dev token -> encoded V3 path TOKEN->...->BUCK (FX route). Empty =
    ///      fall back to the internal pool.  Populated in a later pass.
    mapping(address => bytes) public buckOutPath;

    constructor(address _governance) {
        require(_governance != address(0), "gov=0");
        governance = _governance;
    }

    function planSellTokenForBuck(address token, uint256 buckOut, uint256 maxTokenIn)
        external pure returns (SwapStep[] memory steps)
    {
        // Scaffold: signal the basket to use its internal pool.
        token; buckOut; maxTokenIn;
        steps = new SwapStep[](0);
    }

    function planTreasuryReinvest(uint256 buckAmount)
        external pure returns (SwapStep[] memory steps, uint256 poolIdx)
    {
        buckAmount;
        steps = new SwapStep[](0);
        poolIdx = 0;
    }

    function planRebalance(uint256 maxNotionalBuck)
        external pure returns (SwapStep[] memory steps)
    {
        maxNotionalBuck;
        steps = new SwapStep[](0);
    }
}
