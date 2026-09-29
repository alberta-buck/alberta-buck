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
    /// @notice One leg of a monetary operation in constituent `i`.
    ///         `sellBuck` swaps BUCK -> TOKEN (the issue side); otherwise
    ///         TOKEN -> BUCK (the absorb side).  Pure AMM mechanics: all
    ///         sizing, bounding, minting, burning and book-keeping stay in
    ///         the shell.  Returns what was actually spent and received.
    function monetaryLeg(uint256 i, bool sellBuck, uint256 amountIn)
        external returns (uint256 spent, uint256 received);

    // --- Fence primitives (BuckBasketFence) ------------------------------ //
    //
    // All take the pool and range EXPLICITLY rather than reading the
    // constituent's stored tick range, because a fence is a ladder of
    // arbitrary concentrated positions in a pool the constituent record does
    // not name -- a different fee tier on the same TOKEN/BUCK pair.  The V3
    // mint/swap callbacks already resolve the constituent from the encoded
    // token rather than from the pool address, so they serve any fee tier of
    // that pair unchanged.

    /// @notice Find or create + initialize the fence pool for `token` at
    ///         `feeTier`, and report its tick spacing.
    function fencePool(address token, uint8 decimals, uint256 initialPriceInBuck,
                       uint24 feeTier)
        external returns (address pool, int24 spacing, bool buckIsToken0);

    /// @notice Mint `liquidity` over [lo,hi] in `pool`, paying from the
    ///         basket's own balances.
    function fenceMint(address token, address pool, int24 lo, int24 hi,
                       uint128 liquidity) external returns (uint256 a0, uint256 a1);

    /// @notice Burn `liquidity` over [lo,hi] and collect everything owed --
    ///         principal AND accrued fees, which is where the harvest comes
    ///         from.  `burn` only credits; `collect` is what moves it.
    function fenceBurn(address pool, int24 lo, int24 hi, uint128 liquidity)
        external returns (uint256 a0, uint256 a1);

    /// @notice Swap in the fence pool: TOKEN -> BUCK or BUCK -> TOKEN.
    function fenceSwap(address token, address pool, bool sellBuck, uint256 amountIn)
        external returns (uint256 spent, uint256 received);

    /// @notice Exact contents of a band at the live price, and the TWAP /
    ///         liquidity math around it.  These live on the FACET purely for
    ///         bytecode budget: the UniswapV3OracleLib arithmetic they inline
    ///         put BuckBasketFence 593 bytes over EIP-170, which neither
    ///         forge nor pyrevm enforces -- so it would have shipped as a
    ///         contract that simply cannot be deployed.  Stateless on purpose
    ///         (pool and range passed in) so no fence state has to move into
    ///         the shared storage layout.
    function fenceQuote(address pool, int24 lo, int24 hi, uint128 liquidity,
                        bool buckIsToken0)
        external view returns (uint256 buckAmt, uint256 tokAmt);

    function fenceTwap(address pool, address token, uint8 decimals,
                       uint32 secondsAgo) external view returns (uint256 priceInBuck);

    function fenceLiquidityFor(address pool, int24 lo, int24 hi,
                               uint256 amount0, uint256 amount1)
        external view returns (uint128);

    /// @notice Live pool state for range placement.
    function fenceState(address pool)
        external view returns (uint160 sqrtPriceX96, int24 tick, int24 spacing);

    function convertIntoBucks(uint256[] calldata tokenInventory, uint256 targetBuck)
        external returns (uint256 gained, uint256 lossValue, uint256[] memory inventoryAfter);

    // --- Equity primitives (BuckBasketEquity; doc/BASKET-EQUITY.org 13.7) --- //
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

    /// @notice Whether pool `i` has active liquidity to trade against.
    function poolLive(uint256 i) external view returns (bool);

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
