// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../BN254.sol";
import {IdentityRegistry} from "../IdentityRegistry.sol";

/// @notice The upkeep facet's entry points, as a wheel turns them: Daily
///         (kind 0), Sync(i) (kind 1) and Respond (kind 2). Reached through
///         the basket shell. Both existing upkeep facets also supply the
///         same setup operations; no extra initializer is deployed.
interface IBasketUpkeep {
    function wheelDue(uint8 kind, uint256 i) external view returns (bool);
    function wheelStep(uint8 kind, uint256 i) external returns (uint256 work);
    function initializeEquityCredit(address credit, uint256 face) external;
    function authorizeIdentityBinding(IdentityRegistry registry, address binder,
        BN254.G1Point calldata pk, IdentityRegistry.ElGamalCT calldata E) external;
    function revokeIdentityBinding(IdentityRegistry registry) external;

    error InvalidCreditModel();
    error InvalidCreditIdentity();
    error WrongCreditContract();
    error BindingSetupClosed();
}
