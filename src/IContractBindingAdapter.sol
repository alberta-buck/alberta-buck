// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title Contract-binding adapter metadata.
/// @notice Governance approves adapters only after auditing the semantics of
///         the provenance contract and its binding-authority relationship.
interface IContractBindingAdapter {
    /// @notice The only IdentityRegistry this adapter may call.
    function registry() external view returns (address);

    /// @notice The factory or other contract whose provenance is recognized.
    function provenance() external view returns (address);

    /// @notice The identity currently authorized by the provenance contract.
    function bindingAuthority() external view returns (address);
}
