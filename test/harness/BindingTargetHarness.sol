// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../../src/BN254.sol";
import {IdentityRegistry} from "../../src/IdentityRegistry.sol";

/// @notice Controller-authorized target used by the real-EVM binding review.
contract BindingTargetHarness {
    address public immutable controller;

    constructor() {
        controller = msg.sender;
    }

    function authorizeIdentityBinding(
        IdentityRegistry registry,
        address binder,
        BN254.G1Point calldata pk,
        IdentityRegistry.ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external {
        require(msg.sender == controller, "not controller");
        registry.authorizeContractBinding(
            binder, pk, E, isPublicIdentity_, isCarrying_
        );
    }
}
