// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice The upkeep facet's entry points, as a wheel turns them: Daily
///         (kind 0), Sync(i) (kind 1) and Respond (kind 2). Reached through
///         the basket shell, which delegates these two selectors to the facet.
interface IBasketUpkeep {
    function wheelDue(uint8 kind, uint256 i) external view returns (bool);
    function wheelStep(uint8 kind, uint256 i) external returns (uint256 work);
}
