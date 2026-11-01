// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../BN254.sol";
import {IdentityRegistry} from "../IdentityRegistry.sol";
import {BuckLedger} from "./BuckLedger.sol";
import {BuckCredit} from "../BuckCredit.sol";
import {JubileeClaimBook} from "../JubileeClaimBook.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

interface IBuckNetIssuanceK {
    function compute() external returns (uint256);
    function fundingFactor() external view returns (uint256);
}

interface IBuckNetIssuanceReserveHook {
    function checkpointIssuance() external;
}

/// @notice Linked issuance and identity-checked transfers for BuckNet.
/// @dev Executes with BuckNet's storage and identity. The token must enforce
///      its external authorization and reentrancy guard before calling.
library BuckNetTokenOps {
    using BuckLedger for BuckLedger.Ledger;
    uint256 private constant K_SCALE = 1e18;
    uint256 private constant BP = 10_000;
    uint256 private constant POOL_ROI_INV = 10;
    uint256 private constant RELIEF_SECONDS = 50 * (365 days + 6 hours);

    struct IssueDependencies {
        BuckCredit credit;
        IBuckNetIssuanceK controller;
        IdentityRegistry identity;
        JubileeClaimBook claims;
        address jubilee;
    }

    struct IssueRequest {
        uint256 tokenId;
        uint256 coverage;
        uint256 spendable;
        uint256 maxPremium;
        address expectedPool;
    }

    event Transfer(address indexed from, address indexed to, uint256 value);
    /// @dev Full-value delivery in 1e36 fractional units per raw BUCK unit.
    event NetValueTransfer(address indexed from, address indexed to, uint256 nominal, uint256 netScaled);
    event BuckTransferReceipt(address indexed from, address indexed to, uint256 amount,
                              bytes32 fromCipherHash, bytes32 toCipherHash);
    event FacilityIssued(address indexed owner, uint256 indexed tokenId, uint256 coverage,
                         uint256 principal, uint256 premium, address pool);
    error BadAddress();
    error NotVerified();
    error UnauthorizedTransfer();
    error NotCarryingPool();
    error NotOwner();
    error BadAmount();
    error CreditExceeded();
    error BadPremiumRate();
    error PremiumLimitExceeded();
    error FundingInsufficient();
    error FacilityMatured();
    error NoInsurerPool();
    error InsurerPoolChanged();

    /// @dev Preconditions: the host holds its token-wide reentrancy guard,
    ///      passes its own ledger/maps and immutable dependencies, and the
    ///      caller owns the selected NFT. Identity, pool, maturity, current K,
    ///      funding, premium and balance bounds are checked below.
    ///      Postconditions: the NFT and aggregate supply gain the same
    ///      principal, the owner's issued value and the insurer's full-value
    ///      premium match that principal, and the claim book and fund are
    ///      checkpointed. The identity receipt and all token events are emitted
    ///      from the host. Any failure reverts every participant atomically.
    function issue(BuckLedger.Ledger storage ledger,
                   mapping(uint256 => address) storage facilityPool,
                   mapping(uint256 => uint256) storage premiumPrincipal,
                   mapping(address => address) storage insurerPool,
                   mapping(address => mapping(address => bytes32)) storage fragments,
                   IssueDependencies memory d, IssueRequest memory r)
        public returns (uint256 principal, uint256 premium)
    {
        d.identity.enableCarryingModeHook();
        if (!d.identity.isVerified(msg.sender) || !d.identity.isVerified(d.jubilee)) revert NotVerified();
        if (d.identity.isCarrying(msg.sender) || !d.identity.isCarrying(d.jubilee)) revert NotCarryingPool();
        if (d.credit.ownerOf(r.tokenId) != msg.sender) revert NotOwner();
        if (r.spendable == 0) revert BadAmount();
        d.claims.totalSeconds();
        uint256 k = d.controller.compute();
        uint256 preFunding = ledger.balanceOf(msg.sender);
        uint256 beforeValue = d.credit.currentValue(r.tokenId);
        if (r.coverage != 0) d.credit.activateFromBuck(r.tokenId, msg.sender, r.coverage);
        uint256 afterValue = d.credit.currentValue(r.tokenId);
        if (afterValue < beforeValue) revert CreditExceeded();
        (,, uint32 premiumRate) = d.credit.creditInfo(r.tokenId);
        uint256 effectiveRate = uint256(premiumRate) * POOL_ROI_INV;
        if (effectiveRate >= BP) revert BadPremiumRate();
        premium = Math.mulDiv(afterValue - beforeValue, effectiveRate, BP);
        if (premium > r.maxPremium) revert PremiumLimitExceeded();
        uint256 factor = d.controller.fundingFactor();
        if (premium != 0 && preFunding < Math.mulDiv(premium, factor, K_SCALE)) revert FundingInsufficient();
        principal = r.spendable + premium;
        (uint256 oldPrincipal, uint256 oldSeconds) = d.credit.issuanceOf(r.tokenId);
        if (oldPrincipal != 0 && oldSeconds == oldPrincipal * RELIEF_SECONDS) revert FacilityMatured();
        if (oldPrincipal + principal > Math.mulDiv(afterValue, k, K_SCALE)) revert CreditExceeded();

        address pool;
        if (premium != 0) {
            pool = facilityPool[r.tokenId];
            if (pool == address(0)) {
                pool = insurerPool[d.credit.insurerOf(r.tokenId)];
                if (pool == address(0)) revert NoInsurerPool();
                facilityPool[r.tokenId] = pool;
            }
            if (pool != r.expectedPool) revert InsurerPoolChanged();
            if (!d.identity.isVerified(pool) || !d.identity.isCarrying(pool)) revert NotCarryingPool();
        }
        d.credit.recordIssuanceFromBuck(r.tokenId, msg.sender, principal);
        d.claims.sync(r.tokenId);
        ledger.issue(msg.sender, principal, false);
        emit Transfer(address(0), msg.sender, principal);
        if (premium != 0) {
            premiumPrincipal[r.tokenId] += premium;
            send(ledger, fragments, d.identity, msg.sender, pool, premium);
        }
        emit FacilityIssued(msg.sender, r.tokenId, r.coverage, principal, premium, pool);
        IBuckNetIssuanceReserveHook(d.jubilee).checkpointIssuance();
    }

    /// @dev The host has authenticated any allowance and holds its guard.
    ///      Verified parties and their receipt fragments are checked here;
    ///      the transfer preserves NFT principal and total issued supply.
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
        uint256 net = ledger.transfer(from, to, amount, identity.isCarrying(from), identity.isCarrying(to));
        emit Transfer(from, to, amount);
        emit NetValueTransfer(from, to, amount, net);
        emit BuckTransferReceipt(from, to, amount, fromHash, toHash);
    }

    function _identityHash(IdentityRegistry identity, address account) private view returns (bytes32) {
        BN254.G1Point memory pk = identity.pkOf(account);
        IdentityRegistry.ElGamalCT memory E = identity.ciphertextOf(account);
        return keccak256(abi.encode(pk.X, pk.Y, E.R.X, E.R.Y, E.C.X, E.C.Y));
    }
}
