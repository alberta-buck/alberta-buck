// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IMintVerifierA2} from "./IMintVerifierA2.sol";

/// @title StubMintVerifierA2 -- placeholder for the A2 (private-issuer) mint
///        verifier.  Accepts any proof; used by the contract-gate tests that
///        exercise the issuerMode / binding logic without a real A2 Groth16
///        proof.  Mirrors StubMintVerifier.
///
/// @dev    DO NOT use in production -- performs zero cryptographic checks, so it
///         does NOT enforce the eIss leaf-tie (a real MintBatchA2N*Groth16Verifier
///         does).
contract StubMintVerifierA2 is IMintVerifierA2 {

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

    /// @inheritdoc IMintVerifierA2
    function verifyMint(
        bytes calldata /*proof*/,
        uint256[4][] calldata /*eIss*/,
        uint256 /*oldRoot*/,
        uint256 /*newRoot*/,
        uint256 /*nextLeafIndex*/,
        uint256 /*totalFace*/,
        uint256[] calldata /*commitments*/
    ) external view returns (bool) {
        return enabled;
    }
}
