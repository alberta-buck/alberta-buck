// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../BN254.sol";
import {IdentityRegistry} from "../IdentityRegistry.sol";
import {BuckCash} from "./BuckCash.sol";

/// @notice Buck's original identity dispatcher over the corrected cash book.
/// @dev Preconditions: the host holds its global lock and has consumed any
///      required allowance. Postconditions: verified counterparties and their
///      receipt fragments authorize the transfer; nominal and actual pool
///      delivery are recorded separately; supply and NFT debt are unchanged.
library BuckTransfer {
    using BuckCash for BuckCash.Book;
    error SenderNotVerified();
    error RecipientNotVerified();
    error SenderApprovalRequired();
    error RecipientApprovalRequired();

    event Transfer(address indexed from, address indexed to, uint256 amount);
    event NetValueTransfer(address indexed from, address indexed to, uint256 nominal, uint256 netScaled);
    event BuckTransferReceipt(address indexed from, address indexed to, uint256 amount,
                              bytes32 fromCipherHash, bytes32 toCipherHash);
    event FullValuePayment(address indexed from, address indexed to, uint256 netRaw, uint256 nominalConsumedScaled);

    /// @dev Preserve Buck's four identity quadrants. Each private party must
    ///      CP-approve its counterparty; public identity permits the registered
    ///      credential hash as fallback. Approval of a router alone never
    ///      supplies the counterparty receipt. The inherited Carrying fee can
    ///      be funded by a sponsor; its separate event is not pool delivery.
    function send(BuckCash.Book storage cash,
                  mapping(address => mapping(address => bytes32)) storage fragments,
                  IdentityRegistry identity, address from, address to, uint256 amount)
        public returns (uint256 deliveredScaled)
    {
        (bytes32 fromHash, bytes32 toHash) = check(fragments, identity, from, to);
        deliveredScaled = cash.move(from, to, amount, identity.isCarrying(from), identity.isCarrying(to));
        emit Transfer(from, to, amount);
        emit NetValueTransfer(from, to, amount, deliveredScaled);
        emit BuckTransferReceipt(from, to, amount, fromHash, toHash);
    }

    /// @dev A facility owner's ordinary draw, or the authenticated pool payment
    /// funded by that facility. BuckCash excludes the Jubilee from ordinary issuance.
    function issueSpendable(BuckCash.Book storage cash,
                            mapping(address => mapping(address => bytes32)) storage fragments,
                            IdentityRegistry identity, address destination, uint256 amount, bool carrying) public
    {
        if (destination == address(0)) {
            cash.issue(msg.sender, amount, carrying);
            emit Transfer(address(0), msg.sender, amount);
            emit NetValueTransfer(address(0), msg.sender, amount, amount * 1e36);
        } else {
            if (!identity.isCarrying(destination)) revert NotCarryingPool();
            issueToPool(cash, fragments, identity, msg.sender, destination, amount);
        }
    }

    error NotCarryingPool();

    /// @notice Finance a premium directly into its original pool at full net
    ///         value, before dilution into any old Carrying issuer balance.
    /// @dev This is issuance on the owner's facility, not a nominal debit of
    ///      its old fee-bearing cash. The zero-address mint event records the
    ///      actual recipient; the identity receipt authorizes owner/pool and
    ///      FacilityIssued attributes the premium to the owner's NFT.
    function issueToPool(BuckCash.Book storage cash,
                     mapping(address => mapping(address => bytes32)) storage fragments,
                     IdentityRegistry identity, address owner, address pool, uint256 amount) public
    {
        (bytes32 fromHash, bytes32 toHash) = check(fragments, identity, owner, pool);
        cash.issue(pool, amount, true);
        emit Transfer(address(0), pool, amount);
        emit NetValueTransfer(address(0), pool, amount, amount * 1e36);
        emit BuckTransferReceipt(owner, pool, amount, fromHash, toHash);
    }

    function check(mapping(address => mapping(address => bytes32)) storage fragments,
                   IdentityRegistry identity, address from, address to)
        internal view returns (bytes32 fromHash, bytes32 toHash)
    {
        if (!identity.isVerified(from)) revert SenderNotVerified();
        if (!identity.isVerified(to)) revert RecipientNotVerified();
        toHash = fragments[from][to];
        if (toHash == bytes32(0)) {
            if (!identity.isPublicIdentity(from)) revert SenderApprovalRequired();
            toHash = _identityHash(identity, to);
        }
        fromHash = fragments[to][from];
        if (fromHash == bytes32(0)) {
            if (!identity.isPublicIdentity(to)) revert RecipientApprovalRequired();
            fromHash = _identityHash(identity, from);
        }
    }

    /// @dev A protocol payment explicitly pays the sender's carried fees.
    ///      FullValuePayment records that basis consumption; it must not be
    ///      mistaken for the stable nominal semantics of ERC-20 transfer.
    function payFullValue(BuckCash.Book storage cash,
                          mapping(address => mapping(address => bytes32)) storage fragments,
                          IdentityRegistry identity, address from, address to, uint256 raw) public
    {
        (bytes32 fromHash, bytes32 toHash) = check(fragments, identity, from, to);
        if (from == to) return; // Own-pool renewal needs no movement.
        uint256 consumed = cash.payFullValue(from, to, raw, identity.isCarrying(from), identity.isCarrying(to));
        emit Transfer(from, to, raw);
        emit NetValueTransfer(from, to, raw, raw * 1e36);
        emit FullValuePayment(from, to, raw, consumed);
        emit BuckTransferReceipt(from, to, raw, fromHash, toHash);
    }

    function _identityHash(IdentityRegistry identity, address account) private view returns (bytes32) {
        BN254.G1Point memory pk = identity.pkOf(account);
        IdentityRegistry.ElGamalCT memory E = identity.ciphertextOf(account);
        return keccak256(abi.encode(pk.X, pk.Y, E.R.X, E.R.Y, E.C.X, E.C.Y));
    }
}
