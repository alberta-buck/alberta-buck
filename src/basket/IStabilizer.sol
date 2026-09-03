// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IStabilizer -- the level-1 stabilizer seam (CARRY-CONVEXITY.org 6.4).
///
/// @notice Every fast actuator that takes POSITIONS against bvib -- the
///         monetary desk (Q1/Q3 today), the convertibility undertakings and
///         their ladder, the facility population, the funding house -- reports
///         three numbers upward to the observer, and nothing else.  K consumes
///         the fast actuators' positions and their saturation, never their
///         filtered signals: a stabilizer's own smoothed view of bvib is
///         something K already integrates, so feeding it would double-count
///         (and the ops doc's ladder scan showed a lagged reading INVERTS the
///         outright quadrants past 80 days).  Positions carry the information
///         without the lag.
///
///         SIGNS AND UNITS
///         ---------------
///         netInventory  BUCK ABSORBED (weak side: bought and held, facility
///                       retire) counts POSITIVE; BUCK ISSUED (strong side:
///                       minted and sold, facility issue) counts NEGATIVE.  In
///                       BUCK's own native units (6-dec in the simulation), the
///                       same units the observer's reference depth D is in, so
///                       lambda * netInventory / D is dimensionless.
///         capacity      remaining room, 1e18 = all of it: rho for the ladder,
///                       C_leg room for the strong-side convenience path,
///                       headroom for the facility, OI room for the house, the
///                       smaller of the position / outright fractions for the
///                       desk.
///         saturation    0..1e18, the degree to which the stabilizer is at a
///                       bound; 1e18 once any bound has been hit.  The
///                       controller's gain scheduling reads this so that when
///                       the fast actuators are pinned the structural lever
///                       (K) moves faster -- the ops doc's "escalate on
///                       inventory, because inventory is the one signal the
///                       operator's own action cannot suppress", generalized
///                       to the whole level.
interface IStabilizer {
    /// @notice absorbed - issued, BUCK native units (6-dec in the sim).
    function netInventory() external view returns (int256);

    /// @notice Remaining room under the stabilizer's bounds, 1e18 = all.
    function capacity() external view returns (uint256);

    /// @notice 0..1e18, degree at a bound (1e18 once a bound has been hit).
    function saturation() external view returns (uint256);
}
