// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckCredit} from "./BuckCredit.sol";
import {JubileeClaims} from "./lib/JubileeClaims.sol";

interface IFacilityPremiums {
    function facilityPool(uint256 tokenId) external view returns (address);
    function premiumPrincipal(uint256 tokenId) external view returns (uint256);
}

/// @notice Bounded aggregate of capped BuckCredit claims for one BuckNet.
/// @dev The token alone can synchronize a facility; this contract fetches its
///      principal and claimable seconds directly from its immutable credit
///      contract. Anyone can process matured entries in bounded batches.
///      Exact views and sync revert until a maturity backlog is cleared.
contract JubileeClaimBook {
    using JubileeClaims for JubileeClaims.Book;
    uint256 private constant MATURITY = 50 * (365 days + 6 hours);

    address public immutable buck;
    BuckCredit public immutable credit;
    JubileeClaims.Book private _book;
    /// @notice Matured BUCK issuance assumed by the fund and not yet burned.
    uint256 public deferredPrincipal;
    struct PremiumRefund { address owner; address pool; uint256 nominal; }
    /// @notice Nominal deposits owed by the original pool after lien release.
    mapping(uint256 => PremiumRefund) public premiumRefunds;
    event PrincipalDeferred(uint256 indexed tokenId, uint256 principal, uint256 totalDeferred);
    event DeferredPrincipalBurned(uint256 principal, uint256 totalDeferred);
    event PremiumRefundDeferred(uint256 indexed tokenId, address indexed owner, address indexed pool, uint256 nominal);
    event PremiumRefundClaimed(uint256 indexed tokenId, uint256 nominal);
    error NotBuck();
    error NotMature();
    error BadDeferredAmount();
    error BadPremiumRefund();

    constructor(address buck_, address credit_) {
        buck = buck_;
        credit = BuckCredit(credit_);
        _book.initialize();
    }

    function sync(uint256 tokenId) external {
        if (msg.sender != buck) revert NotBuck();
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(tokenId);
        _book.update(tokenId, principal, seconds_);
    }

    /// @notice Transfer a fully aged facility's contribution to Jubilee's
    ///         debt without decreasing the aggregate relief benchmark.
    /// @dev Only Buck may call this, atomically with recording the facility's
    ///      reduction and releasing its NFT. A backlog must be processed.
    function deferMatured(uint256 tokenId) external returns (uint256 principal) {
        if (msg.sender != buck) revert NotBuck();
        uint256 seconds_;
        (principal, seconds_) = credit.issuanceOf(tokenId);
        if (principal == 0 || seconds_ != principal * MATURITY
            || _book.positionSeconds(tokenId) != seconds_) revert NotMature();
        uint256 nominal = IFacilityPremiums(buck).premiumPrincipal(tokenId);
        if (nominal != 0) {
            address pool = IFacilityPremiums(buck).facilityPool(tokenId);
            if (pool == address(0)) revert BadPremiumRefund();
            address owner = credit.ownerOf(tokenId);
            premiumRefunds[tokenId] = PremiumRefund(owner, pool, nominal);
            emit PremiumRefundDeferred(tokenId, owner, pool, nominal);
        }
        _book.update(tokenId, 0, 0);
        deferredPrincipal += principal;
        emit PrincipalDeferred(tokenId, principal, deferredPrincipal);
    }

    /// @notice Record BUCK actually burned by the fund against its debt.
    /// @dev Buck must extinguish that amount from total supply atomically.
    function consumeDeferred(uint256 principal) external {
        if (msg.sender != buck) revert NotBuck();
        if (principal == 0 || principal > deferredPrincipal) revert BadDeferredAmount();
        deferredPrincipal -= principal;
        emit DeferredPrincipalBurned(principal, deferredPrincipal);
    }

    /// @notice Return and consume the recorded refund. Only Buck may call,
    ///         atomically with the original pool's nominal transfer.
    function consumePremiumRefund(uint256 tokenId) external returns (address owner, address pool, uint256 nominal) {
        if (msg.sender != buck) revert NotBuck();
        PremiumRefund memory refund = premiumRefunds[tokenId];
        if (refund.nominal == 0) revert BadPremiumRefund();
        delete premiumRefunds[tokenId];
        emit PremiumRefundClaimed(tokenId, refund.nominal);
        return (refund.owner, refund.pool, refund.nominal);
    }

    function process(uint256 maxSteps) external returns (uint256 processed, bool current) {
        return _book.process(maxSteps);
    }
    function totalSeconds() external view returns (uint256) {
        return _book.totalSeconds() + deferredPrincipal * MATURITY;
    }
    function pending() external view returns (bool) { return _book.pending(); }
    function nextMaturity() external view returns (uint256 tokenId, uint256 at) { return _book.nextMaturity(); }
    function positionSeconds(uint256 tokenId) external view returns (uint256) {
        return _book.positionSeconds(tokenId);
    }
}
