// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IMintVerifier} from "./IMintVerifier.sol";

/// @title StubMintVerifier — Phase 1 placeholder for the Notes mint verifier.
/// @notice Accepts any proof while the SNARK toolchain is being wired up.  The
///         `enabled` flag lets governance freeze mints (or tests exercise the
///         negative path) without redeploying.
///
/// @dev    DO NOT use in production.  This contract performs zero cryptographic
///         checks; a real verifier is required before Notes mint becomes a
///         security-sensitive operation.
contract StubMintVerifier is IMintVerifier {

    address public governance;
    bool    public enabled;

    event GovernanceTransferred(address indexed previous, address indexed next);
    event EnabledSet(bool enabled);

    constructor(address _governance) {
        require(_governance != address(0), "governance=0");
        governance = _governance;
        enabled    = true;
        emit GovernanceTransferred(address(0), _governance);
        emit EnabledSet(true);
    }

    function transferGovernance(address next) external {
        require(msg.sender == governance, "not governance");
        require(next != address(0),       "governance=0");
        emit GovernanceTransferred(governance, next);
        governance = next;
    }

    function setEnabled(bool _enabled) external {
        require(msg.sender == governance, "not governance");
        enabled = _enabled;
        emit EnabledSet(_enabled);
    }

    /// @inheritdoc IMintVerifier
    function verifyMint(
        bytes calldata /*proof*/,
        uint256 /*totalFace*/,
        uint256[] calldata /*commitments*/,
        address /*issuer*/
    ) external view returns (bool) {
        return enabled;
    }
}
