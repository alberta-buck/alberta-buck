// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @dev The slice of BuckBasketProRata a rebalance director reads (public
///      getters only; the director holds no funds and no basket authority).
interface IBasketMeta {
    function constituents(uint256 i) external view returns (
        address token, uint8 decimals, uint256 basketAmount,
        uint256 initialPriceInBuck, uint24 feeTier, address pool,
        int24 tickLower, int24 tickUpper, bool buckIsToken0,
        uint256 targetWeightBp, uint128 treasuryLiquidity);
    function constituentsLength() external view returns (uint256);
    function buck() external view returns (address);
}

/// @title IRebalanceDirector -- the advisory surface a BuckBasket (and its
///        keepers / the sim's DirectorKeeperAgent) consumes.
///
/// @notice Implementations are lazily-advanced signal state machines: anyone
///         may `poke` with a bounded work budget; the aggregated advice is
///         read through `depositHint` / `redeemHint` / `effortOf`.  Hints are
///         advisory only -- the basket's venue re-verifies all execution.
interface IRebalanceDirector {
    // --- Lifecycle --------------------------------------------------------- //
    function syncConstituents() external;
    function constituentCount() external view returns (uint256);

    // --- The work wheel ---------------------------------------------------- //
    function poke(uint256 maxWork) external returns (uint256 advanced);
    function pokeAll() external returns (uint256 advanced);
    function pending() external view returns (uint256 stale);
    function epochNow() external view returns (uint32);

    // --- Advisory reads ---------------------------------------------------- //
    /// @notice Signed per-epoch effort in bp of NAV: positive = the pool
    ///         wants funds (buy/deposit side), negative = draw it down.
    function effortOf(uint256 i) external view returns (int256);
    function effortsAll() external view returns (int256[] memory);
    /// @notice Best pool for incoming BUCK; type(uint256).max = no signal.
    function depositHint() external view returns (uint256);
    /// @notice Best pool to draw a redemption from; type(uint256).max = none.
    function redeemHint() external view returns (uint256);
    /// @notice Current raw deviation of constituent i (diagnostic).
    function deviationOf(uint256 i) external view returns (int256);
}
