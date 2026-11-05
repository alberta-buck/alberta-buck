// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckCash} from "./BuckCash.sol";
import {BuckTransfer} from "./BuckTransfer.sol";
import {BuckFacilities, IBuckFund} from "./BuckFacilities.sol";
import {BuckRenewal, BuckRenewalConsent} from "../BuckRenewalConsent.sol";

/// @notice Atomic same-insurer renewal using Buck's corrected cash accounting.
/// @dev The host authenticates Jubilee and holds its global lock. Owner and
///      insurer consent bind the exact old terms/principal. Relief consumes
///      the same block budget as reductions and deferred burns. Old premium
///      refund, new terms, additional issuance and full-value new premium are
///      one operation; no interim K shortfall is externally actionable.
library BuckRenewalOps {
    using BuckCash for BuckCash.Book;
    uint256 private constant Q = 1e36;
    uint256 private constant K_SCALE = 1e18;
    uint256 private constant T = 50 * (365 days + 6 hours);
    error SenderNotVerified(); error CarryingIssuerNotApproved(); error NotCarryingPool();
    error FacilityMatured(); error BadAuthorization(); error CreditExceeded();
    error PremiumRateTooHigh(); error InsufficientMintFunding(); error NoInsurerPool(); error InsurerPoolChanged();
    event Transfer(address indexed from, address indexed to, uint256 amount);
    event FacilityRenewed(address indexed owner, uint256 indexed tokenId, uint256 relief,
                          uint256 additionalIssuance, uint256 principal, uint256 nominalFundSpentScaled);

    function renew(BuckCash.Book storage cash, BuckFacilities.State storage state,
                   mapping(address => mapping(address => uint256)) storage allowances,
                   mapping(address => mapping(address => bytes32)) storage fragments,
                   BuckFacilities.Dependencies memory d, BuckRenewalConsent consent,
                   address owner, uint256 tokenId, BuckRenewal.Terms calldata terms, uint256 relief)
        public returns (uint256 refund, uint256 premium)
    {
        if (!d.identity.isVerified(owner)) revert SenderNotVerified();
        bool carrying = d.identity.isCarrying(owner);
        if (carrying && terms.additionalIssuance != 0 && !state.carryingIssuer[owner]) {
            revert CarryingIssuerNotApproved();
        }
        cash.settle(owner);
        if (!d.identity.isVerified(d.jubilee) || !d.identity.isCarrying(d.jubilee)) revert NotCarryingPool();
        (uint256 oldPrincipal, bytes32 digest) = consent.checkRenewal(owner, tokenId, terms);
        (, uint256 oldSeconds) = d.credit.issuanceOf(tokenId);
        if (oldPrincipal != 0 && oldSeconds == oldPrincipal * T) revert FacilityMatured();
        if (relief < terms.minimumRelief || relief > oldPrincipal) revert BadAuthorization();
        cash.consumeRelief(relief * Q);
        d.claims.totalSeconds();
        uint256 k = d.controller.compute();
        uint256 factor = d.controller.fundingFactor();
        uint256 oldLimit = Math.mulDiv(d.credit.currentValue(tokenId), k, K_SCALE);
        uint256 oldShortfall = oldPrincipal > oldLimit ? oldPrincipal - oldLimit : 0;
        (, uint256 oldCoverage,) = d.credit.creditInfo(tokenId);
        if (terms.activatedFace < oldCoverage) revert CreditExceeded();

        refund = state.premium[tokenId];
        BuckFacilities.refundPremium(cash, state, allowances, fragments, d.identity, owner, tokenId, refund);
        consent.consume(tokenId, digest);
        delete state.authorization[tokenId];
        uint256 nominalSpent;
        if (relief != 0) {
            uint256 fundBefore = cash.balanceOf(d.jubilee);
            (, nominalSpent) = cash.retire(owner, relief, relief * Q, carrying);
            emit Transfer(d.jubilee, address(0), fundBefore - cash.balanceOf(d.jubilee));
        }
        // Reduce and checkpoint the old principal first. The credit then
        // applies replacement terms and coverage in one call; additional
        // principal is issued only after its final K bound is checked.
        d.credit.recordRenewalFromBuck(tokenId, owner, relief, 0);
        d.claims.sync(tokenId);
        d.credit.renewTermsFromBuck(tokenId, owner, terms.face, terms.floor, terms.depType,
                                     terms.depRate, terms.depStartAt, terms.premiumRate,
                                     terms.activatedFace - oldCoverage);
        uint256 value = d.credit.currentValue(tokenId);
        uint256 rate = uint256(terms.premiumRate) * 10;
        if (rate >= 10_000) revert PremiumRateTooHigh();
        premium = Math.mulDiv(value, rate, 10_000);
        if (premium > terms.maxPremium) revert BadAuthorization();
        if (premium != 0 && cash.balanceOf(owner) < Math.mulDiv(premium, factor, K_SCALE)) {
            revert InsufficientMintFunding();
        }
        uint256 newPrincipal = oldPrincipal - relief + terms.additionalIssuance;
        uint256 newLimit = Math.mulDiv(value, k, K_SCALE);
        uint256 newShortfall = newPrincipal > newLimit ? newPrincipal - newLimit : 0;
        if (newShortfall > oldShortfall) revert CreditExceeded();

        address pool;
        if (premium != 0) {
            pool = state.insurerPool[d.credit.insurerOf(tokenId)];
            if (pool == address(0)) revert NoInsurerPool();
            if (pool != terms.expectedPool) revert InsurerPoolChanged();
            if (!d.identity.isVerified(pool) || !d.identity.isCarrying(pool)) revert NotCarryingPool();
        }
        if (terms.additionalIssuance != 0) {
            cash.issue(owner, terms.additionalIssuance, carrying);
            d.credit.recordIssuanceFromBuck(tokenId, owner, terms.additionalIssuance);
            emit Transfer(address(0), owner, terms.additionalIssuance);
            d.claims.sync(tokenId);
        }
        state.premium[tokenId] = premium;
        state.facilityPool[tokenId] = pool;
        if (premium != 0) BuckTransfer.payFullValue(cash, fragments, d.identity, owner, pool, premium);
        emit FacilityRenewed(owner, tokenId, relief, terms.additionalIssuance, newPrincipal, nominalSpent);
        IBuckFund(d.jubilee).checkpointIssuance();
        if (d.controller.currentBuckK() != k || d.controller.fundingFactor() != factor) revert CreditExceeded();
    }
}
