// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IBasketRebalancer -- replaceable routing + rebalancing strategy.
///
/// @notice The BuckBasket core holds funds and the unconditionally-solvent
///         primitives; this sub-contract holds the *intelligence*: the FX-pool
///         route registry (BUCK/USDC, BUCK/USDT, USDC|USDT/TOKEN) and the
///         planning logic for (a) covering a redeem-time BUCK shortfall,
///         (b) re-LP'ing treasury BUCK profit, and (c) standalone constant-mix
///         rebalancing.
///
///         It is an **advisor**: it returns `SwapStep`s; the basket executes
///         them (via Uniswap `ISwapRouter` for FX hops, or its own pool for the
///         internal TOKEN/BUCK pair), so funds never leave the basket's
///         custody.  Governance swaps the whole strategy via
///         `BuckBasket.setRebalancer` -- the pre-Diamond replaceability path.
interface IBasketRebalancer {

    /// @dev One router hop.  `path` is a Uniswap V3 encoded path
    ///      (token,fee,token,...).  `useInternalPool == true` signals the
    ///      basket to swap against its own constituent pool instead of the
    ///      external router (no path needed).
    struct SwapStep {
        bytes   path;            // V3 encoded path for ISwapRouter (FX route)
        address tokenIn;
        address tokenOut;
        uint256 amountIn;        // 0 ⇒ exact-output mode (amountOut binds)
        uint256 amountOut;       // 0 ⇒ exact-input mode (amountIn binds)
        bool    useInternalPool; // true ⇒ swap the internal TOKEN/BUCK pool
    }

    /// @notice Plan the cheapest way to raise `buckOut` BUCK by selling at most
    ///         `maxTokenIn` of `token` (redeem shortfall cover).
    function planSellTokenForBuck(address token, uint256 buckOut, uint256 maxTokenIn)
        external view returns (SwapStep[] memory steps);

    /// @notice Plan re-investment of `buckAmount` treasury BUCK profit; returns
    ///         the swap steps and the constituent index to LP into.
    function planTreasuryReinvest(uint256 buckAmount)
        external view returns (SwapStep[] memory steps, uint256 poolIdx);

    /// @notice Plan bounded constant-mix moves (overweight → underweight).
    function planRebalance(uint256 maxNotionalBuck)
        external view returns (SwapStep[] memory steps);
}
