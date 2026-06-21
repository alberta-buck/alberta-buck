// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IBuckBasketVenue -- the AMM-venue seam for a BuckBasket.
///
/// @notice The basket shell (`BuckBasketProRata`) owns the economic *policy*
///         (pro-rata / sell-high allocation, the treasury split, burn seniority,
///         all receipt + outstanding-BUCK accounting) and is venue-agnostic.
///         Everything that actually touches the backing AMM -- pool setup,
///         liquidity in/out, price/TWAP reads, and conversions -- lives behind
///         this interface in a venue facet (`BuckBasketUniswapV3` today; a v4 or
///         Balancer facet later).
///
/// @dev    The facet is reached by `delegatecall` and shares the shell's storage
///         (`BuckBasketStorage`); every method runs in the shell's context, so
///         `address(this)` is the basket and all custody stays at the shell.
///         The shell invokes these via `IBuckBasketVenue(address(this)).fn(...)`
///         -- an external self-call routed by the shell's fallback into the
///         facet -- which is the same dispatch a future Diamond keeps.  Mutating
///         methods therefore require `msg.sender == address(this)` in the facet
///         (they must not be reachable directly through the fallback).
///
///         Verb set: **provide** (exact-TOKEN, mints the partner BUCK) ·
///         **invest** (from BUCK, swap-balanced) · **convert** (TOKEN -> BUCK) ·
///         **withdraw** (liquidity -> both sides).  The facet returns *what it
///         did*; the shell books ownership (treasury vs depositor).
interface IBuckBasketVenue {

    // --- Pool setup ------------------------------------------------------- //

    /// @notice Create/init the (BUCK, token) pool and return the venue-specific
    ///         constituent fields the shell records.
    function setupPool(address token, uint8 decimals, uint256 initialPriceInBuck, uint24 feeTier)
        external returns (address pool, int24 tickLower, int24 tickUpper, bool buckIsToken0);

    // --- Value reads (loops live in the facet, not the shell) ------------- //

    /// @notice Controller process variable: Σ basketAmount·price (BUCK, 18-dec).
    function basketValueInBuck() external view returns (int256);

    /// @notice Per-pool *depositor* BUCK reserve (full-range ⇒ pool value =
    ///         2·buckReserve, the value sufficient statistic), the depositor
    ///         liquidity slice, their total `B`, and each pool's spot price (BUCK
    ///         per whole TOKEN) -- the shell needs spot to scale the basket's
    ///         fixed-quantity target weights by initialPrice/spot.  Carries the
    ///         spot/TWAP manipulation guard on every touched pool.
    function poolBuckValues()
        external view
        returns (uint256[] memory bv, uint128[] memory depL, uint256 B, uint256[] memory prices);

    // --- Liquidity in/out ------------------------------------------------- //

    /// @notice LP an exact TOKEN amount (already held by the basket) full-range,
    ///         minting the partner BUCK.  Returns the minted liquidity and the
    ///         BUCK principal (the receipt's share unit).
    function provideForToken(uint256 i, uint256 tokenAmount, uint256 maxDeviationBp)
        external returns (uint128 liquidity, uint256 buckMinted);

    /// @notice Burn `liquidity` from pool `i` and collect both sides to the basket.
    function withdrawLiquidity(uint256 i, uint128 liquidity)
        external returns (uint256 tokenOut, uint256 buckOut);

    // --- The two generic conversion verbs --------------------------------- //

    /// @notice Deploy `buckAmount` BUCK into a basket position: swap to balance
    ///         and LP into pool `poolHint` (`type(uint256).max` ⇒ most
    ///         underweight).  Returns the pool used, liquidity minted, and BUCK
    ///         consumed; the shell tags ownership (treasury slice or receipt).
    function investFromBucks(uint256 buckAmount, uint256 poolHint)
        external returns (uint256 poolIdx, uint128 liquidity, uint256 buckConsumed);

    /// @notice Raise ~`targetBuck` BUCK by converting from `tokenInventory`
    ///         (per-constituent TOKEN the basket holds), best route first.
    ///         Returns the BUCK gained, the value lost to slippage+fee (for the
    ///         caller's loss-budget check), and the remaining inventory.
    function convertIntoBucks(uint256[] calldata tokenInventory, uint256 targetBuck)
        external returns (uint256 gained, uint256 lossValue, uint256[] memory inventoryAfter);
}
