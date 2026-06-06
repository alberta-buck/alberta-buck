// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IIdentityMembershipVerifier} from "./IIdentityMembershipVerifier.sol";

/// @title StubIdentityMembershipVerifier — always-accepts identity membership stub.
/// @notice For testing the Notes spend-path plumbing before the full G1-tie
///         membership circuit is deployed.  Governance wires the real verifier
///         via setIdentityMembershipVerifier when the circuit is ready.
contract StubIdentityMembershipVerifier is IIdentityMembershipVerifier {
    bool public enabled = true;

    function setEnabled(bool _enabled) external {
        enabled = _enabled;
    }

    function verifyMembership(
        bytes calldata /*proof*/,
        uint256 /*identityRoot*/,
        uint256 /*px*/,
        uint256 /*py*/
    ) external view returns (bool) {
        return enabled;
    }
}
