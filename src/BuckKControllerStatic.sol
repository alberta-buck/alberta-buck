// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title BuckKControllerStatic — Phase-0 simplified BUCK_K source.
/// @notice Holds a single governance-set value used as the BUCK_K stabilization factor.
///         Replaces the eventual on-chain PID + commodity-oracle controller for the
///         duration of identity-layer work.  Surface intentionally matches the eventual
///         BuckKController so callers can swap in the PID implementation without churn.
contract BuckKControllerStatic {

    uint256 public buckK;        // 18-decimal: 1e18 = 1.0 (neutral)
    address public governance;

    event BuckKSet(uint256 oldValue, uint256 newValue, address indexed by);
    event GovernanceTransferred(address indexed previous, address indexed next);

    constructor(uint256 _buckK, address _governance) {
        require(_buckK > 0, "buckK=0");
        require(_governance != address(0), "governance=0");
        buckK = _buckK;
        governance = _governance;
    }

    /// @notice View accessor — preferred surface for new callers.
    function currentBuckK() external view returns (uint256) {
        return buckK;
    }

    /// @notice State-changing accessor -- preserves source-compat with the
    ///         eventual PID controller, but performs no PID work in Phase 0.
    /// @dev    Non-view to match the IBuckK interface signature used by
    ///         Buck.sol (the dynamic controller's compute() writes state).
    function compute() external returns (uint256) {
        return buckK;
    }

    /// @notice Phase-0 stub: funding factor disabled (returns 0).  Buck.sol's
    ///         mint gate is `balance >= poolPrincipal * factor / 1e18`, so a
    ///         zero factor lets all mints through.  The dynamic PID
    ///         controller (BuckKController.fundingFactor) implements the real
    ///         counter-cyclical formula.
    function fundingFactor() external pure returns (uint256) {
        return 0;
    }

    /// @notice Governance updates the published BUCK_K value.
    function setBuckK(uint256 _buckK) external {
        require(msg.sender == governance, "not governance");
        require(_buckK > 0, "buckK=0");
        emit BuckKSet(buckK, _buckK, msg.sender);
        buckK = _buckK;
    }

    /// @notice Hand off governance to a new address.
    function transferGovernance(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0), "next=0");
        emit GovernanceTransferred(governance, next);
        governance = next;
    }
}
