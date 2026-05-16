// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}         from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {Math}           from "@openzeppelin/contracts/utils/math/Math.sol";

import {BN254}            from "./BN254.sol";
import {BuckTypes, BuckQty, BuckSeconds, toBuckQty, toBuckSeconds} from "./BuckTypes.sol";
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

    function balanceOf(address a) public view returns (uint256) {
        AccountState storage s = _state[a];
        uint256 raw = s.balance.asUint();
        if (identity.isCarrying(a)) return raw;
        uint256 fee = _feeOwing(s, raw);
        return fee >= raw ? 0 : raw - fee;
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
        _addBalance(to, amount);
        _totalSupply += amount;
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
        require(_state[msg.sender].balance.asUint() >= amount, "BUCK: insufficient");
        _subBalance(msg.sender, amount);
        _totalSupply -= amount;
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

    function _mintAllocated(uint256 amount, uint256[] memory tokenIds) internal {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");

        uint256 totalCreditValue = buckCredit.totalCurrentValue(msg.sender);
        // compute() advances the PID if dT has elapsed (cheap cached read
        // otherwise).  Mint activity is the primary driver of the controller.
        uint256 currentBuckK     = buckK.compute();
        uint256 maxLimit         = totalCreditValue * currentBuckK / BUCKK_SCALE;
        if (maxLimit > storedLimit[msg.sender]) {
            storedLimit[msg.sender] = maxLimit;
        }
        uint256 limit = storedLimit[msg.sender];
        require(_state[msg.sender].balance.asUint() + amount <= limit, "BUCK: exceeds credit limit");

        (uint256 totalCoverage, uint256 poolPrincipal) = _allocateMint(amount, tokenIds);

        require(
            _state[msg.sender].balance.asUint() + totalCoverage <= limit,
            "BUCK: exceeds credit limit"
        );

        // Counter-cyclical insurance funding gate.  The minter must already
        // hold poolPrincipal * fundingFactor / 1e18 BUCK as a precondition
        // (the balance is NOT consumed -- it is skin-in-the-game collateral
        // that throttles new mints when BUCK trades below basket).
        //
        // Bootstrap exemption: when totalSupply == 0 no BUCK exists yet, so
        // the very first mint by definition cannot satisfy any non-zero
        // requirement.  Skipping the gate here lets the genesis minter seed
        // the system; every subsequent mint must satisfy the live factor.
        //
        // Mints with zero poolPrincipal (NFT premium so low it rounds to 0)
        // also bypass: there is no insurance contribution to back.
        if (_totalSupply > 0 && poolPrincipal > 0) {
            uint256 factor   = buckK.fundingFactor();
            if (factor > 0) {
                uint256 required = poolPrincipal * factor / BUCKK_SCALE;
                require(
                    balanceOf(msg.sender) >= required,
                    "BUCK: insufficient mint funding"
                );
            }
        }

        _accrueJubilee();
        _crystallize(msg.sender);
        _addBalance(msg.sender, amount);
        if (poolPrincipal > 0) {
            _crystallize(insurancePool);
            _addBalance(insurancePool, poolPrincipal);
        }
        _totalSupply += amount + poolPrincipal;

        emit Transfer(address(0), msg.sender, amount);
        if (poolPrincipal > 0) emit Transfer(address(0), insurancePool, poolPrincipal);
        emit Minted(msg.sender, totalCoverage, poolPrincipal, totalCreditValue, currentBuckK, limit);
    }

    function _burnAllocated(uint256 amount, uint256[] memory tokenIds) internal {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");
        // Burn doesn't consume the K value but still touches the controller
        // so burn activity also amortizes PID work alongside mints.
        buckK.compute();
        (, uint256 poolRefund) = _allocateBurn(amount, tokenIds);

        _accrueJubilee();
        _crystallize(msg.sender);
        require(amount <= balanceOf(msg.sender), "BUCK: amount exceeds spendable");
        _subBalance(msg.sender, amount);
        if (poolRefund > 0) {
            _crystallize(insurancePool);
            require(poolRefund <= balanceOf(insurancePool), "BUCK: pool underfunded");
            _subBalance(insurancePool, poolRefund);
        }
        _totalSupply -= amount + poolRefund;

        emit Transfer(msg.sender, address(0), amount);
        if (poolRefund > 0) emit Transfer(insurancePool, address(0), poolRefund);
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
        for (uint256 i = 0; i < tokenIds.length && remaining > 0; i++) {
            uint256 tid = tokenIds[i];
            require(buckCredit.ownerOf(tid) == msg.sender, "BUCK: not credit owner");
            (, uint256 activated, uint32 rate) = buckCredit.creditInfo(tid);
            uint256 effRate = uint256(rate) * POOL_ROI_INV;
            require(effRate < BP, "BUCK: NFT rate too high");

            uint256 used = mintsBacked[tid];
            if (activated <= used) continue;
            uint256 avail  = activated - used;
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
            totalCoverage += take;
            poolPrincipal += principal_i;
        }
        require(remaining == 0, "BUCK: insufficient credit allocation");
    }

    function _allocateMintView(uint256 amount, uint256[] memory tokenIds)
        internal view returns (uint256 totalCoverage, uint256 poolPrincipal)
    {
        uint256 remaining = amount;
        for (uint256 i = 0; i < tokenIds.length && remaining > 0; i++) {
            uint256 tid = tokenIds[i];
            (, uint256 activated, uint32 rate) = buckCredit.creditInfo(tid);
            uint256 effRate = uint256(rate) * POOL_ROI_INV;
            require(effRate < BP, "BUCK: NFT rate too high");
            uint256 used = mintsBacked[tid];
            if (activated <= used) continue;
            uint256 avail  = activated - used;
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
        for (uint256 i = 0; i < tokenIds.length && remaining > 0; i++) {
            uint256 tid = tokenIds[i];
            require(buckCredit.ownerOf(tid) == msg.sender, "BUCK: not credit owner");
            (, , uint32 rate) = buckCredit.creditInfo(tid);
            uint256 effRate = uint256(rate) * POOL_ROI_INV;
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
            totalUnwind += unwind;
            poolRefund  += refund_i;
        }
        require(remaining == 0, "BUCK: insufficient coverage to unwind");
    }

    function _allocateBurnView(uint256 amount, uint256[] memory tokenIds)
        internal view returns (uint256 totalUnwind, uint256 poolRefund)
    {
        uint256 remaining = amount;
        for (uint256 i = 0; i < tokenIds.length && remaining > 0; i++) {
            uint256 tid = tokenIds[i];
            (, , uint32 rate) = buckCredit.creditInfo(tid);
            uint256 effRate = uint256(rate) * POOL_ROI_INV;
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
    ///      for both sides.  For private (non-public) counterparties the
    ///      per-pair receipt fragment MUST have been laid down by the 4-arg
    ///      identity-bound approve() before the transfer.  If a side is bound
    ///      under a Public Identity the fallback to the bound _identityHash
    ///      (its deterministic registered-credential hash) is always valid.
    ///
    ///      The two guards cover the four quadrants:
    ///
    ///        | from \ to  | private               | public               |
    ///        | private    | both must CP-approve   | from must CP-approve |
    ///        | public     | to must CP-approve     | neither needs CP     |
    function _identityCheckedTransfer(address from, address to, uint256 amount) internal {
        require(identity.isVerified(from), "BUCK: sender not verified");
        require(identity.isVerified(to),   "BUCK: recipient not verified");

        bytes32 toHash = _receiptFragments[from][to];
        if (toHash == bytes32(0)) {
            require(
                identity.isPublicIdentity(from) || identity.isPublicIdentity(to),
                "BUCK: sender must identity-approve recipient (both private)"
            );
            toHash = _identityHash(to);
        }
        bytes32 fromHash = _receiptFragments[to][from];
        if (fromHash == bytes32(0)) {
            require(
                identity.isPublicIdentity(from) || identity.isPublicIdentity(to),
                "BUCK: recipient must identity-approve sender (both private)"
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

    /// @dev Non-Carrying sender keeps locked dust in its own slot; recipient
    ///      crystallises and receives fresh BUCK with no inherited IOU.
    function _nonCarryingTransfer(address from, address to, uint256 value) internal {
        _crystallize(from);
        require(value <= balanceOf(from), "BUCK: amount exceeds spendable");
        _crystallize(to);
        _subBalance(from, value);
        _addBalance(to, value);
    }

    /// @dev Carrying transfer: proportionally apportions the sender's live
    ///      buckSeconds (crystallised + current rectangle) to the recipient.
    ///      Both sides settle in one SSTORE each.
    function _carryingTransfer(address from, address to, uint256 value) internal {
        // ---- from ----
        AccountState memory fs = _state[from];
        uint256 raw = fs.balance.asUint();
        // Carrying senders' balanceOf returns raw; checking raw is consistent
        // with the ERC-20 visible balance and avoids re-reading the carrying flag.
        require(value <= raw, "BUCK: amount exceeds raw");

        uint256 elapsed = block.timestamp - uint256(fs.timestamp);
        uint256 liveBs  = fs.buckSeconds.asUint() + raw * elapsed;
        uint256 carried = raw > 0 ? liveBs * value / raw : 0;

        fs.balance     = toBuckQty(raw - value);
        fs.buckSeconds = toBuckSeconds(liveBs - carried);
        fs.timestamp   = uint40(block.timestamp);
        _state[from] = fs;

        // ---- to ----
        AccountState memory ts = _state[to];
        uint256 toRaw     = ts.balance.asUint();
        uint256 toElapsed = block.timestamp - uint256(ts.timestamp);
        uint256 toBs      = ts.buckSeconds.asUint() + toRaw * toElapsed + carried;

        ts.balance     = toBuckQty(toRaw + value);
        ts.buckSeconds = toBuckSeconds(toBs);
        ts.timestamp   = uint40(block.timestamp);
        _state[to] = ts;
    }

    // ---- demurrage views ---------------------------------------------------

    function feeOwing(address a) public view returns (uint256) {
        AccountState storage s = _state[a];
        return _feeOwing(s, s.balance.asUint());
    }

    function balanceOfFees(address a) public view returns (uint256) {
        uint256 fee = feeOwing(a);
        if (identity.isCarrying(a)) return fee;
        uint256 raw = _state[a].balance.asUint();
        return fee >= raw ? raw : fee;
    }

    function rawBalanceOf(address a) external view returns (uint256) {
        return _state[a].balance.asUint();
    }

    function jubileeBalance() external view returns (uint256) {
        return balanceOf(address(this));
    }

    function jubileeActual() external view returns (uint256) {
        return _state[address(this)].balance.asUint();
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
    ///      the same block is a no-op.  No balance change.
    function _crystallize(address a) internal {
        AccountState memory s = _state[a];
        uint256 raw     = s.balance.asUint();
        uint256 elapsed = block.timestamp - uint256(s.timestamp);
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
        _addBalance(address(this), delta);
        emit JubileeAccrued(delta, _state[address(this)].balance.asUint());
    }

    // ---- balance writes ----------------------------------------------------

    function _addBalance(address a, uint256 amount) internal {
        if (amount == 0) return;
        AccountState memory s = _state[a];
        s.balance = toBuckQty(s.balance.asUint() + amount);
        _state[a] = s;
    }

    function _subBalance(address a, uint256 amount) internal {
        if (amount == 0) return;
        AccountState memory s = _state[a];
        uint256 raw = s.balance.asUint();
        require(raw >= amount, "BUCK: insufficient balance");
        unchecked { s.balance = toBuckQty(raw - amount); }
        _state[a] = s;
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
