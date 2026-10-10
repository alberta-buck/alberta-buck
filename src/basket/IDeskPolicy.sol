// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice What the desk asks of its policy: the effort it advises (bp of
///         the desk's size basis; negative sells BUCKs), whether credit may be
///         drawn for it, the epoch that bounds one operation, and whether an
///         operation just executed may stand.
interface IDeskPolicy {
    function monetaryEffort() external view returns (int32 effortBp, bool outright);
    function epochNow() external view returns (uint32);
    function executionAllowed(int32 effort) external view returns (bool);
}
