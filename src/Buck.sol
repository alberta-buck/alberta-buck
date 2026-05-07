// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}         from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {Math}           from "@openzeppelin/contracts/utils/math/Math.sol";

import {BN254}            from "./BN254.sol";
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

    uint256 internal constant PRECISION          = 1e18;
    uint256 internal constant SCALE              = 1e27;
    uint256 internal constant BASE_RATE_PER_YEAR = 2e25;                // 0.02 in SCALE
    uint256 internal constant SECONDS_PER_YEAR_  = 365 days + 6 hours;
    uint256 internal constant BASE_RATE_PER_SEC  = BASE_RATE_PER_YEAR / SECONDS_PER_YEAR_;

    uint256 internal constant BP                 = 10000;
    uint256 internal constant POOL_ROI_INV       = 10;                  // 10% assumed annual ROI

    uint256 internal constant MAX_BALANCE        = type(uint80).max;    // ~1.21e24
    uint256 internal constant MAX_BUCKSECONDS    = type(uint120).max;

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
        uint80  balance;
        uint120 buckSeconds;
        uint40  timestamp;
        uint16  flags;
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
    ///      Invariant: sum(stored balances) == totalSupply + cumulative
    ///      Jubilee accrual.  The "extra" stored on Jubilee's side precisely
    ///      offsets the locked fees hidden inside non-Carrying balanceOf
    ///      results, so total spendable supply across all accounts ==
    ///      totalSupply at every block.
    uint64 internal _jubileeLastUpdate;

    // ---- premium / mutual-insurance pool model -----------------------------
    //
    // mint(N) delivers N to the holder + a mutual-insurance pool deposit of
    // (annual_premium * POOL_ROI_INV) to insurancePool, both drawn against
    // BuckCredit NFT capacity cheapest-first.  Per-NFT inversion:
    //     take = ceil(remaining * BP / (BP - rate * POOL_ROI_INV))
    // burn(N) is the exact inverse.  See _allocateMint / _allocateBurn.

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
    function symbol()   external pure returns (string memory) { return "BUCK";          }
    function decimals() external pure returns (uint8)         { return 6;               }

    // ---- IERC20 ------------------------------------------------------------

    function totalSupply() external view returns (uint256) { return _totalSupply; }

    function balanceOf(address a) public view returns (uint256) {
        AccountState storage s = _state[a];
        uint256 raw = uint256(s.balance);
        if (identity.isCarrying(a)) return raw;
        uint256 fee = _feeOwing(s, raw);
        return fee >= raw ? 0 : raw - fee;
    }

    function allowance(address owner, address spender) external view returns (uint256) {
        return _allowances[owner][spender];
    }

    /// @notice Block parameterless approve.  Identity-bound overload mandatory.
    function approve(address, uint256) external pure returns (bool) {
        revert("BUCK: use identity-bound approve");
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

    function burn(uint256 amount) external {
        _burnAllocated(amount, _selectCheapest(msg.sender));
    }

    function burn(uint256 amount, uint256[] calldata tokenIds) external {
        _burnAllocated(amount, tokenIds);
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
        uint256 currentBuckK     = buckK.currentBuckK();
        uint256 maxLimit         = totalCreditValue * currentBuckK / PRECISION;
        if (maxLimit > storedLimit[msg.sender]) {
            storedLimit[msg.sender] = maxLimit;
        }
        uint256 limit = storedLimit[msg.sender];
        require(uint256(_state[msg.sender].balance) + amount <= limit, "BUCK: exceeds credit limit");

        (uint256 totalCoverage, uint256 poolPrincipal) = _allocateMint(amount, tokenIds);

        require(
            uint256(_state[msg.sender].balance) + totalCoverage <= limit,
            "BUCK: exceeds credit limit"
        );

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
            if (used == 0 || effRate >= BP) continue;
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

    // ---- identity-checked transfers ----------------------------------------

    function _identityCheckedTransfer(address from, address to, uint256 amount) internal {
        require(identity.isVerified(from), "BUCK: sender not verified");
        require(identity.isVerified(to),   "BUCK: recipient not verified");

        bytes32 toHash = _receiptFragments[from][to];
        if (toHash == bytes32(0)) {
            require(
                identity.isPublicIdentity(from) || identity.isPublicIdentity(to),
                "BUCK: missing identity receipt"
            );
            toHash = _identityHash(to);
        }
        bytes32 fromHash = _receiptFragments[to][from];
        if (fromHash == bytes32(0)) fromHash = _identityHash(from);

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

    /// @dev Carrying sender's basis is untouched (timestamp unchanged);
    ///      recipient absorbs `value * (now - sender.timestamp)` into its
    ///      buckSeconds in a single SSTORE alongside its own crystallisation.
    function _carryingTransfer(address from, address to, uint256 value) internal {
        uint256 ageBasis = block.timestamp - uint256(_state[from].timestamp);
        uint256 carriedBuckSeconds = (value == 0 || ageBasis == 0)
            ? 0
            : value * ageBasis;
        require(value <= uint256(_state[from].balance), "BUCK: amount exceeds raw");
        _crystallizeAndAdd(to, carriedBuckSeconds);
        _subBalance(from, value);
        _addBalance(to, value);
    }

    // ---- demurrage views ---------------------------------------------------

    function feeOwing(address a) public view returns (uint256) {
        AccountState storage s = _state[a];
        return _feeOwing(s, uint256(s.balance));
    }

    function balanceOfFees(address a) public view returns (uint256) {
        uint256 fee = feeOwing(a);
        if (identity.isCarrying(a)) return fee;
        uint256 raw = uint256(_state[a].balance);
        return fee >= raw ? raw : fee;
    }

    function rawBalanceOf(address a) external view returns (uint256) {
        return uint256(_state[a].balance);
    }

    function jubileeBalance() external view returns (uint256) {
        return balanceOf(address(this));
    }

    function jubileeActual() external view returns (uint256) {
        return uint256(_state[address(this)].balance);
    }

    // ---- demurrage internals -----------------------------------------------

    function _feeOwing(AccountState storage s, uint256 raw) internal view returns (uint256) {
        uint256 elapsed = block.timestamp - uint256(s.timestamp);
        uint256 buckSecondsLive = uint256(s.buckSeconds) + (raw * elapsed);
        if (buckSecondsLive == 0) return 0;
        return Math.mulDiv(buckSecondsLive, BASE_RATE_PER_SEC, SCALE);
    }

    /// @dev Fold the elapsed (balance * dt) rectangle into buckSeconds and
    ///      bump the timestamp.  Idempotent in time: a second call within
    ///      the same block is a no-op.  No balance change.
    function _crystallize(address a) internal {
        AccountState memory s = _state[a];
        uint256 raw     = uint256(s.balance);
        uint256 elapsed = block.timestamp - uint256(s.timestamp);
        bool dirty = false;
        if (elapsed != 0 && raw != 0) {
            uint256 newBs = uint256(s.buckSeconds) + raw * elapsed;
            require(newBs <= MAX_BUCKSECONDS, "BUCK: buckSeconds overflow");
            s.buckSeconds = uint120(newBs);
            dirty = true;
        }
        if (uint256(s.timestamp) != block.timestamp) {
            s.timestamp = uint40(block.timestamp);
            dirty = true;
        }
        if (dirty) _state[a] = s;
    }

    /// @dev Crystallise `a` and add `extraBuckSeconds` to its IOU integral
    ///      in a single SSTORE.  Used by Carrying transfer to fold the
    ///      carried `value * age_basis` alongside the recipient's own
    ///      rectangle.
    function _crystallizeAndAdd(address a, uint256 extraBuckSeconds) internal {
        AccountState memory s = _state[a];
        uint256 raw     = uint256(s.balance);
        uint256 elapsed = block.timestamp - uint256(s.timestamp);
        uint256 newBs   = uint256(s.buckSeconds);
        if (elapsed != 0 && raw != 0) {
            newBs += raw * elapsed;
        }
        newBs += extraBuckSeconds;
        require(newBs <= MAX_BUCKSECONDS, "BUCK: buckSeconds overflow");
        s.buckSeconds = uint120(newBs);
        s.timestamp   = uint40(block.timestamp);
        _state[a] = s;
    }

    /// @dev System-level Jubilee accrual.  Adds totalSupply*RATE*elapsed to
    ///      Jubilee's balance directly -- NOT a mint, totalSupply unchanged.
    ///      The "extra" balance held by Jubilee precisely matches the sum of
    ///      locked fees hidden in Non-Carrying balanceOf results.
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
        emit JubileeAccrued(delta, uint256(_state[address(this)].balance));
    }

    // ---- balance writes ----------------------------------------------------

    function _addBalance(address a, uint256 amount) internal {
        if (amount == 0) return;
        AccountState memory s = _state[a];
        uint256 newBal = uint256(s.balance) + amount;
        require(newBal <= MAX_BALANCE, "BUCK: balance overflow");
        s.balance = uint80(newBal);
        _state[a] = s;
    }

    function _subBalance(address a, uint256 amount) internal {
        if (amount == 0) return;
        AccountState memory s = _state[a];
        require(uint256(s.balance) >= amount, "BUCK: insufficient balance");
        unchecked { s.balance = uint80(uint256(s.balance) - amount); }
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
