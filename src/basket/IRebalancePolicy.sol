// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IPairSignals} from "./IPairSignals.sol";

/// @notice What the upkeep facet asks of its rebalancing policy, and what it
///         reports back. The policy owns no assets and authorizes no spending:
///         the basket supplies its gaps and enforces its own caps.
interface IRebalancePolicy {
    function basket() external view returns (address);
    function count() external view returns (uint256);
    function signals() external view returns (IPairSignals);
    function observe() external;

    /// @notice Whether a gap may be worked at all: bit i is set once
    ///         constituent i has built its full patience. A policy with no
    ///         patience leaves that to the basket's fixed band.
    function patience() external view returns (uint256);
    function urgent(int256[] calldata gaps) external view returns (uint256 mask);

    /// @notice How fast to close an eligible gap from a to b, as a fraction
    ///         of it per day: ln 2 halves what remains each day.
    function desire(uint256 a, uint256 b) external view returns (uint256 rateWad);

    /// @notice The share of a donor's gap to move now: 1 - exp(-allowance),
    ///         its allowance being the rate it has been asked at, summed over
    ///         time since it last transferred and carried to now at `rateWad`.
    ///         However often it is asked, the same time releases the same share.
    function share(uint256 donor, uint256 rateWad) external view returns (uint256);
    function lastExecutionAt() external view returns (uint64);

    // Reports from the basket alone.
    function recordGaps(int256[] calldata gaps) external;
    function recordRates(uint256[] calldata rates) external;
    function recordImpact(uint256 i, int24 beforeTick, int24 afterTick) external;
    function recordExecution(uint256 donor) external;
}
