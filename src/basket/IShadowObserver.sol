// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IShadowObserver -- what the shadow controller reads instead of the
///        basket (CARRY-CONVEXITY.org D4 and 6.4).
///
/// @notice The observer assembles K's process variable from the level-1
///         stabilizers' books:
///
///           shadowValueInBuck() = bvib + sum_i lambda_i * netInventory_i / D
///           shadowSaturation()  = max_i min(1, lambda_i * saturation_i)
///
///         with D the BUCK reserve of the basket pools.  BUCK absorbed under
///         the weak side (netInventory > 0) RAISES the shadow value -- bvib as
///         it would have been without the desk -- so K keeps tightening behind
///         a position the desk is holding; BUCK issued under the strong side
///         lowers it, so K restores the headroom the fast actuators spent.
///         With every lambda_i = 0 the shadow value IS basketValueInBuck(),
///         which is the lambda = gamma = 0 identity the controller tests
///         assert.
///
///         Named `shadowSaturation` rather than `saturation` because the ops
///         shell is BOTH the observer and (as the desk) an IStabilizer through
///         wave 3, and the two will share one address again in the monetary
///         Diamond; the selectors must not collide.
interface IShadowObserver {
    /// @notice 18-dec, like basketValueInBuck().
    function shadowValueInBuck() external view returns (int256);

    /// @notice 0..1e18, the lambda-weighted maximum of the stabilizers' saturation.
    function shadowSaturation() external view returns (uint256);
}
