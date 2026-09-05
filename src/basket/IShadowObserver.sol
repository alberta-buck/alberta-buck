// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IShadowObserver -- what the shadow controller reads beside the
///        basket (CARRY-CONVEXITY.org D4, D7 and 6.4; WAVE3.org WP-13).
///
/// @notice The observer assembles K's AGGREGATE POSITION s from the level-1
///         stabilizers' `IStabilizer` books, in one of two governance-set
///         units (D7):
///
///           S (shadow; D4 in its own units)
///             s = sum_i lambda_i * q_i / D          dimensionless, price
///                                                   units; D the pools'
///                                                   BUCK reserve
///           V (position vector)
///             s = sum_i w_i * q_i / cap_i           the level's cost-
///                 / sum_i w_i                       weighted fill, in
///                                                   [-1, 1]; no D
///
///         with q_i = netInventory_i (BUCK absorbed positive, issued
///         negative), cap_i the stabilizer's HELD inventory bound, and both
///         sums over the INCLUDED stabilizers (held cap > 0).  Absorbed
///         inventory makes s positive, and the controller's position loop
///         drives K DOWN on a positive s -- the contraction the absorption
///         was betting on (7.3); issued inventory drives K up.
///
///         The controller calls `observe()` once per cycle: the observer
///         refreshes every held cap through the stabilizer's `positionCap()`
///         inside try/catch -- a revert keeps the held cap and flags the
///         stabilizer STALE, a zero EXCLUDES it and renormalizes the weights
///         (WAVE3.org decision 9) -- and returns s.  `aggregatePosition()`
///         is the same number as a view (telemetry).  `shadowValueInBuck()`
///         = bvib + s_S is kept for compatibility and telemetry: D4's
///         composite, which the controller no longer integrates (decision
///         10: the price loop reads the raw basket).  `shadowSaturation()`
///         is the held-cap saturation gamma's optional schedule keys off;
///         `flags()` the stale / excluded summary for the R14 attribution
///         panel (bit i = the i-th registered stabilizer; bit 255 = the
///         sim-only pseudo-stabilizer).
interface IShadowObserver {
    /// @notice Refresh the held caps, then return the aggregate position
    ///         (1e18; absorbed positive).  Permissionless.
    function observe() external returns (int256 s);

    /// @notice The aggregate position under the current mode, from the held
    ///         caps as they stand (1e18; absorbed positive).
    function aggregatePosition() external view returns (int256);

    /// @notice bvib + s_S, 18-dec like basketValueInBuck() (D4's composite).
    function shadowValueInBuck() external view returns (int256);

    /// @notice 0..1e18: the weighted maximum over the included stabilizers
    ///         of min(1, |q_i| / heldCap_i).
    function shadowSaturation() external view returns (uint256);

    /// @notice Stale / excluded bitmasks over the registry (bit 255 = the
    ///         pseudo-stabilizer).
    function flags() external view returns (uint256 staleMask, uint256 excludedMask);
}
