// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IBuckKController -- the reprimable BUCK_K controller surface.
///
/// @notice `Buck` meters supply against the *common* controller surface
///         `IBuckK` (`currentBuckK` / `compute` / `fundingFactor`, declared in
///         `Buck.sol`) that every variant -- Static, Direct, External --
///         implements.  `IBuckKController` is the richer surface that adds
///         `reprime()`, the dilution-discontinuity hook implemented only by the
///         reprimable variants (`BuckKControllerDirect`) and driven by
///         `BuckBasket` after `addConstituent`.
///
///         Consumers (e.g. `BuckBasket`) import this rather than re-declaring a
///         local interface.  A future consolidation may make this
///         `IBuckKController is IBuckK` once `IBuckK` is lifted out of
///         `Buck.sol` into a shared home.
interface IBuckKController {
    /// @notice Run the PID cycle if `dT` has elapsed, else return the cached
    ///         value.  Non-view to match the state-changing PID accessor.
    function compute() external returns (uint256);

    /// @notice Absorb a process-variable discontinuity (e.g. basket dilution
    ///         from `addConstituent`) without firing a one-cycle P/I spike.
    ///         Privileged to the bound BuckBasket.
    function reprime() external;
}
