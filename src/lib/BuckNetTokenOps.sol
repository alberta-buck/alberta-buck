// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "../BN254.sol";
import {IdentityRegistry} from "../IdentityRegistry.sol";
import {BuckLedger} from "./BuckLedger.sol";
import {BuckCredit} from "../BuckCredit.sol";
import {JubileeClaimBook} from "../JubileeClaimBook.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckTypes} from "../BuckTypes.sol";
import {BuckNetMintMath} from "./BuckNetMintMath.sol";

interface IBuckNetIssuanceK {
    function currentBuckK() external view returns (uint256);
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
    uint256 private constant Q = 1e36;

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

    struct MintState {
        uint256 k;
        uint256 factor;
        uint256 creditValueBefore;
        uint256 balanceBefore;
        uint256 accountRoom;
        uint256 supplyRoom;
        uint256 coverage;
        uint256 premium;
        uint256 spendable;
        uint256 used;
    }

    event Transfer(address indexed from, address indexed to, uint256 value);
    /// @dev Full-value delivery in 1e36 fractional units per raw BUCK unit.
    event NetValueTransfer(address indexed from, address indexed to, uint256 nominal, uint256 netScaled);
    event BuckTransferReceipt(address indexed from, address indexed to, uint256 amount,
                              bytes32 fromCipherHash, bytes32 toCipherHash);
    event FacilityIssued(address indexed owner, uint256 indexed tokenId, uint256 coverage,
                         uint256 principal, uint256 premium, address pool);
    event Minted(address indexed account, uint256 amount, uint256 premium, uint256 creditValue,
                 uint256 buckKValue, uint256 newLimit);
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
    error AllocationWorkLimit();

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

    /// @notice Issue a requested visible balance increase over selected NFTs.
    /// @dev Ordinary owners only until approved Carrying issuance is added.
    ///      The host holds its reentrancy guard. NFT ownership and uniqueness,
    ///      the shared pre-mint reserve and the actual post-mint balance are
    ///      checked atomically. The maximum sentinel issues the maximum found
    ///      in the selected order, subject to the same bounds; it is not a
    ///      globally optimal allocation across arbitrary orderings. A bounded-
    ///      search exhaustion is distinct from insufficient capacity.
    function mint(BuckLedger.Ledger storage ledger,
                  mapping(uint256 => address) storage facilityPool,
                  mapping(uint256 => uint256) storage premiumPrincipal,
                  mapping(address => address) storage insurerPool,
                  mapping(address => mapping(address => bytes32)) storage fragments,
                  IssueDependencies memory d, uint256 amount, uint256[] memory ids, bool automatic) public
    {
        d.identity.enableCarryingModeHook();
        if (!d.identity.isVerified(msg.sender) || !d.identity.isVerified(d.jubilee)) revert NotVerified();
        if (d.identity.isCarrying(msg.sender) || !d.identity.isCarrying(d.jubilee)) revert NotCarryingPool();
        d.claims.totalSeconds();
        if (automatic) ids = _cheapestFirst(d.credit, msg.sender);
        (MintState memory state, IssueRequest[] memory requests) =
            _planMint(ledger, facilityPool, insurerPool, d, amount, ids, d.controller.compute());
        if (state.premium != 0 && state.balanceBefore < Math.mulDiv(state.premium, state.factor, K_SCALE)) {
            revert FundingInsufficient();
        }

        for (uint256 i; i < state.used; ++i) {
            (, uint256 paid) = issue(ledger, facilityPool, premiumPrincipal, insurerPool, fragments, d, requests[i]);
            if (paid != requests[i].maxPremium || d.controller.currentBuckK() != state.k) revert CreditExceeded();
            if (d.controller.fundingFactor() != state.factor) revert FundingInsufficient();
        }
        uint256 afterBalance = ledger.balanceOf(msg.sender);
        if (afterBalance < state.balanceBefore || afterBalance - state.balanceBefore < state.spendable) {
            revert BadAmount();
        }
        uint256 newLimit = Math.mulDiv(d.credit.totalCurrentValue(msg.sender), state.k, K_SCALE);
        emit Minted(msg.sender, state.coverage, state.premium, state.creditValueBefore, state.k, newLimit);
    }

    /// @notice Legacy-shaped aggregate quote at current K. It does not reserve
    ///         funding or authorize a pool; execution repeats all checks.
    function quoteMint(BuckLedger.Ledger storage ledger,
                       mapping(uint256 => address) storage facilityPool,
                       mapping(address => address) storage insurerPool,
                       IssueDependencies memory d, uint256 amount, uint256[] memory ids)
        public view returns (uint256 coverage, uint256 premium)
    {
        if (d.identity.isCarrying(msg.sender)) revert NotCarryingPool();
        (MintState memory state,) = _planMint(ledger, facilityPool, insurerPool, d, amount, ids,
                                              d.controller.currentBuckK());
        return (state.coverage, state.premium);
    }

    function _planMint(BuckLedger.Ledger storage ledger,
                       mapping(uint256 => address) storage facilityPool,
                       mapping(address => address) storage insurerPool,
                       IssueDependencies memory d, uint256 amount, uint256[] memory ids, uint256 k)
        private view returns (MintState memory state, IssueRequest[] memory requests)
    {
        _checkSelection(d.credit, msg.sender, ids);
        state.k = k;
        state.factor = d.controller.fundingFactor();
        state.creditValueBefore = d.credit.totalCurrentValue(msg.sender);
        state.balanceBefore = ledger.balanceOf(msg.sender);
        (uint256 held, uint256 unspent) = ledger.partsOf(msg.sender);
        state.accountRoom = (BuckTypes.MAX_BALANCE * Q - held - unspent) / Q;
        state.supplyRoom = type(uint256).max / Q - ledger.issued;
        requests = new IssueRequest[](ids.length);
        bool maximum = amount == type(uint256).max;

        for (uint256 i; i < ids.length && (maximum || state.spendable < amount); ++i) {
            uint256 room = Math.min(state.accountRoom, state.supplyRoom);
            if (room == 0) break;
            uint256 wanted = maximum ? room : Math.min(amount - state.spendable, room);
            (IssueRequest memory request, uint256 spendable) = _mintSlice(d, ids[i], state.k, room, wanted);
            if (spendable == 0) continue;
            if (request.maxPremium != 0) {
                request.expectedPool = facilityPool[ids[i]];
                if (request.expectedPool == address(0)) request.expectedPool = insurerPool[d.credit.insurerOf(ids[i])];
            }
            requests[state.used++] = request;
            state.coverage += request.coverage;
            state.premium += request.maxPremium;
            state.spendable += spendable;
            state.accountRoom -= spendable;
            state.supplyRoom -= spendable + request.maxPremium;
        }
        if (!maximum && state.spendable < amount) revert CreditExceeded();
    }

    function _mintSlice(IssueDependencies memory d, uint256 id, uint256 k, uint256 room, uint256 wanted)
        private view returns (IssueRequest memory request, uint256 spendable)
    {
        (uint256 face, uint256 active, uint32 rate) = d.credit.creditInfo(id);
        uint256 effective = uint256(rate) * POOL_ROI_INV;
        if (effective >= BP) revert BadPremiumRate();
        (uint256 principal, uint256 seconds_) = d.credit.issuanceOf(id);
        if (principal != 0 && seconds_ == principal * RELIEF_SECONDS) return (request, 0);
        BuckNetMintMath.Snapshot memory snapshot = BuckNetMintMath.Snapshot({
            face: face, activated: active, mark: d.credit.depreciatedFaceValue(id), principal: principal,
            k: k, effectiveRate: effective, accountRoom: room
        });
        BuckNetMintMath.Plan memory plan_;
        (spendable, plan_) = BuckNetMintMath.planAtMost(snapshot, wanted);
        if (plan_.status == BuckNetMintMath.Status.WorkLimit) revert AllocationWorkLimit();
        if (plan_.status != BuckNetMintMath.Status.Found) return (request, 0);
        request = IssueRequest({tokenId: id, coverage: plan_.coverage, spendable: spendable,
                                maxPremium: plan_.premium, expectedPool: address(0)});
    }

    function _checkSelection(BuckCredit credit, address owner, uint256[] memory ids) private view {
        for (uint256 i; i < ids.length; ++i) {
            if (credit.ownerOf(ids[i]) != owner) revert NotOwner();
            for (uint256 j; j < i; ++j) if (ids[j] == ids[i]) revert BadAmount();
        }
    }

    function _cheapestFirst(BuckCredit credit, address owner) private view returns (uint256[] memory ids) {
        uint256 count = credit.balanceOf(owner);
        ids = new uint256[](count);
        uint32[] memory rates = new uint32[](count);
        for (uint256 i; i < count; ++i) {
            uint256 id = credit.tokenOfOwnerByIndex(owner, i);
            (,, uint32 rate) = credit.creditInfo(id);
            uint256 j = i;
            while (j > 0 && rates[j - 1] > rate) {
                ids[j] = ids[j - 1];
                rates[j] = rates[j - 1];
                --j;
            }
            ids[j] = id;
            rates[j] = rate;
        }
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
