// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckTypes} from "../BuckTypes.sol";
import {BuckCredit} from "../BuckCredit.sol";
import {IdentityRegistry} from "../IdentityRegistry.sol";
import {JubileeClaimBook} from "../JubileeClaimBook.sol";
import {BuckCash} from "./BuckCash.sol";
import {BuckTransfer} from "./BuckTransfer.sol";
import {BuckNetMintMath} from "./BuckNetMintMath.sol";
import {BuckNetBurnMath} from "./BuckNetBurnMath.sol";

interface IBuckIssuanceK {
    function currentBuckK() external view returns (uint256);
    function compute() external returns (uint256);
    function fundingFactor() external view returns (uint256);
}

interface IBuckFund {
    function token() external view returns (address);
    function checkpointIssuance() external;
    function refreshBurnValuation() external returns (uint256);
    function reliefPositionMark() external view returns (uint256);
}

/// @notice Buck's credit allocation and principal settlement.
/// @dev The host owns identity consent, the global monetary lock and immutable
///      wiring. This library retains no signed wallet or transfer-triggered
///      issuance. Every successful issuance/reduction updates cash supply,
///      BuckCredit principal and the capped aggregate claim in one transaction.
library BuckFacilities {
    using BuckCash for BuckCash.Book;
    uint256 private constant Q = 1e36;
    uint256 private constant K_SCALE = 1e18;
    uint256 private constant T = 50 * (365 days + 6 hours);

    struct Authorization { uint256 principal; uint256 minimumReliefScaled; uint64 deadline; }
    struct State {
        mapping(address => address) insurerPool;
        mapping(address => address) poolInsurer;
        mapping(uint256 => address) facilityPool;
        mapping(uint256 => uint256) premium;
        mapping(uint256 => Authorization) authorization;
        mapping(address => bool) carryingIssuer;
    }
    struct Dependencies {
        BuckCredit credit;
        IBuckIssuanceK controller;
        IdentityRegistry identity;
        JubileeClaimBook claims;
        address jubilee;
    }
    struct Issue { uint256 tokenId; uint256 coverage; uint256 spendable; uint256 maxPremium; address expectedPool; }
    struct MintPlan {
        Issue[] issues;
        uint256 coverage;
        uint256 premium;
        uint256 spendable;
        uint256 used;
    }
    struct Slice { uint256 tokenId; BuckNetBurnMath.Plan reduction; }
    struct BurnPlan { Slice[] slices; uint256 coverage; uint256 refund; uint256 used; }
    struct Pool { address account; uint256 gross; uint256 net; uint256 allowance; }
    struct BurnState {
        uint256 gross;
        uint256 net;
        uint256 sponsorFunds;
        uint256 fundCash;
        uint256 fundMark;
        uint256 totalSeconds;
        uint256 budget;
        bool budgetStarted;
        bool carrying;
        bool sponsored;
        Pool[] pools;
        uint256 poolCount;
    }

    error SenderNotVerified();
    error NotCreditOwner();
    error BadAmount();
    error CreditExceeded();
    error InsufficientCreditAllocation();
    error InsufficientCoverage();
    error InsufficientMintFunding();
    error PremiumRateTooHigh();
    error PremiumLimitExceeded();
    error FacilityMatured();
    error NoInsurerPool();
    error InsurerPoolChanged();
    error NotCarryingPool();
    error CarryingIssuerNotApproved();
    error AllocationWorkLimit();
    error InsufficientAllowance();

    event Transfer(address indexed from, address indexed to, uint256 amount);
    event NetValueTransfer(address indexed from, address indexed to, uint256 nominal, uint256 netScaled);
    event FacilityIssued(address indexed owner, uint256 indexed tokenId, uint256 coverage,
                         uint256 principal, uint256 premium, address pool);
    event Minted(address indexed account, uint256 amount, uint256 premium, uint256 creditValue,
                 uint256 buckKValue, uint256 newLimit);
    event FacilityReduced(address indexed owner, uint256 indexed tokenId, uint256 principal,
                          uint256 reliefScaled, uint256 nominalFundSpentScaled, uint256 premiumRefund);
    event CashRetired(address indexed owner, uint256 principal, uint256 ownerNetScaled,
                      uint256 ownerNominalScaled, uint256 fundNetScaled, uint256 fundNominalScaled);

    /// @dev Preconditions: the host holds its guard and all immutable bindings
    ///      are complete. Approved Carrying services can issue; revocation
    ///      prevents new issuance without blocking retirement. The premium is
    ///      freshly issued directly into the insurer pool, at full net value.
    ///      Postcondition: supply and this NFT gain spendable+premium, the pool
    ///      and owner receive that same cash, and all claim ages synchronize.
    function issue(BuckCash.Book storage cash, State storage state,
                   mapping(address => mapping(address => bytes32)) storage fragments,
                   Dependencies memory d, Issue memory r) public returns (uint256 principal, uint256 premium)
    {
        bool carrying = _owner(state, d, msg.sender, true);
        if (d.credit.ownerOf(r.tokenId) != msg.sender) revert NotCreditOwner();
        if (r.spendable == 0) revert BadAmount();
        d.claims.totalSeconds();
        cash.settle(msg.sender);
        uint256 beforeBalance = cash.balanceOf(msg.sender);
        uint256 k = d.controller.compute();
        (uint256 oldPrincipal, uint256 age) = d.credit.issuanceOf(r.tokenId);
        if (oldPrincipal != 0 && age == oldPrincipal * T) revert FacilityMatured();
        uint256 beforeValue = d.credit.currentValue(r.tokenId);
        if (r.coverage != 0) d.credit.activateFromBuck(r.tokenId, msg.sender, r.coverage);
        uint256 afterValue = d.credit.currentValue(r.tokenId);
        if (afterValue < beforeValue) revert CreditExceeded();
        (,, uint32 rate) = d.credit.creditInfo(r.tokenId);
        uint256 effective = uint256(rate) * 10;
        if (effective >= 10_000) revert PremiumRateTooHigh();
        premium = Math.mulDiv(afterValue - beforeValue, effective, 10_000);
        if (premium > r.maxPremium) revert PremiumLimitExceeded();
        if (beforeBalance < Math.mulDiv(premium, d.controller.fundingFactor(), K_SCALE)) revert InsufficientMintFunding();
        principal = r.spendable + premium;
        if (oldPrincipal + principal > Math.mulDiv(afterValue, k, K_SCALE)) revert CreditExceeded();
        address pool;
        if (premium != 0) {
            pool = state.facilityPool[r.tokenId];
            if (pool == address(0)) pool = state.insurerPool[d.credit.insurerOf(r.tokenId)];
            if (pool == address(0)) revert NoInsurerPool();
            if (pool != r.expectedPool) revert InsurerPoolChanged();
            _pool(d, pool);
            state.facilityPool[r.tokenId] = pool;
        }
        d.credit.recordIssuanceFromBuck(r.tokenId, msg.sender, principal);
        d.claims.sync(r.tokenId);
        cash.issue(msg.sender, r.spendable, carrying);
        emit Transfer(address(0), msg.sender, r.spendable);
        emit NetValueTransfer(address(0), msg.sender, r.spendable, r.spendable * Q);
        if (premium != 0) {
            state.premium[r.tokenId] += premium;
            BuckTransfer.premium(cash, fragments, d.identity, msg.sender, pool, premium);
        }
        if (cash.balanceOf(msg.sender) < beforeBalance + r.spendable) revert BadAmount();
        emit FacilityIssued(msg.sender, r.tokenId, r.coverage, principal, premium, pool);
    }

    /// @notice Mint at least the requested balance increase, with one reserve
    ///         captured before the whole batch. Freshly minted proceeds never
    ///         satisfy that batch's own reserve. Any later failure rolls back
    ///         earlier facilities and all fund checkpoints.
    function mint(BuckCash.Book storage cash, State storage state,
                  mapping(address => mapping(address => bytes32)) storage fragments,
                  Dependencies memory d, uint256 amount, uint256[] memory ids) public
    {
        _owner(state, d, msg.sender, true);
        d.claims.totalSeconds();
        cash.settle(msg.sender);
        uint256 k = d.controller.compute();
        uint256 factor = d.controller.fundingFactor();
        uint256 beforeBalance = cash.balanceOf(msg.sender);
        uint256 beforeValue = d.credit.totalCurrentValue(msg.sender);
        MintPlan memory p = planMint(cash, state, d, amount, ids, k);
        if (beforeBalance < Math.mulDiv(p.premium, factor, K_SCALE)) revert InsufficientMintFunding();
        for (uint256 i; i < p.used; ++i) {
            (, uint256 paid) = issue(cash, state, fragments, d, p.issues[i]);
            if (paid != p.issues[i].maxPremium || d.controller.currentBuckK() != k) revert CreditExceeded();
            if (d.controller.fundingFactor() != factor) revert InsufficientMintFunding();
        }
        if (cash.balanceOf(msg.sender) < beforeBalance + p.spendable) revert BadAmount();
        IBuckFund(d.jubilee).checkpointIssuance();
        if (d.controller.currentBuckK() != k || d.controller.fundingFactor() != factor) revert CreditExceeded();
        emit Minted(msg.sender, p.coverage, p.premium, beforeValue, k,
                    Math.mulDiv(d.credit.totalCurrentValue(msg.sender), k, K_SCALE));
    }

    function planMint(BuckCash.Book storage cash, State storage state, Dependencies memory d,
                      uint256 amount, uint256[] memory ids, uint256 k) public view returns (MintPlan memory p)
    {
        _owner(state, d, msg.sender, true);
        checkSelection(d.credit, msg.sender, ids);
        (uint256 gross,) = cash.grossAndNetOf(msg.sender);
        uint256 room = (BuckTypes.MAX_BALANCE * Q - gross) / Q;
        uint256 supplyRoom = type(uint256).max / Q - cash.issued;
        p.issues = new Issue[](ids.length);
        bool maximum = amount == type(uint256).max;
        for (uint256 i; i < ids.length && (maximum || p.spendable < amount); ++i) {
            uint256 available = Math.min(room, supplyRoom);
            if (available == 0) break;
            uint256 wanted = maximum ? available : Math.min(amount - p.spendable, available);
            uint256 id = ids[i];
            (uint256 prior, uint256 seconds_) = d.credit.issuanceOf(id);
            if (prior != 0 && seconds_ == prior * T) continue;
            (uint256 face, uint256 active, uint32 rate) = d.credit.creditInfo(id);
            if (uint256(rate) * 10 >= 10_000) revert PremiumRateTooHigh();
            BuckNetMintMath.Snapshot memory s = BuckNetMintMath.Snapshot({face:face, activated:active,
                mark:d.credit.depreciatedFaceValue(id), principal:prior, k:k,
                effectiveRate:uint256(rate) * 10, accountRoom:available});
            (uint256 delivered, BuckNetMintMath.Plan memory found) = BuckNetMintMath.planAtMost(s, wanted);
            if (found.status == BuckNetMintMath.Status.WorkLimit) revert AllocationWorkLimit();
            if (delivered == 0) continue;
            address pool = state.facilityPool[id];
            if (pool == address(0)) pool = state.insurerPool[d.credit.insurerOf(id)];
            if (found.premium != 0) _pool(d, pool);
            p.issues[p.used++] = Issue(id, found.coverage, delivered, found.premium, pool);
            p.coverage += found.coverage;
            p.premium += found.premium;
            p.spendable += delivered;
            room -= delivered + (pool == msg.sender ? found.premium : 0);
            supplyRoom -= delivered + found.premium;
        }
        if (!maximum && p.spendable < amount) revert InsufficientCreditAllocation();
    }

    /// @notice Plan the complete ordered batch at one instant. Quotes use the
    ///         last guarded wheel publication; execution refreshes it once at
    ///         entry. This reserves nothing and never prepares insurer cash.
    /// @dev Each virtual slice consumes the same pool allowance, sponsor
    ///      funding, wallet cash, selected claim age, fund cash and shared
    ///      block remainder as execution. Shared insurer pools are snapshotted
    ///      once. Reusing their initial balances for each NFT is incorrect.
    function planBurn(BuckCash.Book storage cash, State storage state,
                      mapping(address => mapping(address => uint256)) storage allowances,
                      Dependencies memory d, uint256 amount, uint256[] memory ids, uint256 k, uint256 mark)
        public view returns (BurnPlan memory p)
    {
        _owner(state, d, msg.sender, false);
        checkSelection(d.credit, msg.sender, ids);
        BurnState memory b = _burnState(cash, d, ids.length, mark);
        uint256 beforeBalance = (b.carrying ? b.gross : b.net) / Q;
        if (amount > beforeBalance) revert InsufficientCoverage();
        p.slices = new Slice[](ids.length);
        uint256 remaining = amount;
        for (uint256 i; i < ids.length && remaining != 0; ++i) {
            uint256 id = ids[i];
            (uint256 principal, uint256 seconds_) = d.credit.issuanceOf(id);
            if (principal == 0) continue;
            address pool = state.facilityPool[id];
            uint256 poolIndex = _poolIndex(cash, allowances, b, pool);
            Pool memory v = b.pools[poolIndex];
            if (state.premium[id] != 0) _pool(d, pool);
            BuckNetBurnMath.Snapshot memory s = BuckNetBurnMath.Snapshot({
                principal:principal, issuedSeconds:seconds_, totalSeconds:b.totalSeconds,
                fundValueScaled:b.fundCash + b.fundMark, reliefLimitScaled:Math.min(b.fundCash / 10, b.budget),
                premiumPrincipal:state.premium[id], poolGrossScaled:v.gross, poolNetScaled:v.net,
                poolAvailableNominal:v.allowance, walletScaled:b.net});
            (uint256 face, uint256 active,) = d.credit.creditInfo(id);
            BuckNetBurnMath.Coverage memory c = BuckNetBurnMath.Coverage({
                face:face, active:active, mark:d.credit.depreciatedFaceValue(id), k:k});
            BuckNetBurnMath.Owner memory o = BuckNetBurnMath.Owner({nominalScaled:b.gross,
                sponsorAvailableScaled:b.sponsorFunds, carrying:b.carrying, sponsored:b.sponsored,
                selfRefund:pool == msg.sender});
            (uint256 target, BuckNetBurnMath.Plan memory found) = BuckNetBurnMath.planAtMost(s, c, o, remaining);
            if (found.status == BuckNetBurnMath.Status.WorkLimit) revert AllocationWorkLimit();
            if (target == 0) continue;
            uint256 priorVisible = (b.carrying ? b.gross : b.net) / Q;
            _applyVirtual(b, found, poolIndex, seconds_, principal);
            uint256 nextVisible = (b.carrying ? b.gross : b.net) / Q;
            if (nextVisible > priorVisible || priorVisible - nextVisible < target) revert BadAmount();
            uint256 decreased = priorVisible - nextVisible;
            remaining = decreased >= remaining ? 0 : remaining - decreased;
            p.slices[p.used++] = Slice(id, found);
            p.coverage += found.coverageReduction;
            p.refund += found.refundNominal;
        }
        if (remaining != 0) revert InsufficientCoverage();
    }

    function _burnState(BuckCash.Book storage cash, Dependencies memory d, uint256 count, uint256 mark)
        private view returns (BurnState memory b)
    {
        (b.gross, b.net) = cash.grossAndNetOf(msg.sender);
        b.carrying = d.identity.isCarrying(msg.sender);
        address sponsor = cash.sponsor[msg.sender];
        b.sponsored = sponsor != address(0);
        if (b.sponsored) {
            uint256 fee = b.gross - b.net;
            uint256 available = cash.netOf(sponsor);
            if (available < fee) revert BuckCash.DemurrageSponsorUnderfunded(msg.sender, sponsor, fee, available);
            b.sponsorFunds = available - fee;
            b.net = b.gross;
        }
        (, b.fundCash) = cash.fundParts();
        if (mark > (type(uint256).max - b.fundCash) / Q) revert BadAmount();
        b.fundMark = mark * Q;
        b.totalSeconds = d.claims.totalSeconds();
        b.budgetStarted = cash.budget.started && cash.budget.blockNumber == block.number;
        b.budget = b.budgetStarted ? cash.budget.remaining : b.fundCash / 10;
        b.pools = new Pool[](count);
    }

    function _poolIndex(BuckCash.Book storage cash,
                        mapping(address => mapping(address => uint256)) storage allowances,
                        BurnState memory b, address pool) private view returns (uint256 index)
    {
        for (; index < b.poolCount; ++index) if (b.pools[index].account == pool) return index;
        (uint256 gross, uint256 net) = cash.grossAndNetOf(pool);
        // A Carrying owner's own pool balance is the virtual post-burn
        // wallet, not the original on-chain amount from an earlier slice.
        if (pool == msg.sender) { gross = b.gross; net = b.net; }
        b.pools[index] = Pool(pool, gross, net, allowances[pool][address(this)]);
        ++b.poolCount;
    }

    function _applyVirtual(BurnState memory b, BuckNetBurnMath.Plan memory p, uint256 index,
                           uint256 seconds_, uint256 priorPrincipal) private view
    {
        Pool memory pool = b.pools[index];
        bool selfRefund = pool.account == msg.sender;
        uint256 refund = selfRefund ? 0 : p.refundNominal * Q;
        uint256 received = selfRefund ? 0 : b.sponsored ? refund : p.refundNetScaled;
        if (b.sponsored && !selfRefund) b.sponsorFunds -= received - p.refundNetScaled;
        uint256 debit = p.principal * Q - p.reliefScaled;
        if (b.carrying) {
            b.gross += refund;
            b.gross -= debit == 0 ? 0 : Math.mulDiv(b.gross, debit, b.net + received, Math.Rounding.Ceil);
        }
        b.net = b.net + received - debit;
        if (!b.carrying) b.gross = b.net;
        if (p.reliefScaled != 0) {
            if (!b.budgetStarted) { b.budgetStarted = true; b.budget = b.fundCash / 10; }
            b.budget -= p.reliefScaled;
            b.fundCash -= p.reliefScaled;
        }
        b.totalSeconds -= Math.mulDiv(seconds_, p.principal, priorPrincipal);
        if (!selfRefund) { pool.gross -= refund; pool.net -= p.refundNetScaled; }
        else { pool.gross = b.gross; pool.net = b.net; }
        if (pool.allowance != type(uint256).max) pool.allowance -= p.refundNominal;
        b.pools[index] = pool;
        // Other cached snapshots can also name the Carrying owner.
        for (uint256 i; i < b.poolCount; ++i) {
            if (b.pools[i].account == msg.sender) { b.pools[i].gross = b.gross; b.pools[i].net = b.net; }
        }
    }

    /// @dev Preconditions: authenticated owner, configured fund and host lock.
    ///      Only a valuation-only refresh runs under that lock. No basket
    ///      sale, insurer preparation, swap or keeper reward is reachable.
    ///      Postcondition: actual balance decreases by at least amount and
    ///      every selected NFT, pool and shared fund budget reconciles. Any
    ///      mismatch or later failure reverts the entire batch and wheel mark.
    function burn(BuckCash.Book storage cash, State storage state,
                  mapping(address => mapping(address => uint256)) storage allowances,
                  mapping(address => mapping(address => bytes32)) storage fragments,
                  Dependencies memory d, uint256 amount, uint256[] memory ids) public
    {
        _owner(state, d, msg.sender, false);
        cash.settle(msg.sender);
        uint256 beforeBalance = cash.balanceOf(msg.sender);
        uint256 k = d.controller.compute();
        IBuckFund fund = IBuckFund(d.jubilee);
        uint256 mark = fund.refreshBurnValuation();
        if (d.controller.currentBuckK() != k) revert CreditExceeded();
        BurnPlan memory p = planBurn(cash, state, allowances, d, amount, ids, k, mark);
        for (uint256 i; i < p.used; ++i) {
            Slice memory slice = p.slices[i];
            cash.consumeRelief(slice.reduction.reliefScaled);
            settle(cash, state, allowances, fragments, d, msg.sender, slice.tokenId,
                   slice.reduction.principal, slice.reduction.reliefScaled);
            if (d.controller.currentBuckK() != k) revert CreditExceeded();
        }
        uint256 afterBalance = cash.balanceOf(msg.sender);
        if (afterBalance > beforeBalance || beforeBalance - afterBalance < amount) revert BadAmount();
        fund.checkpointIssuance();
        if (d.controller.currentBuckK() != k) revert CreditExceeded();
    }

    /// @dev The host authenticates a direct owner call or exact coordinator
    ///      authorization and consumes the shared relief budget first. Refunds
    ///      are nominal BUCK carrying the original pool's average fee; the
    ///      owner and fund then supply exact full-value principal. Retirement
    ///      may not worsen the facility's K shortfall.
    function settle(BuckCash.Book storage cash, State storage state,
                    mapping(address => mapping(address => uint256)) storage allowances,
                    mapping(address => mapping(address => bytes32)) storage fragments,
                    Dependencies memory d, address owner, uint256 id, uint256 principal, uint256 relief)
        public returns (uint256 refund)
    {
        bool carrying = _owner(state, d, owner, false);
        if (d.credit.ownerOf(id) != owner) revert NotCreditOwner();
        (uint256 prior,) = d.credit.issuanceOf(id);
        if (principal == 0 || principal > prior) revert BadAmount();
        uint256 k = d.controller.currentBuckK();
        uint256 oldCapacity = Math.mulDiv(d.credit.currentValue(id), k, K_SCALE);
        uint256 oldShortfall = prior > oldCapacity ? prior - oldCapacity : 0;
        (, uint256 active,) = d.credit.creditInfo(id);
        uint256 coverage = Math.mulDiv(active, principal, prior);
        refund = Math.mulDiv(state.premium[id], principal, prior);
        delete state.authorization[id];
        if (refund != 0) {
            refundPremium(cash, state, allowances, fragments, d.identity, owner, id, refund);
            state.premium[id] -= refund;
        }
        uint256 ownerBefore = cash.balanceOf(owner);
        uint256 fundBefore = cash.balanceOf(d.jubilee);
        (uint256 ownerNominal, uint256 fundNominal) = cash.retire(owner, principal, relief, carrying);
        d.credit.recordReductionFromBuck(id, owner, principal);
        if (coverage != 0) d.credit.deactivateFromBuck(id, owner, coverage);
        d.claims.sync(id);
        uint256 left = prior - principal;
        uint256 afterCapacity = Math.mulDiv(d.credit.currentValue(id), k, K_SCALE);
        uint256 newShortfall = left > afterCapacity ? left - afterCapacity : 0;
        if (newShortfall > oldShortfall) revert CreditExceeded();
        if (left == 0) delete state.facilityPool[id];
        uint256 ownerBurn = ownerBefore - cash.balanceOf(owner);
        uint256 fundBurn = fundBefore - cash.balanceOf(d.jubilee);
        if (ownerBurn != 0) emit Transfer(owner, address(0), ownerBurn);
        if (fundBurn != 0) emit Transfer(d.jubilee, address(0), fundBurn);
        emit CashRetired(owner, principal, principal * Q - relief, ownerNominal, relief, fundNominal);
        emit FacilityReduced(owner, id, principal, relief, fundNominal, refund);
    }

    function refundPremium(BuckCash.Book storage cash, State storage state,
                           mapping(address => mapping(address => uint256)) storage allowances,
                           mapping(address => mapping(address => bytes32)) storage fragments,
                           IdentityRegistry identity, address owner, uint256 id, uint256 amount) public
    {
        if (amount == 0) return;
        address pool = state.facilityPool[id];
        if (pool == address(0)) revert NoInsurerPool();
        if (!identity.isVerified(pool) || !identity.isCarrying(pool)) revert NotCarryingPool();
        uint256 permitted = allowances[pool][address(this)];
        if (permitted != type(uint256).max) {
            if (permitted < amount) revert InsufficientAllowance();
            allowances[pool][address(this)] = permitted - amount;
        }
        BuckTransfer.send(cash, fragments, identity, pool, owner, amount);
    }

    /// @dev The selected order is explicit; duplicate NFTs cannot obtain a
    ///      second quote against the same pre-operation capacity/age.
    function checkSelection(BuckCredit credit, address owner, uint256[] memory ids) internal view {
        for (uint256 i; i < ids.length; ++i) {
            if (credit.ownerOf(ids[i]) != owner) revert NotCreditOwner();
            for (uint256 j; j < i; ++j) if (ids[j] == ids[i]) revert BadAmount();
        }
    }

    function select(BuckCredit credit, address owner, bool cheapest) public view returns (uint256[] memory ids) {
        uint256 count = credit.balanceOf(owner);
        ids = new uint256[](count);
        uint32[] memory rates = new uint32[](count);
        for (uint256 i; i < count; ++i) {
            uint256 id = credit.tokenOfOwnerByIndex(owner, i);
            (,, uint32 rate) = credit.creditInfo(id);
            uint256 j = i;
            while (j > 0 && (cheapest ? rates[j - 1] > rate : rates[j - 1] < rate)) {
                ids[j] = ids[j - 1]; rates[j] = rates[j - 1]; --j;
            }
            ids[j] = id; rates[j] = rate;
        }
    }

    function _owner(State storage state, Dependencies memory d, address owner, bool issuing)
        private view returns (bool carrying)
    {
        if (!d.identity.isVerified(owner)) revert SenderNotVerified();
        if (!d.identity.isVerified(d.jubilee) || !d.identity.isCarrying(d.jubilee)) revert NotCarryingPool();
        carrying = d.identity.isCarrying(owner);
        if (owner == d.jubilee) revert BadAmount();
        if (issuing && carrying && !state.carryingIssuer[owner]) revert CarryingIssuerNotApproved();
    }

    function _pool(Dependencies memory d, address pool) private view {
        if (pool == address(0)) revert NoInsurerPool();
        if (pool == d.jubilee || !d.identity.isVerified(pool) || !d.identity.isCarrying(pool)) revert NotCarryingPool();
    }
}
