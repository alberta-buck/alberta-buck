// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckCredit} from "./BuckCredit.sol";
import {BuckTypes} from "./BuckTypes.sol";
import {BN254} from "./BN254.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";
import {BuckLedger} from "./lib/BuckLedger.sol";
import {BuckRenewal, BuckRenewalConsent} from "./BuckRenewalConsent.sol";
import {JubileeClaimBook} from "./JubileeClaimBook.sol";

interface IBuckNetK {
    function currentBuckK() external view returns (uint256);
    function compute() external returns (uint256);
    function fundingFactor() external view returns (uint256);
}

interface IBuckNetReserveHook {
    function checkpointIssuance() external;
}

/// @notice Experimental integration of net BUCK accounting and per-NFT credit.
/// @dev NOT A DEPLOYMENT TARGET: leveraged basket compatibility, production
///      deployment/migration, in-kind asset conversion, real insurer liquidity
///      and fractional event semantics remain incomplete.
///      Fund-authorized reductions trust the designated coordinator to apply
///      the proportional policy; the owner separately authorizes each request.
contract BuckNet is IERC20, IERC20Metadata {
    using BuckLedger for BuckLedger.Ledger;

    uint256 public constant FRACTION_SCALE = 1e36;
    uint256 private constant K_SCALE = 1e18;
    uint256 private constant BP = 10_000;
    uint256 private constant POOL_ROI_INV = 10;

    BuckCredit public immutable buckCredit;
    IBuckNetK public immutable buckK;
    IdentityRegistry public immutable identity;
    BuckRenewalConsent public immutable renewalConsent;
    JubileeClaimBook public immutable claimBook;
    address public immutable jubilee;
    BuckLedger.Ledger private _book;
    mapping(address => mapping(address => uint256)) private _allowances;
    mapping(address => mapping(address => bytes32)) private _receiptFragments;

    /// @notice The current pool chosen by each insurer for new facilities.
    mapping(address => address) public insurerPool;
    /// @notice Insurer that originally registered a pool; old obligations
    ///         remain isolated even if that insurer chooses a new pool.
    mapping(address => address) public poolInsurer;
    /// @notice The pool that actually received this facility's active deposit.
    mapping(uint256 => address) public facilityPool;
    /// @notice Returnable premium deposit, in nominal raw BUCK.
    mapping(uint256 => uint256) public premiumPrincipal;

    struct ReductionAuthorization {
        uint256 principal;
        uint256 minimumReliefScaled;
        uint64 deadline;
    }
    mapping(uint256 => ReductionAuthorization) public reductionAuthorization;
    bool private transient _entered;

    event ApproveReceipt(address indexed owner, address indexed spender, bytes32 receiptHash);
    event BuckTransferReceipt(address indexed from, address indexed to, uint256 amount,
                              bytes32 fromCipherHash, bytes32 toCipherHash);
    event InsurerPoolSet(address indexed insurer, address indexed pool);
    event FacilityIssued(address indexed owner, uint256 indexed tokenId, uint256 coverage,
                         uint256 principal, uint256 premium, address pool);
    event ReductionAuthorized(address indexed owner, uint256 indexed tokenId, uint256 principal,
                              uint256 minimumReliefScaled, uint64 deadline);
    event FacilityReduced(address indexed owner, uint256 indexed tokenId, uint256 principal,
                          uint256 reliefScaled, uint256 nominalFundSpentScaled, uint256 premiumRefund);
    event FacilityRenewed(address indexed owner, uint256 indexed tokenId, uint256 relief,
                          uint256 additionalIssuance, uint256 principal, uint256 nominalFundSpentScaled);

    error Reentrant();
    error BadAddress();
    error NotVerified();
    error NotOwner();
    error NotJubilee();
    error NotCarryingPool();
    error PoolNotControlled();
    error NoInsurerPool();
    error CreditExceeded();
    error FundingInsufficient();
    error BadPremiumRate();
    error BadAmount();
    error BadAuthorization();
    error UnauthorizedTransfer();
    error InsufficientAllowance();
    error NotRegistry();

    modifier nonReentrant() {
        if (_entered) revert Reentrant();
        _entered = true;
        _;
        _entered = false;
    }

    constructor(address credit_, address controller_, address identity_, address jubilee_) {
        if (credit_ == address(0) || controller_ == address(0) || identity_ == address(0) || jubilee_ == address(0)) {
            revert BadAddress();
        }
        buckCredit = BuckCredit(credit_);
        buckK = IBuckNetK(controller_);
        identity = IdentityRegistry(identity_);
        jubilee = jubilee_;
        renewalConsent = new BuckRenewalConsent(address(this), credit_);
        claimBook = new JubileeClaimBook(address(this), credit_);
        _book.initialize(jubilee_);
    }

    function name() external pure returns (string memory) { return "Alberta Buck"; }
    function symbol() external pure returns (string memory) { return "BUCK"; }
    function decimals() external pure returns (uint8) { return BuckTypes.DECIMALS; }
    function totalSupply() external view returns (uint256) { return _book.issued; }
    function totalIssued() external view returns (uint256) { return _book.issued; }
    function balanceOf(address owner) public view returns (uint256) { return _book.balanceOf(owner); }
    function netBalanceOf(address owner) external view returns (uint256) { return _book.netOf(owner); }
    function heldAndUnspentScaled(address owner) external view returns (uint256 held, uint256 unspent) {
        return _book.partsOf(owner);
    }
    function fundBalancesScaled() external view returns (uint256 gross, uint256 net) { return _book.fundParts(); }
    function fundFlows() external view returns (BuckLedger.FundFlows memory) { return _book.fundFlows(); }
    /// @notice Whole nominal Carrying BUCK needed to transfer at least `net`
    ///         full-value raw BUCK from the Jubilee fund at the current block.
    function fundNominalForNet(uint256 net) external view returns (uint256) {
        return _book.fundNominalForNet(net);
    }
    function maxFundNetPayout() external view returns (uint256) { return _book.maxNetPayout(); }
    function allowance(address owner, address spender) external view returns (uint256) { return _allowances[owner][spender]; }
    function receiptFragment(address from, address to) external view returns (bytes32) {
        return _receiptFragments[from][to];
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        _approve(msg.sender, spender, amount);
        return true;
    }

    function approve(address spender, uint256 amount, IdentityRegistry.ElGamalCT calldata E,
                     IdentityRegistry.CPProof calldata proof) external nonReentrant returns (bool)
    {
        if (!identity.isVerified(msg.sender) || !identity.isVerified(spender)) revert NotVerified();
        if (!identity.verifyApprove(msg.sender, spender, E, proof)) revert UnauthorizedTransfer();
        bytes32 receipt = keccak256(abi.encode(E.R.X, E.R.Y, E.C.X, E.C.Y));
        _receiptFragments[msg.sender][spender] = receipt;
        emit ApproveReceipt(msg.sender, spender, receipt);
        identity.markApproved(spender);
        _approve(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external nonReentrant returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external nonReentrant returns (bool) {
        _spendAllowance(from, msg.sender, amount);
        _transfer(from, to, amount);
        return true;
    }

    /// @notice Register a Carrying pool bound by this insurer. Existing NFT
    ///         deposits remain pinned to the pool that received them.
    function setInsurerPool(address pool) external nonReentrant {
        if (!identity.isVerified(msg.sender) || !identity.isVerified(pool)) revert NotVerified();
        if (!identity.isCarrying(pool) || pool == jubilee) revert NotCarryingPool();
        if (pool != msg.sender && identity.binderOf(pool) != msg.sender) revert PoolNotControlled();
        address registered = poolInsurer[pool];
        if (registered != address(0) && registered != msg.sender) revert PoolNotControlled();
        poolInsurer[pool] = msg.sender;
        insurerPool[msg.sender] = pool;
        emit InsurerPoolSet(msg.sender, pool);
    }

    /// @notice Activate coverage and issue BUCK against one selected NFT.
    /// @param tokenId The owner's BuckCredit NFT.
    /// @param coverage Face units to activate; zero can use existing capacity.
    /// @param spendable BUCK newly made available after the premium deposit.
    /// @return principal Total BUCK issued, including the premium funding.
    /// @return premium Full-value BUCK sent to the NFT's insurer pool.
    function issue(uint256 tokenId, uint256 coverage, uint256 spendable)
        external nonReentrant returns (uint256 principal, uint256 premium)
    {
        _enableModeHook();
        if (!identity.isVerified(msg.sender) || !identity.isVerified(jubilee)) revert NotVerified();
        if (identity.isCarrying(msg.sender) || !identity.isCarrying(jubilee)) revert NotCarryingPool();
        if (buckCredit.ownerOf(tokenId) != msg.sender) revert NotOwner();
        if (spendable == 0) revert BadAmount();
        claimBook.totalSeconds(); // Do not update a stale capped benchmark.
        uint256 k = buckK.compute();
        uint256 preFunding = _book.balanceOf(msg.sender);
        uint256 beforeValue = buckCredit.currentValue(tokenId);
        if (coverage != 0) buckCredit.activateFromBuck(tokenId, msg.sender, coverage);
        uint256 afterValue = buckCredit.currentValue(tokenId);
        if (afterValue < beforeValue) revert CreditExceeded();
        (,, uint32 premiumRate) = buckCredit.creditInfo(tokenId);
        uint256 effectiveRate = uint256(premiumRate) * POOL_ROI_INV;
        if (effectiveRate >= BP) revert BadPremiumRate();
        premium = Math.mulDiv(afterValue - beforeValue, effectiveRate, BP);
        uint256 factor = buckK.fundingFactor();
        if (premium != 0 && preFunding < Math.mulDiv(premium, factor, K_SCALE)) revert FundingInsufficient();
        principal = spendable + premium;
        (uint256 oldPrincipal,) = buckCredit.issuanceOf(tokenId);
        if (oldPrincipal + principal > Math.mulDiv(afterValue, k, K_SCALE)) revert CreditExceeded();

        address pool;
        if (premium != 0) {
            pool = facilityPool[tokenId];
            if (pool == address(0)) {
                pool = insurerPool[buckCredit.insurerOf(tokenId)];
                if (pool == address(0)) revert NoInsurerPool();
                facilityPool[tokenId] = pool;
            }
            if (!identity.isVerified(pool) || !identity.isCarrying(pool)) revert NotCarryingPool();
        }
        buckCredit.recordIssuanceFromBuck(tokenId, msg.sender, principal);
        _syncClaim(tokenId);
        _book.issue(msg.sender, principal, false);
        emit Transfer(address(0), msg.sender, principal);
        if (premium != 0) {
            premiumPrincipal[tokenId] += premium;
            _transfer(msg.sender, pool, premium);
        }
        emit FacilityIssued(msg.sender, tokenId, coverage, principal, premium, pool);
        IBuckNetReserveHook(jubilee).checkpointIssuance();
    }

    /// @notice Authorize one exact facility reduction with a net-relief floor.
    ///         An owner can cancel by setting principal to zero.
    function authorizeReduction(uint256 tokenId, uint256 principal, uint256 minimumReliefScaled, uint64 deadline)
        external nonReentrant
    {
        if (buckCredit.ownerOf(tokenId) != msg.sender) revert NotOwner();
        if (principal == 0) {
            delete reductionAuthorization[tokenId];
        } else {
            (uint256 issued,) = buckCredit.issuanceOf(tokenId);
            if (principal > issued || minimumReliefScaled > principal * FRACTION_SCALE || deadline < block.timestamp) {
                revert BadAuthorization();
            }
            reductionAuthorization[tokenId] = ReductionAuthorization(principal, minimumReliefScaled, deadline);
        }
        emit ReductionAuthorized(msg.sender, tokenId, principal, minimumReliefScaled, deadline);
    }

    /// @notice Settle a same-insurer renewal after the fund's proportional
    ///         sale and original pool's liquidity preparation. `relief` is
    ///         whole full-value raw BUCK; no other facility is touched.
    function renewFromJubilee(address owner, uint256 tokenId, BuckRenewal.Terms calldata terms, uint256 relief)
        external nonReentrant returns (uint256 refund, uint256 premium)
    {
        if (msg.sender != jubilee) revert NotJubilee();
        if (!identity.isVerified(owner) || identity.isCarrying(owner)) revert NotVerified();
        if (!identity.isVerified(jubilee) || !identity.isCarrying(jubilee)) revert NotCarryingPool();
        (uint256 oldPrincipal, bytes32 digest) = renewalConsent.checkRenewal(owner, tokenId, terms);
        if (relief < terms.minimumRelief || relief > oldPrincipal) revert BadAuthorization();
        claimBook.totalSeconds();
        uint256 k = buckK.compute();
        uint256 oldLimit = Math.mulDiv(buckCredit.currentValue(tokenId), k, K_SCALE);
        uint256 oldShortfall = oldPrincipal > oldLimit ? oldPrincipal - oldLimit : 0;
        (, uint256 oldCoverage,) = buckCredit.creditInfo(tokenId);
        if (terms.activatedFace < oldCoverage) revert CreditExceeded();

        refund = premiumPrincipal[tokenId];
        _refundPremium(owner, tokenId, refund);
        renewalConsent.consume(tokenId, digest);
        uint256 nominalSpent;
        if (relief != 0) {
            nominalSpent = _book.retireWithFund(owner, relief, relief * FRACTION_SCALE, false);
            emit Transfer(jubilee, address(0), relief);
        }
        // Reduce and checkpoint the old principal first. The credit then
        // applies replacement terms and coverage in one call; additional
        // principal is issued only after its final K bound is checked.
        buckCredit.recordRenewalFromBuck(tokenId, owner, relief, 0);
        _syncClaim(tokenId);
        buckCredit.renewTermsFromBuck(tokenId, owner, terms.face, terms.floor, terms.depType,
                                     terms.depRate, terms.depStartAt, terms.premiumRate,
                                     terms.activatedFace - oldCoverage);
        uint256 value = buckCredit.currentValue(tokenId);
        uint256 rate = uint256(terms.premiumRate) * POOL_ROI_INV;
        if (rate >= BP) revert BadPremiumRate();
        premium = Math.mulDiv(value, rate, BP);
        if (premium > terms.maxPremium) revert BadAuthorization();
        if (premium != 0 && _book.balanceOf(owner) < Math.mulDiv(premium, buckK.fundingFactor(), K_SCALE)) {
            revert FundingInsufficient();
        }
        uint256 newPrincipal = oldPrincipal - relief + terms.additionalIssuance;
        uint256 newLimit = Math.mulDiv(value, k, K_SCALE);
        uint256 newShortfall = newPrincipal > newLimit ? newPrincipal - newLimit : 0;
        if (newShortfall > oldShortfall) revert CreditExceeded();

        address pool;
        if (premium != 0) {
            pool = insurerPool[buckCredit.insurerOf(tokenId)];
            if (pool == address(0)) revert NoInsurerPool();
            if (!identity.isVerified(pool) || !identity.isCarrying(pool)) revert NotCarryingPool();
        }
        if (terms.additionalIssuance != 0) {
            _book.issue(owner, terms.additionalIssuance, false);
            buckCredit.recordIssuanceFromBuck(tokenId, owner, terms.additionalIssuance);
            emit Transfer(address(0), owner, terms.additionalIssuance);
            _syncClaim(tokenId);
        }
        premiumPrincipal[tokenId] = premium;
        facilityPool[tokenId] = pool;
        if (premium != 0) _transfer(owner, pool, premium);
        emit FacilityRenewed(owner, tokenId, relief, terms.additionalIssuance, newPrincipal, nominalSpent);
        IBuckNetReserveHook(jubilee).checkpointIssuance();
    }

    /// @notice Complete a pre-authorized reduction after the Jubilee manager
    ///         has sold its proportional position and prepared insurer funds.
    /// @dev Only the configured coordinator may call it. The coordinator must
    ///      compute proportional relief from actual proceeds; BuckNet cannot
    ///      verify that policy itself. No basket call occurs while this token's
    ///      reentrancy guard is held.
    function settleFromJubilee(address owner, uint256 tokenId, uint256 principal, uint256 reliefScaled)
        external nonReentrant returns (uint256 refund)
    {
        if (msg.sender != jubilee) revert NotJubilee();
        if (buckCredit.ownerOf(tokenId) != owner) revert NotOwner();
        if (!identity.isVerified(owner) || identity.isCarrying(owner)) revert NotVerified();
        if (!identity.isVerified(jubilee) || !identity.isCarrying(jubilee)) revert NotCarryingPool();
        ReductionAuthorization memory auth = reductionAuthorization[tokenId];
        if (principal == 0 || principal != auth.principal || block.timestamp > auth.deadline
            || reliefScaled < auth.minimumReliefScaled) revert BadAuthorization();
        claimBook.totalSeconds();
        (uint256 priorPrincipal,) = buckCredit.issuanceOf(tokenId);
        if (principal > priorPrincipal) revert BadAmount();
        (, uint256 active,) = buckCredit.creditInfo(tokenId);
        uint256 coverage = principal == priorPrincipal ? active : Math.mulDiv(active, principal, priorPrincipal);
        refund = principal == priorPrincipal ? premiumPrincipal[tokenId]
                 : Math.mulDiv(premiumPrincipal[tokenId], principal, priorPrincipal);
        uint256 beforeCapacity = Math.mulDiv(buckCredit.currentValue(tokenId), buckK.currentBuckK(), K_SCALE);
        uint256 oldShortfall = priorPrincipal > beforeCapacity ? priorPrincipal - beforeCapacity : 0;
        delete reductionAuthorization[tokenId];
        if (refund != 0) {
            _refundPremium(owner, tokenId, refund);
            premiumPrincipal[tokenId] -= refund;
        }
        uint256 nominalFundSpent = _book.retireWithFund(owner, principal, reliefScaled, false);
        buckCredit.recordReductionFromBuck(tokenId, owner, principal);
        if (coverage != 0) buckCredit.deactivateFromBuck(tokenId, owner, coverage);
        _syncClaim(tokenId);
        uint256 newPrincipal = priorPrincipal - principal;
        uint256 afterCapacity = Math.mulDiv(buckCredit.currentValue(tokenId), buckK.currentBuckK(), K_SCALE);
        uint256 newShortfall = newPrincipal > afterCapacity ? newPrincipal - afterCapacity : 0;
        if (newShortfall > oldShortfall) revert CreditExceeded();
        if (newPrincipal == 0) delete facilityPool[tokenId];
        // ERC-20 cannot express fractional raw units. Record the exact split in
        // FacilityReduced; the integral burn events add to total principal.
        uint256 fundRaw = Math.ceilDiv(reliefScaled, FRACTION_SCALE);
        if (fundRaw != 0) emit Transfer(jubilee, address(0), fundRaw);
        if (principal != fundRaw) emit Transfer(owner, address(0), principal - fundRaw);
        emit FacilityReduced(owner, tokenId, principal, reliefScaled, nominalFundSpent, refund);
    }

    function claimSeconds() external view returns (uint256) { return claimBook.totalSeconds(); }
    function maturitiesPending() external view returns (bool) { return claimBook.pending(); }
    function processMaturities(uint256 maxSteps) external nonReentrant returns (uint256, bool) {
        return claimBook.process(maxSteps);
    }
    function selectedClaimSeconds(uint256 tokenId, uint256 principal) external view returns (uint256 selected, uint256 total) {
        total = claimBook.totalSeconds();
        (uint256 issued, uint256 seconds_) = buckCredit.issuanceOf(tokenId);
        if (issued == 0 || principal > issued) revert BadAmount();
        selected = Math.mulDiv(seconds_, principal, issued);
    }

    function _syncClaim(uint256 tokenId) private {
        claimBook.sync(tokenId);
    }

    function _refundPremium(address owner, uint256 tokenId, uint256 amount) private {
        if (amount == 0) return;
        address pool = facilityPool[tokenId];
        if (pool == address(0)) revert NoInsurerPool();
        if (!identity.isVerified(pool) || !identity.isCarrying(pool)) revert NotCarryingPool();
        _spendAllowance(pool, address(this), amount);
        _transfer(pool, owner, amount);
    }

    function _transfer(address from, address to, uint256 amount) private {
        _enableModeHook();
        if (from == address(0) || to == address(0)) revert BadAddress();
        if (!identity.isVerified(from) || !identity.isVerified(to)) revert NotVerified();
        bytes32 toHash = _receiptFragments[from][to];
        if (toHash == bytes32(0)) {
            if (!identity.isPublicIdentity(from)) revert UnauthorizedTransfer();
            toHash = _identityHash(to);
        }
        bytes32 fromHash = _receiptFragments[to][from];
        if (fromHash == bytes32(0)) {
            if (!identity.isPublicIdentity(to)) revert UnauthorizedTransfer();
            fromHash = _identityHash(from);
        }
        _book.transfer(from, to, amount, identity.isCarrying(from), identity.isCarrying(to));
        emit Transfer(from, to, amount);
        emit BuckTransferReceipt(from, to, amount, fromHash, toHash);
    }

    /// @notice Called by the opted-in identity registry before it changes a
    ///         Carrying flag. Outstanding credit owners stay ordinary; the
    ///         Jubilee fund and registered insurer pools stay Carrying.
    function onCarryingModeChange(address account, bool carrying) external nonReentrant {
        if (msg.sender != address(identity)) revert NotRegistry();
        if (account == jubilee) {
            if (!carrying) revert NotCarryingPool();
            return;
        }
        if (!carrying && poolInsurer[account] != address(0)) revert NotCarryingPool();
        if (carrying && buckCredit.hasIssuedPrincipal(account)) revert CreditExceeded();
        _book.syncMode(account, carrying);
    }

    function _enableModeHook() private {
        identity.enableCarryingModeHook();
    }

    function _approve(address owner, address spender, uint256 amount) private {
        if (spender == address(0)) revert BadAddress();
        _allowances[owner][spender] = amount;
        emit Approval(owner, spender, amount);
    }

    function _spendAllowance(address owner, address spender, uint256 amount) private {
        uint256 current = _allowances[owner][spender];
        if (current != type(uint256).max) {
            if (current < amount) revert InsufficientAllowance();
            _allowances[owner][spender] = current - amount;
        }
    }

    function _identityHash(address account) private view returns (bytes32) {
        BN254.G1Point memory pk = identity.pkOf(account);
        IdentityRegistry.ElGamalCT memory E = identity.ciphertextOf(account);
        return keccak256(abi.encode(pk.X, pk.Y, E.R.X, E.R.Y, E.C.X, E.C.Y));
    }
}
