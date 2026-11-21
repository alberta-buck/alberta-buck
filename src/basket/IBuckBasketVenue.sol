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
    /// @notice Exact-input route leg, self-call only. Reports the actual
    /// received wallet balance change, including BUCK's carried demurrage.
    /// A partial spend reverts so no intermediate inventory is left unbooked.
    function arbSwap(address pool, address input, address output, uint256 amount)
        external returns (uint256 received);
    /// @notice Equity basket registration, invoked only by its own governed
    ///         wrapper. Updates the shared constituent prefix; no LP/cash flows.
    function registerToken(address token, uint8 decimals, uint256 initialPriceInBuck,
                           uint256 weightBp, uint24 feeTier) external returns (address pool);

    // --- Value reads (loops live in the facet, not the shell) ------------- //

    /// @notice Controller process variable: Σ basketAmount·price (BUCK, 18-dec).
    function basketValueInBuck() external view returns (int256);

    // --- Market legs -------------------------------------------------------- //

    /// @notice One leg of a monetary operation in constituent `i`.
    ///         `sellBuck` swaps BUCK -> TOKEN (the issue side); otherwise
    ///         TOKEN -> BUCK (the absorb side).  Pure AMM mechanics: all
    ///         sizing, bounding, minting, burning and book-keeping stay in
    ///         the shell.  Returns what was actually spent and received.
    function monetaryLeg(uint256 i, bool sellBuck, uint256 amountIn)
        external returns (uint256 spent, uint256 received);

    // --- Equity primitives (BuckBasketEquity) -------------------------------- //
    //
    // The equity basket keeps its own books (the wallet, liquidityOf) and asks
    // the venue only to value, place, withdraw and swap.  Pool reads are for
    // prices; nothing here reads a balance.

    /// @notice A pool's marks: `liquidity` of the basket's full-range position
    ///         valued in BUCK (twice its BUCK side) at the TWAP and at the
    ///         higher and lower of spot and TWAP, and one whole TOKEN's price
    ///         in BUCK at the same three marks.
    struct Marks {
        uint256 posLow;
        uint256 posTwap;
        uint256 posHigh;
        uint256 pLow;
        uint256 pTwap;
        uint256 pHigh;
        uint256 depth;          // the pool's virtual BUCK reserve at the spot (all LPs')
    }

    function marks(uint256 i, uint128 liquidity) external view returns (Marks memory);

    /// @notice Most discounted eligible position and the slice replaceable within the issue budget.
    function refreshQuote(uint256 floorBp, uint256 topBp, uint256 budget)
        external view returns (uint256 index, uint128 liquidity, uint256 nominal);

    /// @notice Add the most full-range liquidity `tokenAmount` and `buckAmount`
    ///         (held by the basket) balance at the spot; report what was used.
    function positionMint(uint256 i, uint256 tokenAmount, uint256 buckAmount)
        external returns (uint128 liquidity, uint256 tokenUsed, uint256 buckUsed);

    /// @notice Remove `liquidity` and collect exactly the principal it releases.
    function positionBurn(uint256 i, uint128 liquidity)
        external returns (uint256 tokenOut, uint256 buckOut);

    /// @notice Collect everything the position is owed (its fees).
    function positionSync(uint256 i) external returns (uint256 tokenOut, uint256 buckOut);
}
