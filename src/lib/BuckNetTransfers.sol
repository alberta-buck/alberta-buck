// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../BN254.sol";
import {IdentityRegistry} from "../IdentityRegistry.sol";
import {BuckLedger} from "./BuckLedger.sol";

/// @notice Linked transfer path shared by ordinary and premium-refund flows.
/// @dev Executes with BuckNet's storage and identity. The token must enforce
///      its external authorization and reentrancy guard before calling.
library BuckNetTransfers {
    using BuckLedger for BuckLedger.Ledger;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event BuckTransferReceipt(address indexed from, address indexed to, uint256 amount,
                              bytes32 fromCipherHash, bytes32 toCipherHash);
    error BadAddress();
    error NotVerified();
    error UnauthorizedTransfer();

    function send(BuckLedger.Ledger storage ledger,
                  mapping(address => mapping(address => bytes32)) storage fragments,
                  IdentityRegistry identity, address from, address to, uint256 amount) public {
        identity.enableCarryingModeHook();
        if (from == address(0) || to == address(0)) revert BadAddress();
        if (!identity.isVerified(from) || !identity.isVerified(to)) revert NotVerified();
        bytes32 toHash = fragments[from][to];
        if (toHash == bytes32(0)) {
            if (!identity.isPublicIdentity(from)) revert UnauthorizedTransfer();
            toHash = _identityHash(identity, to);
        }
        bytes32 fromHash = fragments[to][from];
        if (fromHash == bytes32(0)) {
            if (!identity.isPublicIdentity(to)) revert UnauthorizedTransfer();
            fromHash = _identityHash(identity, from);
        }
        ledger.transfer(from, to, amount, identity.isCarrying(from), identity.isCarrying(to));
        emit Transfer(from, to, amount);
        emit BuckTransferReceipt(from, to, amount, fromHash, toHash);
    }

    function _identityHash(IdentityRegistry identity, address account) private view returns (bytes32) {
        BN254.G1Point memory pk = identity.pkOf(account);
        IdentityRegistry.ElGamalCT memory E = identity.ciphertextOf(account);
        return keccak256(abi.encode(pk.X, pk.Y, E.R.X, E.R.Y, E.C.X, E.C.Y));
    }
}
