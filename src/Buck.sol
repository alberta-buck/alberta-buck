// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}         from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {Math}           from "@openzeppelin/contracts/utils/math/Math.sol";

import {BN254}            from "./BN254.sol";
import {BuckTypes, BuckQty, BuckSeconds, CreditSlice, toBuckQty, toBuckQtySigned, toBuckSeconds} from "./BuckTypes.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";

/// @title Buck — identity-bound ERC-20 with single-slot per-account state.
///
/// All transfer-path state for an account fits in one storage slot
/// (AccountState).  Demurrage runs against the packed `balance` field; the
/// Jubilee receives system-level demurrage credit via direct slot writes
/// (`totalSupply` is NOT mutated by demurrage -- only by user mint / burn).
///
/// Mint/burn-side bookkeeping (storedLimit, mintsBacked, allowances, receipt
/// fragments) lives in separate maps because it's touched per-mint, not per
/// transfer.  This keeps the hot path to one SSTORE per side per transfer.
interface IBuckK {
    function currentBuckK() external view returns (uint256);
    /// @dev State-changing accessor.  Runs a PID cycle if `dT` has elapsed,
    ///      otherwise returns the cached value.  Buck mints/burns call this
    ///      so user activity drives (and amortizes) PID work.
    function compute() external returns (uint256);
    /// @dev Counter-cyclical insurance funding factor (18-dec; 1e18 == 1.0).
    ///      Buck.mint gates on
    ///        balanceOf(minter) >= poolPrincipal * fundingFactor / 1e18.
    ///      The Static controller returns 0 (gate disabled); the PID
    ///      controller returns max(0, 1e18 + 10*(basket-BUCK)*1e18/basket).
    function fundingFactor() external view returns (uint256);
}

interface IBuckCredit {
    function totalCurrentValue(address holder) external view returns (uint256);
    function balanceOf(address owner) external view returns (uint256);
    function ownerOf(uint256 tokenId) external view returns (address);
    function tokenOfOwnerByIndex(address owner, uint256 index) external view returns (uint256);
    function creditInfo(uint256 tokenId)
        external view returns (uint256 faceValue, uint256 activatedValue, uint32 premiumRate);
    function batchCreditInfo(uint256[] calldata tokenIds)
        external view returns (CreditSlice[] memory slices);
    function activateFromBuck(uint256 tokenId, address holder, uint256 amount) external;
    function deactivateFromBuck(uint256 tokenId, address holder, uint256 amount) external;
}

contract Buck is IERC20, IERC20Metadata {

    // ---- immutables --------------------------------------------------------

    IBuckCredit       public immutable buckCredit;
    IBuckK            public immutable buckK;
    IdentityRegistry  public immutable identity;
    address           public immutable insurancePool;

    // ---- constants ---------------------------------------------------------

    /// @dev BuckK fixed-point scale.  IBuckK.currentBuckK() returns a 1e18
    ///      ratio; division by this scale converts (BUCK * buckK) back to BUCK.
    ///      Sourced from BuckTypes so all BUCK-denominated math shares one
    ///      authoritative constant.
    uint256 internal constant BUCKK_SCALE        = BuckTypes.BUCKK_SCALE;
    uint256 internal constant SCALE              = 1e27;
    uint256 internal constant BASE_RATE_PER_YEAR = 2e25;                // 0.02 in SCALE
    uint256 internal constant SECONDS_PER_YEAR_  = 365 days + 6 hours;
    /// @dev Integer division truncation loses ~3e-19 per second relative,
    ///      or ~1e-11 annually.  This is below the resolution of 6-decimal
    ///      BUCK and far smaller than the base rate itself.
    uint256 internal constant BASE_RATE_PER_SEC  = BASE_RATE_PER_YEAR / SECONDS_PER_YEAR_;

    uint256 internal constant BP                 = 10000;
    uint256 internal constant POOL_ROI_INV       = 10;                  // 10% assumed annual ROI

    // ---- packed per-account state ------------------------------------------
    //
    //   balance      uint80   raw stored balance.  Spendable (Non-Carrying):
    //                         balance - feeOwing.  Cap = 2^80-1 ≈ 1.21e24
    //                         (= 1.21e18 BUCK at 6 decimals).
    //
    //   buckSeconds  uint120  cumulative integral of (balance * dt)
    //                         crystallised through `timestamp`.
    //                         feeOwing(a) = (buckSeconds + balance*elapsed)
    //                                       * BASE_RATE_PER_SEC / SCALE
    //
    //   timestamp    uint40   last crystallisation (seconds since epoch).
    //                         2^40 sec ≈ year 36812 -- safe past 2038.
    //
    //   flags        uint16   reserved for future per-account flags.

    struct AccountState {
        BuckQty     balance;       // uint80 underlying; cap = BuckTypes.MAX_BALANCE
        BuckSeconds buckSeconds;   // uint120 underlying; cap = BuckTypes.MAX_BS
        uint40      timestamp;
        uint16      flags;
    }
    mapping(address => AccountState) internal _state;

    // ---- ERC-20 supply + allowances ----------------------------------------

    uint256 private _totalSupply;
    mapping(address => mapping(address => uint256)) private _allowances;

    // ---- mint-side bookkeeping (rare path) ---------------------------------

    /// @notice Highest-ever credit limit observed for this account.
    mapping(address => uint256) public storedLimit;
    /// @notice Outstanding BUCK coverage backed by a given BuckCredit NFT.
    mapping(uint256 => uint256) public mintsBacked;
    /// @dev keccak256(E_to) per (from, to) from approve-time CP receipts.
    mapping(address => mapping(address => bytes32)) internal _receiptFragments;

    // ---- Jubilee accrual checkpoint ----------------------------------------

    /// @dev Timestamp through which Jubilee accrual has been applied.
    ///      Mint/burn -> _accrueJubilee writes
    ///        _state[address(this)].balance += totalSupply * RATE * elapsed
    ///      directly into Jubilee's slot.  totalSupply is NOT mutated --
    ///      demurrage is internal redistribution, not minting.
    ///
    ///      Exact invariant: sum_a(rawBalance(a)) == totalSupply + jubileeActual.
    ///
    ///      Every BUCK has exactly one fee owner:
    ///        Non-Carrying: locked silently inside raw balance
    ///                      (balanceOf = raw - feeOwing).
    ///        Carrying:     balanceOf == raw; Jubilee pre-accrues their share.
    ///      When Carrying BUCKs flow to non-Carrying via _carryingTransfer,
    ///      liveBs*value/raw of the sender's full live integral propagates
    ///      to the recipient -- Jubilee pre-accrued it; it now debits in
    ///      exact proportion to the BUCKs transferred.
    uint64 internal _jubileeLastUpdate;

    // ---- Direct-mint integration -------------------------------------------
    //
    // BuckBasket is the privileged caller of mintFromBasket / burnFromBasket
    // for the TOKEN-presentation (direct-mint) path.  Wired post-deploy by
    // `setBasket(address)` so the basket can be constructed with Buck's
    // address.  Once set, the field is immutable in effect (further
    // setBasket calls revert).
    //
    // Placed last in the storage layout so the existing slot positions of
    // _state / _totalSupply / _allowances / storedLimit / mintsBacked /
    // _receiptFragments / _jubileeLastUpdate (which tests reach via
    // `vm.store(..., slot, ...)`) remain unchanged.
    address public basket;

    // ---- Credit-limit cache ------------------------------------------------
    //
    // creditLimit(a) = totalCurrentValue(a) * currentBuckK / 1e18 -- live sum
    // over the holder's BuckCredit NFTs.  Caching per block avoids re-scanning
    // NFTs in the (common) case where the same account makes multiple
    // transfers in one block.  BuckCredit invalidates the cache via the
    // onCreditMutation hook on every NFT mint / burn / transfer / activate /
    // updateCredit.
    //
    // Appended after `basket` so existing slot positions are preserved (see
    // the `vm.store` consumers enumerated in the Phase-1 plan).
    mapping(address => uint256) public creditLimitCache;
    mapping(address => uint64)  public creditLimitBlock;

    // ---- premium / mutual-insurance pool model -----------------------------
    //
    // mint(N) delivers N to the holder + a mutual-insurance pool deposit of
    // (annual_premium * POOL_ROI_INV) to insurancePool, both drawn against
    // BuckCredit NFT capacity cheapest-first to minimise the holder's premium
    // cost.  Per-NFT inversion:
    //     take = ceil(remaining * BP / (BP - rate * POOL_ROI_INV))
    //
    // burn(N) walks the holder's NFTs most-expensive-first.  This frees the
    // most expensive coverage capacity and returns the largest pool principal
    // per BUCK burned (the holder's mutual-insurance investment unwound
    // dearest-side first).  The asymmetry is rate-neutral: per-NFT inversion is
    // symmetric (same `denom` on both sides), so a mint-burn round-trip on the
    // same NFT restores its mintsBacked exactly -- no arbitrage from the
    // differing default selectors.

    // ---- events ------------------------------------------------------------

    event Minted(
        address indexed account,
        uint256 amount,
        uint256 premium,
        uint256 creditValue,
        uint256 buckKValue,
        uint256 newLimit
    );
    event ApproveReceipt(address indexed owner, address indexed spender, bytes32 receiptHash);
    event BuckTransferReceipt(
        address indexed from,
        address indexed to,
        uint256 amount,
        bytes32 fromCipherHash,
        bytes32 toCipherHash
    );
    event JubileeAccrued(uint256 delta, uint256 newJubileeBalance);

    // ---- constructor -------------------------------------------------------

    constructor(
        address _buckCredit,
        address _buckK,
        address _identity,
        address _insurancePool
    ) {
        require(_buckCredit    != address(0), "buckCredit=0");
        require(_buckK         != address(0), "buckK=0");
        require(_identity      != address(0), "identity=0");
        require(_insurancePool != address(0), "insurancePool=0");
        buckCredit    = IBuckCredit(_buckCredit);
        buckK         = IBuckK(_buckK);
        identity      = IdentityRegistry(_identity);
        insurancePool = _insurancePool;
        _jubileeLastUpdate = uint64(block.timestamp);
    }

    // ---- IERC20Metadata ----------------------------------------------------

    function name()     external pure returns (string memory) { return "Alberta Buck"; }
    function symbol()   external pure returns (string memory) { return "BUCK";         }
    function decimals() external pure returns (uint8)         { return BuckTypes.DECIMALS; }

    // ---- IERC20 ------------------------------------------------------------

    function totalSupply() external view returns (uint256) { return _totalSupply; }

    /// @notice ERC-20 spendable balance.  Includes positive held BUCK (net
    ///         of demurrage for non-Carrying accounts) plus *unused credit
    ///         headroom* (creditLimit - debt) for non-Carrying accounts.
    ///         The user-facing semantic: "what alice can spend right now."
    ///
    ///         Carrying accounts (AMM pools, Notes, Jubilee) hold no NFT-
    ///         backed credit and cannot go negative; balanceOf returns
    ///         raw for them.
    function balanceOf(address a) public view returns (uint256) {
        AccountState storage s = _state[a];
        int256 raw = s.balance.asInt();
        if (identity.isCarrying(a)) {
            return raw > 0 ? uint256(raw) : 0;
        }
        // Non-carrying: held + unused credit.
        uint256 held = 0;
        if (raw > 0) {
            uint256 rawU = uint256(raw);
            uint256 fee = _feeOwing(s, rawU);
            held = fee >= rawU ? 0 : rawU - fee;
        }
        uint256 debt = raw < 0 ? uint256(-raw) : 0;
        uint256 limit = creditLimit(a);
        uint256 unusedCredit = limit > debt ? limit - debt : 0;
        return held + unusedCredit;
    }

    /// @notice Signed view of an account's balance after demurrage.  Returns
    ///         the negative raw value directly for debt-bearing accounts
    ///         (those that have spent into BuckCredit-backed headroom).  Does
    ///         NOT include credit headroom -- use `balanceOf` for the
    ///         ERC-20 visible "what can I spend" semantic.
    function signedBalanceOf(address a) public view returns (int256) {
        AccountState storage s = _state[a];
        int256 raw = s.balance.asInt();
        if (raw <= 0) return raw;          // debt accrues no demurrage (clamped in _feeOwing)
        if (identity.isCarrying(a)) return raw;
        uint256 fee = _feeOwing(s, uint256(raw));
        return raw - int256(fee);
    }

    function allowance(address owner, address spender) external view returns (uint256) {
        return _allowances[owner][spender];
    }

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

    function transfer(address to, uint256 amount) external returns (bool) {
        _identityCheckedTransfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
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
    ) external returns (bool) {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");
        require(identity.isVerified(spender),    "BUCK: spender not verified");
        require(
            identity.verifyApprove(msg.sender, spender, E_bob, pi_CP),
            "BUCK: bad CP proof"
        );
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

    // ---- Credit-limit machinery (NFT-backed negative-balance headroom) ----

    /// @notice Live credit limit (NFT-backed BUCK headroom) for account `a`.
    /// @dev    Formula: ~totalCurrentValue(a) * currentBuckK / BUCKK_SCALE~.
    ///         Sum of depreciated activated BuckCredit values scaled by the
    ///         current PID multiplier.  Cached per block to avoid re-scanning
    ///         a holder's NFT list across multiple transfers in the same
    ///         block; the cache is invalidated by BuckCredit via the
    ///         `onCreditMutation` hook on every NFT mint / burn / transfer /
    ///         activate / updateCredit.
    function creditLimit(address a) public view returns (uint256) {
        if (creditLimitBlock[a] == uint64(block.number)) {
            return creditLimitCache[a];
        }
        return _computeCreditLimit(a);
    }

    /// @dev Pure computation of the live credit limit; no cache read/write.
    function _computeCreditLimit(address holder) internal view returns (uint256) {
        uint256 cv = buckCredit.totalCurrentValue(holder);
        if (cv == 0) return 0;
        uint256 bk = buckK.currentBuckK();
        return cv * bk / BUCKK_SCALE;
    }

    /// @dev Refresh the per-block cache.  Called from any non-view path that
    ///      needs the credit limit (mint, burn, negative-going transfer).
    function _refreshCreditLimit(address holder) internal returns (uint256 limit) {
        if (creditLimitBlock[holder] == uint64(block.number)) {
            return creditLimitCache[holder];
        }
        limit = _computeCreditLimit(holder);
        creditLimitCache[holder] = limit;
        creditLimitBlock[holder] = uint64(block.number);
    }

    /// @dev Mark the cache stale for `holder` so the next read recomputes.
    function _invalidateCreditCache(address holder) internal {
        if (holder == address(0)) return;
        creditLimitBlock[holder] = 0;
    }

    /// @notice Hook called by BuckCredit on every NFT state change to
    ///         invalidate Buck's per-block credit-limit cache.  Restricted
    ///         to the registered BuckCredit contract.
    function onCreditMutation(address from, address to) external {
        require(msg.sender == address(buckCredit), "BUCK: not credit");
        _invalidateCreditCache(from);
        _invalidateCreditCache(to);
    }

    // ---- mint / burn -------------------------------------------------------

    function mint(uint256 amount) external {
        _mintAllocated(amount, _selectCheapest(msg.sender));
    }

    function mint(uint256 amount, uint256[] calldata tokenIds) external {
        _mintAllocated(amount, tokenIds);
    }

    /// @notice Burn `amount` BUCK.  Coverage is unwound most-expensive-first
    ///         so the dearest insurance is released first, returning the
    ///         largest pool principal per BUCK burned and freeing expensive
    ///         capacity for re-use.
    function burn(uint256 amount) external {
        _burnAllocated(amount, _selectMostExpensive(msg.sender));
    }

    function burn(uint256 amount, uint256[] calldata tokenIds) external {
        _burnAllocated(amount, tokenIds);
    }

    // ---- Direct-mint path (TOKEN-presentation via BuckBasket) -------------

    /// @notice One-shot wiring of the BuckBasket address; immutable thereafter.
    /// @dev    Must be set by `insurancePool` (which is governance-bound at
    ///         deploy) so that the basket address is locked under the same
    ///         authority that holds the system's mutual reserves.
    function setBasket(address _basket) external {
        require(msg.sender == insurancePool, "BUCK: not insurancePool");
        require(basket == address(0), "BUCK: basket already set");
        require(_basket != address(0), "BUCK: basket=0");
        basket = _basket;
    }

    /// @notice Mint `amount` BUCK to `to`.  Bypasses the BuckCredit /
    ///         funding-factor machinery -- direct-mint BUCK is backed by
    ///         the TOKEN reserves in BuckBasket's pools, not by insured-
    ///         asset credit.  Only callable by the registered basket.
    function mintFromBasket(address to, uint256 amount) external {
        require(msg.sender == basket && basket != address(0), "BUCK: not basket");
        if (amount == 0) return;
        _accrueJubilee();
        _crystallize(to);
        // _addBalance -> _setBalanceSigned tracks _totalSupply.  BuckBasket
        // is a Carrying account, so its balance always crosses upward through
        // the positive branch and the invariant grows by exactly `amount`.
        _addBalance(to, amount);
        emit Transfer(address(0), to, amount);
    }

    /// @notice Burn `amount` BUCK from BuckBasket's balance.  Only callable
    ///         by the registered basket.  Mirrors mintFromBasket on the
    ///         supply side without consulting credit-NFT machinery.
    function burnFromBasket(uint256 amount) external {
        require(msg.sender == basket && basket != address(0), "BUCK: not basket");
        if (amount == 0) return;
        _accrueJubilee();
        _crystallize(msg.sender);
        int256 raw = _state[msg.sender].balance.asInt();
        require(raw > 0 && uint256(raw) >= amount, "BUCK: insufficient");
        // _subBalance -> _setBalanceSigned tracks _totalSupply.  BuckBasket
        // is Carrying so the post-balance stays >= 0 and the invariant
        // decrements by exactly `amount`.
        _subBalance(msg.sender, amount);
        emit Transfer(msg.sender, address(0), amount);
    }

    /// @notice Quote total coverage / pool principal for delivering `amount`
    ///         net via the supplied tokenIds order.
    function quoteMint(uint256 amount, uint256[] calldata tokenIds)
        external view returns (uint256 totalCoverage, uint256 poolPrincipal)
    {
        return _allocateMintView(amount, tokenIds);
    }

    function quoteBurn(uint256 amount, uint256[] calldata tokenIds)
        external view returns (uint256 totalUnwind, uint256 poolRefund)
    {
        return _allocateBurnView(amount, tokenIds);
    }

    /// @dev Mint flow (Phase 1b): one-step activate + draw.  For each NFT in
    ///      `tokenIds`, _allocateMint computes a `take_i` (insurance-overhead-
    ///      inclusive) and a `principal_i = take_i - amount_i` per the per-NFT
    ///      inversion `take = ceil(amount * BP / (BP - rate * POOL_ROI_INV))`.
    ///      It calls `BuckCredit.activateFromBuck(tid, holder, take_i)` so the
    ///      holder's NFT-backed credit headroom expands by `take_i`, and writes
    ///      `mintsBacked[tid] += take_i`.
    ///
    ///      Insurance pool gets `poolPrincipal` (= sum principal_i) added; the
    ///      minter's signed balance is deducted by the same amount, driving it
    ///      negative (NFT-backed debt).  _setBalanceSigned tracks `_totalSupply`
    ///      across both writes -- net effect on totalSupply: `+poolPrincipal`
    ///      (insurance pool gained positive; minter's positive contribution
    ///      stayed 0 since they went from 0 toward negative).
    ///
    ///      End-state arithmetic:
    ///          creditLimit(alice)  = sum take_i      (= totalCurrentValue * buckK)
    ///          signedRawBalance     = -poolPrincipal  (= -sum principal_i)
    ///          balanceOf            = creditLimit - debt
    ///                               = sum(take_i) - sum(principal_i)
    ///                               = sum amount_i
    ///                               = `amount`        (modulo integer rounding)
    function _mintAllocated(uint256 amount, uint256[] memory tokenIds) internal {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");

        // PID cadence: compute() advances the PID if dT elapsed (cheap cached
        // read otherwise).  totalCreditValue and currentBuckK are captured
        // for the Minted event; the live credit-limit is recomputed below
        // after _allocateMint has run activateFromBuck.
        uint256 totalCreditValue = buckCredit.totalCurrentValue(msg.sender);
        uint256 currentBuckK     = buckK.compute();

        // Walk the holder's NFTs cheapest-first; activates `take_i` on each
        // via BuckCredit.activateFromBuck, writes mintsBacked[tid] += take_i,
        // returns aggregate (totalCoverage = sum take, poolPrincipal = sum
        // principal).  The cap per NFT is `faceValue - mintsBacked` (auto-
        // activation may walk into unactivated capacity).
        (uint256 totalCoverage, uint256 poolPrincipal) = _allocateMint(amount, tokenIds);

        // Counter-cyclical insurance funding gate.  The minter's *current*
        // spendable (positive held + unused credit) must cover poolPrincipal
        // * fundingFactor / 1e18 -- so that under-valued-BUCK regimes
        // throttle fresh credit issuance.
        if (poolPrincipal > 0) {
            uint256 factor = buckK.fundingFactor();
            if (factor > 0) {
                uint256 required = poolPrincipal * factor / BUCKK_SCALE;
                require(
                    balanceOf(msg.sender) >= required,
                    "BUCK: insufficient mint funding"
                );
            }
        }

        // Settlement: insurance pool receives `poolPrincipal`, minter is
        // debited the same amount.  _setBalanceSigned tracks _totalSupply
        // across both writes so the invariant
        //   _totalSupply == sum_a max(0, signedRaw(a))
        // is maintained.
        _accrueJubilee();
        if (poolPrincipal > 0) {
            _crystallize(insurancePool);
            _addBalance(insurancePool, poolPrincipal);
            _crystallize(msg.sender);
            _subBalance(msg.sender, poolPrincipal);
            // Per-side Transfer events.  The minter→pool transfer is a real
            // BUCK flow; we emit it as `from -> insurancePool` for
            // observability (the BUCK is freshly minted into the pool from
            // the minter's credit, not from a pre-held positive balance).
            emit Transfer(address(0), insurancePool, poolPrincipal);
        }

        // The Minted event keeps its historical shape; `newLimit` now refers
        // to the live creditLimit after activation, not a stored ratchet.
        emit Minted(
            msg.sender,
            totalCoverage,
            poolPrincipal,
            totalCreditValue,
            currentBuckK,
            creditLimit(msg.sender)
        );
    }

    /// @dev Burn flow (Phase 1b): symmetric to mint -- closes NFT-backed
    ///      credit positions.  For each NFT in `tokenIds` (most-expensive-
    ///      first by default), _allocateBurn computes the unwind take per
    ///      NFT and refunds the insurance principal back to the holder.
    ///      `BuckCredit.deactivateFromBuck(tid, holder, unwind_i)` shrinks
    ///      the holder's activatedValue (and thus creditLimit) by the
    ///      unwound take; `mintsBacked[tid] -= unwind_i`; insurance pool's
    ///      raw shrinks by `poolRefund` (= sum principal); holder's signed
    ///      raw grows by `poolRefund` (climbs out of debt or into positive).
    ///
    ///      End-state arithmetic:
    ///          creditLimit drops by sum take_i
    ///          signedRawBalance += poolRefund  (toward zero from debt;
    ///                                           into positive from zero)
    ///          balanceOf change = -sum amount_i (the user's "spendable"
    ///                                            shrinks by exactly `amount`)
    function _burnAllocated(uint256 amount, uint256[] memory tokenIds) internal {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");
        // Burn activity amortizes the PID; the K value isn't consumed here.
        buckK.compute();
        require(amount <= balanceOf(msg.sender), "BUCK: amount exceeds spendable");

        (, uint256 poolRefund) = _allocateBurn(amount, tokenIds);

        _accrueJubilee();
        if (poolRefund > 0) {
            _crystallize(insurancePool);
            int256 poolRaw = _state[insurancePool].balance.asInt();
            require(int256(poolRefund) <= poolRaw, "BUCK: pool underfunded");
            _subBalance(insurancePool, poolRefund);
            _crystallize(msg.sender);
            _addBalance(msg.sender, poolRefund);
            // Per-side Transfer event: pool -> holder for the refund.
            emit Transfer(insurancePool, address(0), poolRefund);
        }
        // No additional totalSupply mutation needed -- _setBalanceSigned
        // tracked both writes.
    }

    // ---- mint/burn allocator (per-NFT cheapest-first inversion) ------------

    /// @dev Walk `tokenIds` cheapest-first and allocate enough coverage to
    ///      deliver `amount` net to msg.sender.  Per-NFT inversion:
    ///         take = ceil(remaining * BP / (BP - rate * POOL_ROI_INV)).
    ///      Writes mintsBacked.  Returns (totalCoverage, poolPrincipal).
    function _allocateMint(uint256 amount, uint256[] memory tokenIds)
        internal returns (uint256 totalCoverage, uint256 poolPrincipal)
    {
        uint256 remaining = amount;
        // One external call returns (owner, faceValue, activatedValue,
        // premiumRate) for every NFT -- replaces the prior 2N cross-contract
        // dispatches (ownerOf + creditInfo per iter).  ~1.1k gas saved/NFT.
        CreditSlice[] memory slices = buckCredit.batchCreditInfo(tokenIds);
        for (uint256 i = 0; i < tokenIds.length && remaining > 0; i++) {
            uint256 tid = tokenIds[i];
            CreditSlice memory s = slices[i];
            require(s.owner == msg.sender, "BUCK: not credit owner");
            // Cap is now faceValue (auto-activation can walk into unactivated
            // capacity); mintsBacked is the running tally of activated take.
            uint256 effRate = uint256(s.premiumRate) * POOL_ROI_INV;
            require(effRate < BP, "BUCK: NFT rate too high");

            uint256 used = mintsBacked[tid];
            if (s.faceValue <= used) continue;
            uint256 avail  = s.faceValue - used;
            uint256 denom  = BP - effRate;
            uint256 netCap = avail * denom / BP;

            uint256 take;
            uint256 principal_i;
            if (netCap >= remaining) {
                take = (remaining * BP + denom - 1) / denom;
                if (take > avail) take = avail;
                principal_i = take - remaining;
                remaining = 0;
            } else {
                take = avail;
                principal_i = take - netCap;
                remaining -= netCap;
            }
            mintsBacked[tid] = used + take;
            // Top-up activation: only expand activatedValue if mintsBacked
            // now exceeds it.  A holder may pre-`activate()` to grow their
            // credit limit independently of any mint, in which case the
            // existing activatedValue already covers `mintsBacked + take`
            // and this call is a no-op (delta = 0).  BuckCredit fires
            // onCreditMutation on real activation, invalidating Buck's
            // per-block credit-limit cache for msg.sender.  The slice's
            // activatedValue is the pre-call snapshot; safe to use here
            // because activatedValue only mutates via activate/activateFromBuck
            // and we never see the same tid twice in one call.
            uint256 needed = used + take;
            if (s.activatedValue < needed) {
                buckCredit.activateFromBuck(tid, msg.sender, needed - s.activatedValue);
            }
            totalCoverage += take;
            poolPrincipal += principal_i;
        }
        require(remaining == 0, "BUCK: insufficient credit allocation");
    }

    function _allocateMintView(uint256 amount, uint256[] memory tokenIds)
        internal view returns (uint256 totalCoverage, uint256 poolPrincipal)
    {
        uint256 remaining = amount;
        CreditSlice[] memory slices = buckCredit.batchCreditInfo(tokenIds);
        for (uint256 i = 0; i < tokenIds.length && remaining > 0; i++) {
            uint256 tid = tokenIds[i];
            CreditSlice memory s = slices[i];
            uint256 effRate = uint256(s.premiumRate) * POOL_ROI_INV;
            require(effRate < BP, "BUCK: NFT rate too high");
            uint256 used = mintsBacked[tid];
            if (s.faceValue <= used) continue;
            uint256 avail  = s.faceValue - used;
            uint256 denom  = BP - effRate;
            uint256 netCap = avail * denom / BP;
            uint256 take;
            uint256 principal_i;
            if (netCap >= remaining) {
                take = (remaining * BP + denom - 1) / denom;
                if (take > avail) take = avail;
                principal_i = take - remaining;
                remaining = 0;
            } else {
                take = avail;
                principal_i = take - netCap;
                remaining -= netCap;
            }
            totalCoverage += take;
            poolPrincipal += principal_i;
        }
        require(remaining == 0, "BUCK: insufficient credit allocation");
    }

    function _allocateBurn(uint256 amount, uint256[] memory tokenIds)
        internal returns (uint256 totalUnwind, uint256 poolRefund)
    {
        uint256 remaining = amount;
        CreditSlice[] memory slices = buckCredit.batchCreditInfo(tokenIds);
        for (uint256 i = 0; i < tokenIds.length && remaining > 0; i++) {
            uint256 tid = tokenIds[i];
            CreditSlice memory s = slices[i];
            require(s.owner == msg.sender, "BUCK: not credit owner");
            uint256 effRate = uint256(s.premiumRate) * POOL_ROI_INV;
            uint256 used    = mintsBacked[tid];
            // Silently skip fully-unused or over-rate NFTs rather than reverting: a reappraisal
            // that pushes premiumRate above the pool-ROI threshold must not strand a burn.
            if (used == 0 || effRate >= BP) continue;
            uint256 denom   = BP - effRate;
            uint256 netCap  = used * denom / BP;

            uint256 unwind;
            uint256 refund_i;
            if (netCap >= remaining) {
                unwind = (remaining * BP + denom - 1) / denom;
                if (unwind > used) unwind = used;
                refund_i = unwind - remaining;
                remaining = 0;
            } else {
                unwind   = used;
                refund_i = unwind - netCap;
                remaining -= netCap;
            }
            mintsBacked[tid] = used - unwind;
            // burn() does NOT shrink activatedValue.  The holder's
            // expressed credit limit is sticky -- it only grows on mint
            // top-up or an explicit user-side activate().  Freeing
            // mintsBacked is enough to make the same coverage capacity
            // re-usable for a future mint; the cache need not invalidate
            // because creditLimit is a function of activatedValue, which
            // didn't change.
            totalUnwind += unwind;
            poolRefund  += refund_i;
        }
        require(remaining == 0, "BUCK: insufficient coverage to unwind");
    }

    function _allocateBurnView(uint256 amount, uint256[] memory tokenIds)
        internal view returns (uint256 totalUnwind, uint256 poolRefund)
    {
        uint256 remaining = amount;
        CreditSlice[] memory slices = buckCredit.batchCreditInfo(tokenIds);
        for (uint256 i = 0; i < tokenIds.length && remaining > 0; i++) {
            uint256 tid = tokenIds[i];
            uint256 effRate = uint256(slices[i].premiumRate) * POOL_ROI_INV;
            uint256 used = mintsBacked[tid];
            if (used == 0 || effRate >= BP) continue; // mirrors _allocateBurn skip, not a revert
            uint256 denom  = BP - effRate;
            uint256 netCap = used * denom / BP;
            uint256 unwind;
            uint256 refund_i;
            if (netCap >= remaining) {
                unwind = (remaining * BP + denom - 1) / denom;
                if (unwind > used) unwind = used;
                refund_i = unwind - remaining;
                remaining = 0;
            } else {
                unwind   = used;
                refund_i = unwind - netCap;
                remaining -= netCap;
            }
            totalUnwind += unwind;
            poolRefund  += refund_i;
        }
        require(remaining == 0, "BUCK: insufficient coverage to unwind");
    }

    /// @dev Build the caller's NFT list sorted ascending by premiumRate.
    function _selectCheapest(address holder) internal view returns (uint256[] memory) {
        uint256 n = buckCredit.balanceOf(holder);
        uint256[] memory tids  = new uint256[](n);
        uint32[]  memory rates = new uint32[](n);
        for (uint256 i = 0; i < n; i++) {
            uint256 tid = buckCredit.tokenOfOwnerByIndex(holder, i);
            (, , uint32 r) = buckCredit.creditInfo(tid);
            tids[i]  = tid;
            rates[i] = r;
        }
        for (uint256 i = 1; i < n; i++) {
            uint256 j = i;
            while (j > 0 && rates[j - 1] > rates[j]) {
                (rates[j - 1], rates[j]) = (rates[j], rates[j - 1]);
                (tids[j - 1],  tids[j])  = (tids[j],  tids[j - 1]);
                j--;
            }
        }
        return tids;
    }

    /// @dev Build the caller's NFT list sorted descending by premiumRate.
    ///      Reverses _selectCheapest in place; one extra pass is negligible
    ///      next to the n storage reads we already did.
    function _selectMostExpensive(address holder) internal view returns (uint256[] memory) {
        uint256[] memory tids = _selectCheapest(holder);
        uint256 n = tids.length;
        for (uint256 i = 0; i < n / 2; i++) {
            (tids[i], tids[n - 1 - i]) = (tids[n - 1 - i], tids[i]);
        }
        return tids;
    }

    // ---- identity-checked transfers ----------------------------------------

    /// @dev Every transfer emits a BuckTransferReceipt carrying identity material
    ///      for both sides.  Mutual decryptability requires that every private
    ///      (non-public) party hold a per-pair CP receipt fragment for its
    ///      counterparty, laid down by the 4-arg identity-bound approve() before
    ///      the transfer.  The identityHash fallback is only valid for a party
    ///      bound under a Public Identity (whose plaintext identity is already
    ///      attested off-chain in the registry).  Only public→public transfers
    ///      may proceed without any CP fragments.
    ///
    ///      The two guards cover the four quadrants:
    ///
    ///        | from \ to  | private                   | public                    |
    ///        | private    | both must CP-approve       | from must CP-approve to   |
    ///        | public     | to must CP-approve from    | neither needs CP          |
    ///
    ///      Rationale: when a regulator subpoenas the operator of a public
    ///      contract (Uniswap pool, router), the operator must be able to
    ///      decrypt every counterparty's identity from the on-chain receipt
    ///      alone.  The operator holds the secret key for the contract's bound
    ///      (pk, E).  A per-pair CP fragment from the private counterparty
    ///      (re-encrypted under that pk) gives the operator exactly that
    ///      capability.  The identityHash fallback (a keccak256 of the
    ///      counterparty's registered credential) is not decryptable — it
    ///      identifies the credential but does not reveal the plaintext
    ///      identity.  Hence the fallback is only valid when the party it
    ///      represents is already public.
    function _identityCheckedTransfer(address from, address to, uint256 amount) internal {
        require(identity.isVerified(from), "BUCK: sender not verified");
        require(identity.isVerified(to),   "BUCK: recipient not verified");

        bytes32 toHash = _receiptFragments[from][to];
        if (toHash == bytes32(0)) {
            require(
                identity.isPublicIdentity(from),
                "BUCK: sender must identity-approve recipient"
            );
            toHash = _identityHash(to);
        }
        bytes32 fromHash = _receiptFragments[to][from];
        if (fromHash == bytes32(0)) {
            require(
                identity.isPublicIdentity(to),
                "BUCK: recipient must identity-approve sender"
            );
            fromHash = _identityHash(from);
        }

        if (identity.isCarrying(from)) {
            _carryingTransfer(from, to, amount);
        } else {
            _nonCarryingTransfer(from, to, amount);
        }
        emit Transfer(from, to, amount);
        emit BuckTransferReceipt(from, to, amount, fromHash, toHash);
    }

    /// @dev Non-Carrying sender: spendable = held + unused credit
    ///      (see balanceOf).  Transferring `value` either consumes held BUCK
    ///      (raw stays positive) or extends into NFT-backed credit (raw
    ///      goes negative).  _subBalance is signed-aware and _setBalanceSigned
    ///      tracks the `_totalSupply` delta -- fresh BUCK enters circulation
    ///      precisely when the sender's raw crosses from positive (or zero)
    ///      toward more-negative.
    function _nonCarryingTransfer(address from, address to, uint256 value) internal {
        _crystallize(from);
        require(value <= balanceOf(from), "BUCK: amount exceeds spendable");
        _crystallize(to);
        _subBalance(from, value);
        _addBalance(to, value);
    }

    /// @dev Carrying transfer: proportionally apportions the sender's live
    ///      buckSeconds (crystallised + current rectangle) to the recipient.
    ///      Both sides settle in one SSTORE each.  Carrying accounts hold no
    ///      NFT-backed credit (creditLimit == 0) and cannot go negative; the
    ///      `value <= raw` assertion enforces this.
    function _carryingTransfer(address from, address to, uint256 value) internal {
        // ---- from ----
        AccountState memory fs = _state[from];
        int256 rawSigned = fs.balance.asInt();
        require(rawSigned >= int256(value), "BUCK: Carrying amount exceeds raw");
        uint256 raw = uint256(rawSigned);

        uint256 elapsed = block.timestamp - uint256(fs.timestamp);
        uint256 liveBs  = fs.buckSeconds.asUint() + raw * elapsed;
        uint256 carried = raw > 0 ? liveBs * value / raw : 0;

        // Compute new positive contributions for totalSupply tracking
        // (Carrying accounts only ever hold raw >= 0, so the deltas are
        // simple unsigned subtractions/additions in this branch).
        uint256 newFromRaw = raw - value;
        fs.balance     = toBuckQtySigned(int256(newFromRaw));
        fs.buckSeconds = toBuckSeconds(liveBs - carried);
        fs.timestamp   = uint40(block.timestamp);
        _state[from] = fs;
        _totalSupply -= value;     // from's positive contribution dropped by value

        // ---- to ----
        AccountState memory ts = _state[to];
        int256 toRawSigned = ts.balance.asInt();
        // The recipient may be in debt (raw < 0); receiving BUCK first pays
        // down their debt before turning positive.  We use _setBalanceSigned-
        // style accounting for the totalSupply delta but inline the writes
        // here to preserve the carrying-fold-buckSeconds logic.
        uint256 oldToPos = toRawSigned > 0 ? uint256(toRawSigned) : 0;
        int256  newToSigned = toRawSigned + int256(value);
        uint256 newToPos = newToSigned > 0 ? uint256(newToSigned) : 0;

        // buckSeconds carry-over uses the *positive* portion of the recipient's
        // history; if the recipient was in debt their buckSeconds is zero and
        // elapsed-rectangle is meaningless.
        uint256 toRawPos  = oldToPos;
        uint256 toElapsed = block.timestamp - uint256(ts.timestamp);
        uint256 toBs      = ts.buckSeconds.asUint() + toRawPos * toElapsed + carried;

        ts.balance     = toBuckQtySigned(newToSigned);
        ts.buckSeconds = toBuckSeconds(toBs);
        ts.timestamp   = uint40(block.timestamp);
        _state[to] = ts;
        if (newToPos > oldToPos) {
            _totalSupply += (newToPos - oldToPos);
        } else if (oldToPos > newToPos) {
            _totalSupply -= (oldToPos - newToPos);
        }
    }

    // ---- demurrage views ---------------------------------------------------

    function feeOwing(address a) public view returns (uint256) {
        AccountState storage s = _state[a];
        int256 raw = s.balance.asInt();
        if (raw <= 0) return 0;             // no demurrage on debt or empty
        return _feeOwing(s, uint256(raw));
    }

    function balanceOfFees(address a) public view returns (uint256) {
        uint256 fee = feeOwing(a);
        if (identity.isCarrying(a)) return fee;
        int256 raw = _state[a].balance.asInt();
        if (raw <= 0) return 0;
        uint256 rawU = uint256(raw);
        return fee >= rawU ? rawU : fee;
    }

    /// @notice Unsigned raw balance.  Clamps negative (debt) accounts to 0
    ///         so legacy ERC-20-style readers see a non-negative number;
    ///         use `signedRawBalanceOf` if you need to distinguish debt.
    function rawBalanceOf(address a) external view returns (uint256) {
        int256 raw = _state[a].balance.asInt();
        return raw <= 0 ? 0 : uint256(raw);
    }

    /// @notice Signed raw balance (negative = NFT-backed debt).
    function signedRawBalanceOf(address a) external view returns (int256) {
        return _state[a].balance.asInt();
    }

    function jubileeBalance() external view returns (uint256) {
        return balanceOf(address(this));
    }

    function jubileeActual() external view returns (uint256) {
        int256 raw = _state[address(this)].balance.asInt();
        return raw <= 0 ? 0 : uint256(raw);
    }

    // ---- demurrage internals -----------------------------------------------

    /// @dev Dimensional analysis:
    ///        buckSecondsLive     [raw * s]
    ///        BASE_RATE_PER_SEC   [2e25 / (365d+6h)]  = 0.02 / year_in_seconds
    ///        SCALE = 1e27        [dimensionless]
    ///        fee = buckSecondsLive * BASE_RATE_PER_SEC / SCALE
    ///            = balance * elapsed * 0.02 / year_length   [raw units]
    ///
    ///        Example: 1 BUCK (1e6 raw) held 1 year → 1e6 * 0.02 = 20,000 raw.
    function _feeOwing(AccountState storage s, uint256 raw) internal view returns (uint256) {
        uint256 elapsed = block.timestamp - uint256(s.timestamp);
        uint256 buckSecondsLive = s.buckSeconds.asUint() + (raw * elapsed);
        if (buckSecondsLive == 0) return 0;
        return Math.mulDiv(buckSecondsLive, BASE_RATE_PER_SEC, SCALE);
    }

    /// @dev Fold the elapsed (balance * dt) rectangle into buckSeconds and
    ///      bump the timestamp.  Idempotent in time: a second call within
    ///      the same block is a no-op.  No balance change.  Debt-bearing
    ///      accounts (signed raw < 0) accrue no demurrage -- the rectangle
    ///      uses only the positive portion of the balance.
    function _crystallize(address a) internal {
        AccountState memory s = _state[a];
        int256 rawSigned = s.balance.asInt();
        uint256 raw      = rawSigned > 0 ? uint256(rawSigned) : 0;
        uint256 elapsed  = block.timestamp - uint256(s.timestamp);
        bool dirty = false;
        if (elapsed != 0 && raw != 0) {
            uint256 newBs = s.buckSeconds.asUint() + raw * elapsed;
            s.buckSeconds = toBuckSeconds(newBs);
            dirty = true;
        }
        if (uint256(s.timestamp) != block.timestamp) {
            s.timestamp = uint40(block.timestamp);
            dirty = true;
        }
        if (dirty) _state[a] = s;
    }

    /// @dev System-level Jubilee accrual.  Adds totalSupply*RATE*elapsed to
    ///      Jubilee's balance directly -- NOT a mint, totalSupply unchanged.
    ///      Accrues for all BUCK (Carrying and non-Carrying alike); when
    ///      Carrying BUCKs later move to non-Carrying via _carryingTransfer,
    ///      liveBs*value/raw of the sender's full live integral propagates
    ///      proportionally to the recipient -- Jubilee pre-accrued it.
    function _accrueJubilee() internal {
        uint256 elapsed = block.timestamp - uint256(_jubileeLastUpdate);
        if (elapsed == 0) return;
        uint256 supply = _totalSupply;
        _jubileeLastUpdate = uint64(block.timestamp);
        if (supply == 0) return;
        uint256 delta = Math.mulDiv(supply, BASE_RATE_PER_SEC * elapsed, SCALE);
        if (delta == 0) return;
        _crystallize(address(this));
        // Direct balance write: Jubilee accrual is redistribution from
        // every Carrying/non-Carrying account's already-accounted raw, NOT
        // a fresh mint.  Going through _addBalance / _setBalanceSigned
        // would inflate _totalSupply by `delta`, breaking the invariant
        //   sum_a max(0, signedRaw(a)) == totalSupply + jubileeActual.
        int256 oldJubSigned = _state[address(this)].balance.asInt();
        int256 newJubSigned = oldJubSigned + int256(delta);
        AccountState memory js = _state[address(this)];
        js.balance = toBuckQtySigned(newJubSigned);
        _state[address(this)] = js;
        emit JubileeAccrued(delta, newJubSigned > 0 ? uint256(newJubSigned) : 0);
    }

    // ---- balance writes ----------------------------------------------------
    //
    // Under the negative-balance model, _addBalance / _subBalance operate on
    // the *signed* underlying.  _subBalance does NOT revert on underflow --
    // callers must check creditLimit upstream.  Every state change updates
    // `_totalSupply` so that the invariant
    //
    //     _totalSupply == sum_a max(0, signedRawBalance(a))
    //
    // holds.  Fresh BUCK enters circulation precisely when an account moves
    // toward more-negative (somebody else is receiving real BUCK against
    // the debt-payer's credit); BUCK leaves circulation when a negative-raw
    // account climbs toward zero (the debt is being paid down).

    function _addBalance(address a, uint256 amount) internal {
        if (amount == 0) return;
        int256 oldSigned = _state[a].balance.asInt();
        _setBalanceSigned(a, oldSigned + int256(amount));
    }

    function _subBalance(address a, uint256 amount) internal {
        if (amount == 0) return;
        int256 oldSigned = _state[a].balance.asInt();
        _setBalanceSigned(a, oldSigned - int256(amount));
    }

    /// @dev Write a new signed balance into the account slot, maintaining
    ///      the `_totalSupply == sum_a max(0, signedRaw(a))` invariant.
    function _setBalanceSigned(address a, int256 newSigned) internal {
        int256 oldSigned = _state[a].balance.asInt();
        AccountState memory s = _state[a];
        s.balance = toBuckQtySigned(newSigned);
        _state[a] = s;
        int256 oldPos = oldSigned > 0 ? oldSigned : int256(0);
        int256 newPos = newSigned > 0 ? newSigned : int256(0);
        if (newPos > oldPos) {
            _totalSupply += uint256(newPos - oldPos);
        } else if (oldPos > newPos) {
            _totalSupply -= uint256(oldPos - newPos);
        }
    }

    // ---- ERC-20 internals --------------------------------------------------

    function _approve(address owner, address spender, uint256 amount) internal {
        _allowances[owner][spender] = amount;
        emit Approval(owner, spender, amount);
    }

    function _spendAllowance(address owner, address spender, uint256 amount) internal {
        uint256 cur = _allowances[owner][spender];
        if (cur != type(uint256).max) {
            require(cur >= amount, "BUCK: insufficient allowance");
            unchecked { _allowances[owner][spender] = cur - amount; }
        }
    }

    // ---- helpers -----------------------------------------------------------

    function _ciphertextHash(IdentityRegistry.ElGamalCT calldata E)
        internal pure returns (bytes32)
    {
        return keccak256(abi.encode(E.R.X, E.R.Y, E.C.X, E.C.Y));
    }

    function _identityHash(address account) internal view returns (bytes32) {
        BN254.G1Point memory pk             = identity.pkOf(account);
        IdentityRegistry.ElGamalCT memory E = identity.ciphertextOf(account);
        return keccak256(abi.encode(pk.X, pk.Y, E.R.X, E.R.Y, E.C.X, E.C.Y));
    }
}
