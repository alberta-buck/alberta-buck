// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckCash} from "./BuckCash.sol";
import {BuckTransfer} from "./BuckTransfer.sol";
import {BuckFacilities, IBuckFund} from "./BuckFacilities.sol";
import {BuckRenewal, BuckRenewalConsent} from "../BuckRenewalConsent.sol";

/// @notice Atomic same-insurer renewal using Buck's corrected cash accounting,
///         and the owner's release of coverage that no principal needs.
/// @dev The host authenticates Jubilee and holds its global lock. Owner and
///      insurer consent bind the exact old terms/principal. Relief consumes
///      the same block budget as reductions and deferred burns. Old premium
///      refund, new terms, additional issuance and full-value new premium are
///      one operation; no interim K shortfall is externally actionable.
///      Release lives here only because BuckFacilities is at the deployed
///      code-size limit; it retires no principal and consumes no relief.
library BuckRenewalOps {
    using BuckCash for BuckCash.Book;
    uint256 private constant Q = 1e36;
    uint256 private constant K_SCALE = 1e18;
    uint256 private constant T = 50 * (365 days + 6 hours);
    error SenderNotVerified(); error CarryingIssuerNotApproved(); error NotCarryingPool();
    error FacilityMatured(); error BadAuthorization(); error CreditExceeded();
    error PremiumRateTooHigh(); error InsufficientMintFunding(); error NoInsurerPool(); error InsurerPoolChanged();
    error BadAmount();
    event Transfer(address indexed from, address indexed to, uint256 amount);
    event FacilityRenewed(address indexed owner, uint256 indexed tokenId, uint256 relief,
                          uint256 additionalIssuance, uint256 principal, uint256 nominalFundSpentScaled);
    event FacilityReleased(address indexed owner, uint256 indexed tokenId, uint256 coverage, uint256 premiumRefund);

    struct Release { uint256 tokenId; uint256 coverage; uint256 refund; }
    struct ReleasePlan { Release[] releases; uint256 used; uint256 coverage; uint256 refund; }

    /// @notice Plan burn(0): for each selected facility in order, the largest
    ///         coverage whose release leaves its principal fully backed at k,
    ///         and the premium deposit refunded in the same proportion.
    /// @dev A facility with no principal releases all of its coverage. One
    ///      with principal keeps the least activated face whose present value
    ///      times k still covers that principal, so a facility at or beyond
    ///      its limit releases nothing and no shortfall is created or made
    ///      worse. The refund is premium * released / active, rounded down:
    ///      the deposit keeps its ratio to the coverage that remains, which
    ///      keeps a later reduction's proportional refund consistent. A
    ///      facility whose refund its original pool cannot pay now (identity,
    ///      allowance, nominal balance, or the owner's sponsor for the pool's
    ///      carried fee) is skipped whole: coverage is never released without
    ///      its deposit. Shared pools, the sponsor and a Carrying owner's own
    ///      pool are carried virtually through the batch, as in planBurn.
    ///      Principal, supply, claim age and relief are untouched.
    function planRelease(BuckCash.Book storage cash, BuckFacilities.State storage state,
                         mapping(address => mapping(address => uint256)) storage allowances,
                         BuckFacilities.Dependencies memory d, address owner, uint256[] memory ids, uint256 k)
        public view returns (ReleasePlan memory p)
    {
        _releaser(d, owner);
        BuckFacilities.checkSelection(d.credit, owner, ids);
        (uint256 ownerGross, uint256 ownerNet) = cash.grossAndNetOf(owner);
        address sponsor = cash.sponsor[owner];
        uint256 sponsorFunds;
        if (sponsor != address(0)) {
            uint256 available = cash.netOf(sponsor);
            if (available < ownerGross - ownerNet) {
                revert BuckCash.DemurrageSponsorUnderfunded(owner, sponsor, ownerGross - ownerNet, available);
            }
            sponsorFunds = available - (ownerGross - ownerNet);
        }
        p.releases = new Release[](ids.length);
        BuckFacilities.Pool[] memory pools = new BuckFacilities.Pool[](ids.length);
        uint256 poolCount;
        for (uint256 i; i < ids.length; ++i) {
            (uint256 coverage, uint256 refund) = _releasable(state, d, ids[i], k);
            if (coverage == 0) continue;
            if (refund != 0) {
                address account = state.facilityPool[ids[i]];
                if (account == address(0) || !d.identity.isVerified(account) || !d.identity.isCarrying(account)) continue;
                uint256 j;
                while (j < poolCount && pools[j].account != account) ++j;
                if (j == poolCount) {
                    (uint256 gross, uint256 net) = cash.grossAndNetOf(account);
                    pools[poolCount++] = BuckFacilities.Pool(account, gross, net, allowances[account][address(this)]);
                }
                BuckFacilities.Pool memory v = pools[j];
                bool own = account == owner;
                if (refund > v.allowance || refund > (own ? ownerGross : v.gross) / Q) continue;
                if (!own) {
                    uint256 delivered = Math.mulDiv(v.net, refund * Q, v.gross);
                    if (sponsor != address(0)) {
                        if (refund * Q - delivered > sponsorFunds) continue;
                        sponsorFunds -= refund * Q - delivered;
                    }
                    v.gross -= refund * Q;
                    v.net -= delivered;
                    ownerGross += refund * Q;
                }
                if (v.allowance != type(uint256).max) v.allowance -= refund;
            }
            p.releases[p.used++] = Release(ids[i], coverage, refund);
            p.coverage += coverage;
            p.refund += refund;
        }
    }

    /// @notice Execute burn(0) for the caller: release what planRelease finds
    ///         at a freshly computed K, or revert everything.
    /// @dev Preconditions: the host holds its guard and its bindings are
    ///      complete. Postconditions: each released facility's principal is
    ///      still within its K-scaled present value; the caller's balance has
    ///      not fallen (it rises by any refund received); supply, principal
    ///      and claim age are unchanged; K did not move during the call. The
    ///      fund's valuation wheel is neither read nor refreshed, so release
    ///      does not depend on it.
    function release(BuckCash.Book storage cash, BuckFacilities.State storage state,
                     mapping(address => mapping(address => uint256)) storage allowances,
                     mapping(address => mapping(address => bytes32)) storage fragments,
                     BuckFacilities.Dependencies memory d, uint256[] memory ids) public
    {
        _releaser(d, msg.sender);
        cash.settle(msg.sender);
        uint256 beforeBalance = cash.balanceOf(msg.sender);
        uint256 k = d.controller.compute();
        ReleasePlan memory p = planRelease(cash, state, allowances, d, msg.sender, ids, k);
        for (uint256 i; i < p.used; ++i) {
            Release memory r = p.releases[i];
            if (r.refund != 0) {
                BuckFacilities.refundPremium(cash, state, allowances, fragments, d.identity, msg.sender,
                                             r.tokenId, r.refund);
                state.premium[r.tokenId] -= r.refund;
            }
            d.credit.deactivateFromBuck(r.tokenId, msg.sender, r.coverage);
            (uint256 principal,) = d.credit.issuanceOf(r.tokenId);
            if (principal == 0) delete state.facilityPool[r.tokenId];
            else if (principal > Math.mulDiv(d.credit.currentValue(r.tokenId), k, K_SCALE)) revert CreditExceeded();
            emit FacilityReleased(msg.sender, r.tokenId, r.coverage, r.refund);
        }
        if (cash.balanceOf(msg.sender) < beforeBalance) revert BadAmount();
        if (d.controller.currentBuckK() != k) revert CreditExceeded();
    }

    function _releaser(BuckFacilities.Dependencies memory d, address owner) private view {
        if (!d.identity.isVerified(owner)) revert SenderNotVerified();
        if (!d.identity.isVerified(d.jubilee) || !d.identity.isCarrying(d.jubilee)) revert NotCarryingPool();
        if (owner == d.jubilee) revert BadAmount();
    }

    function _releasable(BuckFacilities.State storage state, BuckFacilities.Dependencies memory d,
                         uint256 id, uint256 k) private view returns (uint256 coverage, uint256 refund)
    {
        (uint256 face, uint256 active,) = d.credit.creditInfo(id);
        if (active == 0) return (0, 0);
        (uint256 principal,) = d.credit.issuanceOf(id);
        uint256 keep;
        if (principal != 0) {
            uint256 mark = d.credit.depreciatedFaceValue(id);
            // At or beyond its limit nothing is excess. Below it, k and mark
            // are nonzero and the two ceilings cannot exceed the active face.
            if (principal >= Math.mulDiv(Math.mulDiv(mark, active, face), k, K_SCALE)) return (0, 0);
            keep = Math.mulDiv(Math.mulDiv(principal, K_SCALE, k, Math.Rounding.Ceil), face, mark,
                               Math.Rounding.Ceil);
            if (keep >= active) return (0, 0);
        }
        coverage = active - keep;
        refund = Math.mulDiv(state.premium[id], coverage, active);
    }

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
