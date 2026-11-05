// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckTypes} from "./BuckTypes.sol";
import {BuckCredit} from "./BuckCredit.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";
import {BuckCash} from "./lib/BuckCash.sol";
import {BuckRenewalOps} from "./lib/BuckRenewalOps.sol";
import {BuckTransfer} from "./lib/BuckTransfer.sol";
import {BuckFacilities, IBuckIssuanceK, IBuckFund} from "./lib/BuckFacilities.sol";
import {BuckRenewal, BuckRenewalConsent} from "./BuckRenewalConsent.sol";
import {JubileeClaimBook} from "./JubileeClaimBook.sol";
import {IBuckJubilee} from "./IBuckJubilee.sol";

interface IBuckK is IBuckIssuanceK {}

/// @title Buck — identity-bound ERC-20, compounded cash and per-facility Jubilee.
/// @notice Ordinary balanceOf is spendable net cash; Carrying balanceOf is
///         nominal cash whose pooled fees travel with ERC-20 transfers.
///         Unissued credit capacity is never included in a wallet balance.
/// @dev Retains Buck's identity approvals, allowance/receipt dispatcher and
///      numeric mint/burn entry points. The former signed wallet and holding
///      BUCK-seconds are replaced by one unsigned net/checkpoint/flags slot.
///      BuckCredit owns actual principal and its capped issuance age; the
///      external Jubilee fund owns investment and marked-relief policy.
///
///      Monetary invariants, up to the tested fractional rounding bound:
///      S = sum(open facility principal) + deferred Jubilee principal;
///      net cash over actual holders, including Jubilee, = S*1e36;
///      transfers change neither S nor facility age; mint/burn change both
///      principal and cash atomically. Carrying nominal value and LP marks
///      are never summed as additional supply.
///
///      Experimental migration checkpoint: clients, leveraged basket/wheel,
///      release proofs and long-run validation are tracked in the unified
///      plan. This source change does not convert deployed storage or results.
contract Buck is IERC20Metadata, IBuckJubilee {
    using BuckCash for BuckCash.Book;
    uint256 public constant FRACTION_SCALE = 1e36;
    bytes32 public constant ACCOUNTING_MODEL = keccak256("Buck/compounded-cash/per-facility-jubilee/v1");
    uint256 private constant K_SCALE = 1e18;
    uint256 private constant T = 50 * (365 days + 6 hours);

    BuckCredit public immutable override buckCredit;
    IBuckK public immutable buckK;
    IdentityRegistry public immutable override identity;
    /// @notice Retained bootstrap/default insurance address. It receives no
    ///         premiums until the appropriate insurer explicitly authorizes
    ///         a Carrying pool. Existing deposits keep their original pool.
    address public immutable insurancePool;
    address public immutable override jubilee;
    BuckRenewalConsent public immutable override renewalConsent;
    JubileeClaimBook public immutable override claimBook;

    BuckCash.Book private _cash;
    BuckFacilities.State private _facilities;
    mapping(address => mapping(address => uint256)) private _allowances;
    mapping(address => mapping(address => bytes32)) internal _receiptFragments;
    mapping(address => address) public demurragePayerRequest;
    bool private _configured;
    bool private transient _entered;

    error ZeroCredit(); error ZeroController(); error ZeroIdentityRegistry();
    error ZeroInsurancePool(); error ZeroJubilee(); error InvalidConfiguration();
    error Reentrant(); error SenderNotVerified(); error SpenderNotVerified();
    error RecipientNotVerified(); error InvalidIdentityProof();
    error SelfSponsorship(); error SponsorshipNotRequested(); error AccountNotVerified();
    error SponsorNotVerified(); error SponsoredAccountCarrying(); error SponsorCarrying();
    error AlreadySponsored(); error AccountIsSponsor(); error SponsorIsSponsored();
    error NotSponsored(); error NotSponsorshipParty();
    error DemurrageSponsorUnderfunded(address account, address sponsor, uint256 requiredNetScaled, uint256 availableNetScaled);
    error InsufficientMintFunding(); error NotCreditOwner(); error PremiumRateTooHigh();
    error InsufficientCreditAllocation(); error InsufficientCoverage();
    error SenderApprovalRequired(); error RecipientApprovalRequired();
    error InsufficientSpendableBalance(); error InsufficientNominalBalance(); error InsufficientAllowance();
    error BadAmount(); error CreditExceeded(); error PremiumLimitExceeded(); error NoInsurerPool();
    error InsurerPoolChanged(); error NotCarryingPool(); error PoolNotControlled();
    error NotJubilee(); error BadAuthorization(); error NotRegistry(); error NotGovernance();
    error CarryingIssuerNotApproved(); error ReliefLiquidityLimit(); error FacilityMatured();
    error AllocationWorkLimit(); error AccountOverflow(); error IssuanceOverflow();
    error CarryingModeChanged(); error TimestampOverflow();

    event Minted(address indexed account, uint256 amount, uint256 premium, uint256 creditValue,
                 uint256 buckKValue, uint256 newLimit);
    event ApproveReceipt(address indexed owner, address indexed spender, bytes32 receiptHash);
    event BuckTransferReceipt(address indexed from, address indexed to, uint256 amount,
                              bytes32 fromCipherHash, bytes32 toCipherHash);
    event NetValueTransfer(address indexed from, address indexed to, uint256 nominal, uint256 netScaled);
    event FullValuePayment(address indexed from, address indexed to, uint256 netRaw, uint256 nominalConsumedScaled);
    event FacilityIssued(address indexed owner, uint256 indexed tokenId, uint256 coverage,
                         uint256 principal, uint256 premium, address pool);
    event FacilityReduced(address indexed owner, uint256 indexed tokenId, uint256 principal,
                          uint256 reliefScaled, uint256 nominalFundSpentScaled, uint256 premiumRefund);
    event CashRetired(address indexed owner, uint256 principal, uint256 ownerNetScaled,
                      uint256 ownerNominalScaled, uint256 fundNetScaled, uint256 fundNominalScaled);
    event DemurragePayerRequested(address indexed account, address indexed payer);
    event DemurragePayerSet(address indexed account, address indexed payer);
    event DemurragePayerCleared(address indexed account, address indexed payer);
    event DemurrageSponsor(address indexed account, address indexed sponsor);
    event DemurrageSponsored(address indexed account, address indexed sponsor, uint256 netScaled);
    event InsurerPoolSet(address indexed insurer, address indexed pool);
    event CarryingIssuerSet(address indexed account, bool approved);
    event AccountingActivated();
    event ReductionAuthorized(address indexed owner, uint256 indexed tokenId, uint256 principal,
                              uint256 minimumReliefScaled, uint64 deadline);
    event FacilityRenewed(address indexed owner, uint256 indexed tokenId, uint256 relief,
                          uint256 additionalIssuance, uint256 principal, uint256 nominalFundSpentScaled);
    event MaturedFacilityReleased(address indexed owner, uint256 indexed tokenId, uint256 deferredPrincipal);
    event MaturedPremiumRefunded(address indexed owner, uint256 indexed tokenId, address indexed pool, uint256 nominal);
    event DeferredJubileeBurned(uint256 principal, uint256 nominalFundSpentScaled);

    /// @dev Token-wide transient mutex, retained from Buck. Any cross-account
    ///      reentry reverts. Valuation-only wheel reads can observe state;
    ///      mutating basket/insurer work must finish before token settlement.
    modifier nonReentrant() {
        if (_entered) revert Reentrant();
        _entered = true;
        _;
        _entered = false;
    }

    /// @dev The original four argument meanings are unchanged. Jubilee is an
    ///      explicit fifth dependency, never a repurposed insurance address.
    constructor(address credit_, address controller_, address identity_, address insurancePool_, address jubilee_) {
        if (credit_ == address(0)) revert ZeroCredit();
        if (controller_ == address(0)) revert ZeroController();
        if (identity_ == address(0)) revert ZeroIdentityRegistry();
        if (insurancePool_ == address(0)) revert ZeroInsurancePool();
        if (jubilee_ == address(0)) revert ZeroJubilee();
        buckCredit = BuckCredit(credit_);
        buckK = IBuckK(controller_);
        identity = IdentityRegistry(identity_);
        insurancePool = insurancePool_;
        jubilee = jubilee_;
        renewalConsent = new BuckRenewalConsent(address(this), credit_);
        claimBook = new JubileeClaimBook(address(this), credit_);
        _cash.initialize(jubilee_);
    }

    function name() external pure returns (string memory) { return "Alberta Buck"; }
    function symbol() external pure returns (string memory) { return "BUCK"; }
    function decimals() external pure returns (uint8) { return BuckTypes.DECIMALS; }
    function totalSupply() external view returns (uint256) { return _cash.issued; }
    function totalIssued() external view returns (uint256) { return _cash.issued; }

    /// @notice Cash only. Sponsored displays are protected targets; an
    ///         underfunded sponsor prevents spending until funded or detached.
    function balanceOf(address account) public view returns (uint256) { return _cash.balanceOf(account); }
    function netBalanceOf(address account) external view returns (uint256) { return _cash.netOf(account) / FRACTION_SCALE; }
    function netBalanceScaled(address account) external view returns (uint256) { return _cash.netOf(account); }
    function accountBalancesScaled(address account) external view returns (uint256 gross, uint256 net) {
        return _cash.grossAndNetOf(account);
    }
    /// @notice Ordinary cash has no separate unpaid fee lien. Carrying gross
    ///         is nominal; its difference from net is its current carried fee.
    function rawBalanceOf(address account) external view returns (uint256) { return balanceOf(account); }
    function feeOwing(address account) external view returns (uint256) {
        if (!identity.isCarrying(account)) return 0;
        (uint256 gross, uint256 net) = _cash.grossAndNetOf(account);
        return (gross - net) / FRACTION_SCALE;
    }
    function creditLimit(address owner) public view returns (uint256) {
        return Math.mulDiv(buckCredit.totalCurrentValue(owner), buckK.currentBuckK(), K_SCALE);
    }
    function mintsBacked(uint256 id) external view returns (uint256) {
        (, uint256 active,) = buckCredit.creditInfo(id); return active;
    }
    function mintsPrincipal(uint256 id) external view returns (uint256) { return _facilities.premium[id]; }
    function premiumPrincipal(uint256 id) external view override returns (uint256) { return _facilities.premium[id]; }
    function facilityPool(uint256 id) external view override returns (address) { return _facilities.facilityPool[id]; }
    function insurerPool(address insurer) external view returns (address) { return _facilities.insurerPool[insurer]; }
    function poolInsurer(address pool) external view returns (address) { return _facilities.poolInsurer[pool]; }
    function approvedCarryingIssuer(address owner) external view returns (bool) { return _facilities.carryingIssuer[owner]; }
    function fundBalancesScaled() external view override returns (uint256 gross, uint256 net) { return _cash.fundParts(); }
    function fundFlows() external view returns (BuckCash.FundFlows memory) { return _cash.fundFlows(); }
    function jubileeBalance() external view returns (uint256) { return _cash.balanceOf(jubilee); }
    function jubileeActual() external view returns (uint256) { return _cash.netOf(jubilee) / FRACTION_SCALE; }
    function availableReliefScaled() external view override returns (uint256) { return _cash.availableRelief(); }
    function fundNominalForNet(uint256 netRaw) external view override returns (uint256) { return _cash.fundNominalForNet(netRaw); }
    function maxFundNetPayout() external view override returns (uint256) { return _cash.maxNetPayout(); }
    function allowance(address owner, address spender) external view returns (uint256) { return _allowances[owner][spender]; }

    /// @notice Standard ERC-20 approve.
    /// @dev    Identity is enforced at TRANSFER time, not at approve time.
    ///         A bare allowance only authorises `spender` to *initiate* a
    ///         transferFrom; `_identityCheckedTransfer` then independently
    ///         re-validates that both `from` and `to` are verified and that
    ///         the (from,to) pair satisfies the receipt-fragment / public-
    ///         identity rule.  Because that gate keys on (from,to) -- never
    ///         (from,spender) -- a plain allowance can never manufacture a
    ///         transfer the transfer rules would not already permit: a
    ///         plain-approved spender still cannot move BUCK between two
    ///         non-public parties without a real Chaum-Pedersen receipt
    ///         fragment established by the 4-arg identity-bound `approve`.
    ///
    ///         This makes BUCK a first-class citizen of standard router /
    ///         Permit2 infrastructure (which requires a plain
    ///         `approve(PERMIT2, max)`) without weakening any
    ///         counterparty-privacy invariant.  The 4-arg identity-bound
    ///         `approve` remains the only path that lays down the receipt
    ///         fragment + `markApproved` carrying-flag freeze required for
    ///         confidential (non-public) counterparty transfers.
    function approve(address spender, uint256 amount) external returns (bool) {
        _approve(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external nonReentrant returns (bool) {
        _identityCheckedTransfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount)
        external nonReentrant returns (bool)
    {
        _spendAllowance(from, msg.sender, amount);
        _identityCheckedTransfer(from, to, amount);
        return true;
    }

    // ---- identity-bound approve --------------------------------------------

    function approve(
        address spender,
        uint256 amount,
        IdentityRegistry.ElGamalCT calldata E_bob,
        IdentityRegistry.CPProof calldata pi_CP
    ) external nonReentrant returns (bool) {
        if (!(identity.isVerified(msg.sender))) revert SenderNotVerified();
        if (!(identity.isVerified(spender))) revert SpenderNotVerified();
        if (!(identity.verifyApprove(msg.sender, spender, E_bob, pi_CP))) revert InvalidIdentityProof();
        bytes32 receipt = _ciphertextHash(E_bob);
        _receiptFragments[msg.sender][spender] = receipt;
        emit ApproveReceipt(msg.sender, spender, receipt);
        identity.markApproved(spender);
        _approve(msg.sender, spender, amount);
        return true;
    }

    function receiptFragment(address from, address to) external view returns (bytes32) {
        return _receiptFragments[from][to];
    }

    function _identityCheckedTransfer(address from, address to, uint256 amount) internal {
        _ready();
        BuckTransfer.send(_cash, _receiptFragments, identity, from, to, amount);
    }

    // ---- complete wiring and authorized modes ------------------------------

    /// @notice Permissionless one-time readiness check after fund configuration.
    /// @dev All participants are immutable/one-shot bound. No monetary action
    ///      is allowed against an unconfigured fund or mismatched registry/NFT.
    function activateAccounting() external nonReentrant { _ready(); }

    function _ready() private {
        if (_configured) return;
        if (buckCredit.buck() != address(this) || identity.buck() != address(this)
            || jubilee.code.length == 0 || IBuckFund(jubilee).token() != address(this)
            || !identity.isVerified(jubilee) || !identity.isCarrying(jubilee)) revert InvalidConfiguration();
        identity.enableCarryingModeHook();
        _configured = true;
        emit AccountingActivated();
    }

    function setApprovedCarryingIssuer(address account, bool approved) external nonReentrant {
        if (msg.sender != identity.governance()) revert NotGovernance();
        if (approved && (account.code.length == 0 || !identity.isVerified(account)
                         || account == jubilee)) revert NotCarryingPool();
        _facilities.carryingIssuer[account] = approved;
        emit CarryingIssuerSet(account, approved);
    }

    /// @dev Revocation blocks new issuance, not repayments. Registry mode
    ///      changes settle before reinterpretation; sponsorship cannot become
    ///      Carrying, and the fund/registered insurer pools must stay Carrying.
    function onCarryingModeChange(address account, bool carrying) external nonReentrant {
        if (msg.sender != address(identity)) revert NotRegistry();
        if (account == jubilee) { if (!carrying) revert NotCarryingPool(); return; }
        if (!carrying && _facilities.poolInsurer[account] != address(0)) revert NotCarryingPool();
        if (carrying && buckCredit.hasIssuedPrincipal(account) && !_facilities.carryingIssuer[account]) {
            revert CarryingIssuerNotApproved();
        }
        _cash.syncMode(account, carrying);
    }

    function setInsurerPool(address pool) external nonReentrant {
        if (!identity.isVerified(msg.sender) || !identity.isVerified(pool)) revert AccountNotVerified();
        if (!identity.isCarrying(pool) || pool == jubilee) revert NotCarryingPool();
        if (pool != msg.sender && identity.binderOf(pool) != msg.sender) revert PoolNotControlled();
        address registered = _facilities.poolInsurer[pool];
        if (registered != address(0) && registered != msg.sender) revert PoolNotControlled();
        _facilities.poolInsurer[pool] = msg.sender;
        _facilities.insurerPool[msg.sender] = pool;
        emit InsurerPoolSet(msg.sender, pool);
    }

    // ---- sponsorship: whole payment or whole transaction rollback ----------

    function demurragePayer(address account) external view returns (address) { return _cash.sponsor[account]; }
    function sponseeCount(address payer) external view returns (uint32) { return _cash.dependants[payer]; }
    function isSponsored(address account) external view returns (bool) { return _cash.sponsor[account] != address(0); }
    function settleDemurrage(address account) external nonReentrant { _ready(); _cash.settle(account); }
    function requestDemurragePayer(address payer) external nonReentrant {
        if (payer == msg.sender) revert SelfSponsorship();
        demurragePayerRequest[msg.sender] = payer;
        emit DemurragePayerRequested(msg.sender, payer);
    }
    /// @dev Both parties consent and are verified ordinary accounts, with no
    ///      self-link or chains. Existing fees settle before protection starts.
    function acceptDemurragePayer(address account) external nonReentrant {
        if (demurragePayerRequest[account] != msg.sender) revert SponsorshipNotRequested();
        if (!identity.isVerified(account)) revert AccountNotVerified();
        if (!identity.isVerified(msg.sender)) revert SponsorNotVerified();
        if (identity.isCarrying(account)) revert SponsoredAccountCarrying();
        if (identity.isCarrying(msg.sender)) revert SponsorCarrying();
        _ready();
        _cash.establishSponsor(account, msg.sender);
        delete demurragePayerRequest[account];
    }
    /// @dev Either party may exit without sponsor funding. Detach first;
    ///      outstanding normal fees fall on the account, previously paid
    ///      sponsorship periods stay paid. No partial or automatic fallback.
    function clearDemurragePayer(address account) external nonReentrant {
        address payer = _cash.sponsor[account];
        if (payer == address(0)) revert NotSponsored();
        if (msg.sender != account && msg.sender != payer) revert NotSponsorshipParty();
        _ready(); _cash.clearSponsor(account);
    }

    // ---- original numeric mint/burn and quote surfaces ----------------------

    /// @notice Increase balanceOf by at least amount after insurance funding.
    ///         Max requests the maximum found in cheapest-first NFT order.
    function mint(uint256 amount) external nonReentrant {
        _ready(); BuckFacilities.mint(_cash, _facilities, _receiptFragments, _deps(), amount,
                                    BuckFacilities.select(buckCredit, msg.sender, true));
    }
    function mint(uint256 amount, uint256[] calldata ids) external nonReentrant {
        _ready(); BuckFacilities.mint(_cash, _facilities, _receiptFragments, _deps(), amount, ids);
    }
    /// @notice Reduce actual balanceOf by at least amount after relief and
    ///         nominal refunds, or revert everything. Amount is net for ordinary
    ///         holders and nominal for Carrying services. No max sentinel.
    function burn(uint256 amount) external nonReentrant {
        _ready(); BuckFacilities.burn(_cash, _facilities, _allowances, _receiptFragments, _deps(), amount,
                                    BuckFacilities.select(buckCredit, msg.sender, false));
    }
    function burn(uint256 amount, uint256[] calldata ids) external nonReentrant {
        _ready(); BuckFacilities.burn(_cash, _facilities, _allowances, _receiptFragments, _deps(), amount, ids);
    }
    function quoteMint(uint256 amount, uint256[] calldata ids)
        external view returns (uint256 totalCoverage, uint256 poolPrincipal)
    {
        BuckFacilities.MintPlan memory p = BuckFacilities.planMint(_cash, _facilities, _deps(), amount,
                                                                  ids, buckK.currentBuckK());
        return (p.coverage, p.premium);
    }
    /// @dev Advisory last guarded mark; execution refreshes the existing wheel.
    ///      Both paths carry complete virtual batch state, including shared
    ///      insurer pools, sponsor funds and the current block's relief budget.
    function quoteBurn(uint256 amount, uint256[] calldata ids)
        external view returns (uint256 totalUnwind, uint256 poolRefund)
    {
        BuckFacilities.BurnPlan memory p = BuckFacilities.planBurn(_cash, _facilities, _allowances, _deps(),
            amount, ids, buckK.currentBuckK(), IBuckFund(jubilee).reliefPositionMark());
        return (p.coverage, p.refund);
    }
    /// @notice Activate coverage and issue the requested spendable cash.
    /// @dev Positive coverage with zero spendable is permitted. Any premium
    ///      is still funded/issued in full; zero premium creates no currency,
    ///      principal or claim age. Both numeric mint/burn APIs are unchanged.
    function issue(uint256 id, uint256 coverage, uint256 spendable, uint256 maxPremium, address expectedPool)
        external nonReentrant returns (uint256 principal, uint256 premium)
    {
        _ready();
        uint256 k = buckK.compute();
        uint256 factor = buckK.fundingFactor();
        (principal, premium) = BuckFacilities.issue(_cash, _facilities, _receiptFragments, _deps(),
            BuckFacilities.Issue(id, coverage, spendable, maxPremium, expectedPool));
        IBuckFund(jubilee).checkpointIssuance();
        if (buckK.currentBuckK() != k || buckK.fundingFactor() != factor) revert CreditExceeded();
    }
    function _deps() private view returns (BuckFacilities.Dependencies memory) {
        return BuckFacilities.Dependencies(buckCredit, IBuckIssuanceK(address(buckK)), identity, claimBook, jubilee);
    }

    // ---- external Jubilee coordinator: authorized liquid settlement --------

    function reductionAuthorization(uint256 id) external view override
        returns (uint256 principal, uint256 minimumReliefScaled, uint64 deadline)
    {
        BuckFacilities.Authorization memory a = _facilities.authorization[id];
        return (a.principal, a.minimumReliefScaled, a.deadline);
    }
    function authorizeReduction(uint256 id, uint256 principal, uint256 minimumReliefScaled, uint64 deadline)
        external nonReentrant
    {
        if (buckCredit.ownerOf(id) != msg.sender) revert NotCreditOwner();
        if (principal == 0) delete _facilities.authorization[id];
        else {
            (uint256 issued,) = buckCredit.issuanceOf(id);
            if (principal > issued || minimumReliefScaled > principal * FRACTION_SCALE || deadline < block.timestamp) {
                revert BadAuthorization();
            }
            _facilities.authorization[id] = BuckFacilities.Authorization(principal, minimumReliefScaled, deadline);
        }
        emit ReductionAuthorized(msg.sender, id, principal, minimumReliefScaled, deadline);
    }
    function settleFromJubilee(address owner, uint256 id, uint256 principal, uint256 reliefScaled)
        external override nonReentrant returns (uint256 refund)
    {
        if (msg.sender != jubilee) revert NotJubilee();
        _ready();
        BuckFacilities.Authorization memory a = _facilities.authorization[id];
        if (principal == 0 || principal != a.principal || block.timestamp > a.deadline
            || reliefScaled < a.minimumReliefScaled) revert BadAuthorization();
        _cash.consumeRelief(reliefScaled);
        claimBook.totalSeconds();
        return BuckFacilities.settle(_cash, _facilities, _allowances, _receiptFragments, _deps(),
                                      owner, id, principal, reliefScaled);
    }

    /// @notice Renew one facility atomically using its owner's and insurer's
    ///         exact consent. Insurer preparation happens before this lock.
    function renewFromJubilee(address owner, uint256 tokenId, BuckRenewal.Terms calldata terms, uint256 relief)
        external override nonReentrant returns (uint256 refund, uint256 premium)
    {
        if (msg.sender != jubilee) revert NotJubilee();
        _ready();
        return BuckRenewalOps.renew(_cash, _facilities, _allowances, _receiptFragments,
                                   _deps(), renewalConsent, owner, tokenId, terms, relief);
    }

    /// @notice Release a 50-year average-age lien regardless of fund liquidity.
    /// @dev The coordinator authenticates the owner. Principal stays issued
    ///      as deferred fund debt until actually burned; the original insurer
    ///      refund survives as an independently payable nominal claim.
    function matureFromJubilee(address owner, uint256 id) external override nonReentrant returns (uint256 principal) {
        if (msg.sender != jubilee) revert NotJubilee();
        _ready();
        if (buckCredit.ownerOf(id) != owner) revert NotCreditOwner();
        principal = claimBook.deferMatured(id);
        buckCredit.recordReductionFromBuck(id, owner, principal);
        (, uint256 active,) = buckCredit.creditInfo(id);
        if (active != 0) buckCredit.deactivateFromBuck(id, owner, active);
        delete _facilities.facilityPool[id]; delete _facilities.premium[id]; delete _facilities.authorization[id];
        buckCredit.closeFromBuck(id, owner);
        emit MaturedFacilityReleased(owner, id, principal);
    }
    function claimMaturedPremium(uint256 id) external override nonReentrant returns (uint256 nominal) {
        _ready();
        (address owner, address pool, uint256 amount) = claimBook.consumePremiumRefund(id);
        if (!identity.isVerified(pool) || !identity.isCarrying(pool)) revert NotCarryingPool();
        _spendAllowance(pool, address(this), amount);
        _identityCheckedTransfer(pool, owner, amount);
        emit MaturedPremiumRefunded(owner, id, pool, amount);
        return amount;
    }
    function burnDeferredFromJubilee(uint256 principal) external override nonReentrant {
        if (msg.sender != jubilee) revert NotJubilee();
        _ready();
        if (principal > type(uint256).max / FRACTION_SCALE) revert BadAmount();
        _cash.consumeRelief(principal * FRACTION_SCALE);
        uint256 beforeNominal = _cash.balanceOf(jubilee);
        uint256 nominal = _cash.retireFundDebt(principal);
        claimBook.consumeDeferred(principal);
        emit Transfer(jubilee, address(0), beforeNominal - _cash.balanceOf(jubilee));
        emit DeferredJubileeBurned(principal, nominal);
    }
    function claimSeconds() external view override returns (uint256) { return claimBook.totalSeconds(); }
    function maturitiesPending() external view returns (bool) { return claimBook.pending(); }
    function processMaturities(uint256 maxSteps) external nonReentrant returns (uint256, bool) { return claimBook.process(maxSteps); }
    function selectedClaimSeconds(uint256 id, uint256 principal) external view override returns (uint256 selected, uint256 total) {
        total = claimBook.totalSeconds();
        (uint256 issued, uint256 age) = buckCredit.issuanceOf(id);
        if (issued == 0 || principal > issued) revert BadAmount();
        selected = Math.mulDiv(age, principal, issued);
    }

    // ---- ERC-20 internals --------------------------------------------------

    function _approve(address owner, address spender, uint256 amount) internal {
        _allowances[owner][spender] = amount;
        emit Approval(owner, spender, amount);
    }

    function _spendAllowance(address owner, address spender, uint256 amount) internal {
        uint256 cur = _allowances[owner][spender];
        if (cur != type(uint256).max) {
            if (!(cur >= amount)) revert InsufficientAllowance();
            unchecked { _allowances[owner][spender] = cur - amount; }
        }
    }

    // ---- helpers -----------------------------------------------------------

    function _ciphertextHash(IdentityRegistry.ElGamalCT calldata E)
        internal pure returns (bytes32)
    {
        return keccak256(abi.encode(E.R.X, E.R.Y, E.C.X, E.C.Y));
    }
}
