// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IBasketRebalancer -- replaceable BUCK<->TOKEN route provider.
///
/// @notice The BuckBasket core holds funds and executes swaps; this replaceable
///         sub-contract only *advises* the route.  It is a governance-curated
///         registry of external FX paths (e.g. TOKEN -> USDC -> BUCK through deep
///         third-party pools) used to convert TOKEN<->BUCK more cheaply than the
///         basket's own (possibly thin) TOKEN/BUCK pool.
///
///         `pathFor` returns a Uniswap V3 encoded multi-hop path
///         (`abi.encodePacked(tokenIn, fee0, mid, fee1, tokenOut, ...)`) the
///         basket executes via `ISwapRouter.exactInput`/`exactOutput`.  An empty
///         result means "no FX route" -- the basket falls back to swapping its
///         internal TOKEN/BUCK pool directly, so it never depends on the
///         rebalancer being set or a route being registered.
///
///         Governance swaps the whole strategy via `BuckBasket.setRebalancer`
///         (the pre-Diamond replaceability path); funds never leave the basket.
interface IBasketRebalancer {
    /// @notice Encoded V3 path `tokenIn -> ... -> tokenOut`, or empty bytes if no
    ///         external route is registered (caller uses the internal pool).
    function pathFor(address tokenIn, address tokenOut)
        external view returns (bytes memory path);
}
