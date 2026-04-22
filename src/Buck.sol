// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Math}  from "@openzeppelin/contracts/utils/math/Math.sol";

import {BN254} from "./BN254.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";

/// @title Buck — identity-bound ERC-20 minted against aggregated BUCK_CREDIT value.
/// @notice
///   * mint(amount) — caller must be identity-verified; mint is bounded by
///     BuckCredit aggregated value scaled by BUCK_K, with a quadratic
///     utilization premium routed to the insurance pool.
///   * approve(spender, amount, E_bob, pi) — bilateral identity binding.
///     The plain ERC-20 approve is blocked: the identity-bound overload is
///     mandatory so allowances always carry a Chaum-Pedersen receipt.
///   * transfer / transferFrom — every counterparty pair must be identity-bound
///     (via prior approve receipt) or the recipient must be a public account.
///
/// @dev   Receipt fragments are keccak256 commitments over E_recipient.  They
///        let Buck cheaply re-check identity binding on every transfer without
///        re-running the Chaum-Pedersen NIZK; auditors recompute the hash from
///        their off-chain ciphertext copies.
interface IBuckK {
    function currentBuckK() external view returns (uint256);
}

interface IBuckCredit {
    function totalCurrentValue(address holder) external view returns (uint256);
}

contract Buck is ERC20 {

    IBuckCredit       public immutable buckCredit;
    IBuckK            public immutable buckK;
    IdentityRegistry  public immutable identity;
    address           public immutable insurancePool;

    uint256 internal constant PRECISION = 1e18;

    // ---- demurrage / Jubilee fund -----------------------------------------
    //
    // Flat 2%/yr demurrage.  Per-account state = (raw_balance, idxAtLastTouch).
    //   feeOwing(a) = raw_balance(a) * (cumIndexNow - idxAtLastTouch[a]) / SCALE
    //
    // cumIndex grows linearly: BASE_RATE_PER_SEC * (now - genesis).
    //
    // Deducting transfer (transfer / transferFrom): sender's fee is BURNED
    // (totalSupply drops by fee), sender's index resets to cumIndex.  The
    // recipient is weighted-merge'd with cumIndex -- recipient's prior fee debt
    // is preserved and the fresh BUCKs land at age 0.  No incoming burn.
    //
    // Carrying transfer (transferCarrying): no burn.  Recipient is
    // weighted-merge'd with the SENDER's idxAtLastTouch -- the recipient
    // absorbs the carried age basis.  Sender's index is unchanged so the
    // remaining balance keeps its own age.  Total system fee debt preserved.
    //
    // Jubilee fund = address(this).  Treated as a Carrying account that grows
    // with fresh BUCKs at BASE_RATE on the FULL totalSupply (Jubilee included).
    // Each _update advance-mints `jubileeTarget() - jubileeRaw` to Jubilee and
    // weighted-merges its index toward cumIndex (preserves Jubilee's prior fee
    // debt; fresh BUCKs land at age 0).  Funds are deployed to pools via
    // standard approve + transferFrom (Deducting), which burns Jubilee's
    // accrued fee on outflow exactly like any other account.

    uint256 internal constant SCALE              = 1e27;
    uint256 internal constant BASE_RATE_PER_YEAR = 2e25;                              // 0.02 in SCALE
    uint256 internal constant SECONDS_PER_YEAR_  = 365 days + 6 hours;                // 365.25 days
    uint256 internal constant BASE_RATE_PER_SEC  = BASE_RATE_PER_YEAR / SECONDS_PER_YEAR_;

    uint256 internal _cumIndex;
    uint64  internal _lastIndexUpdate;
    uint256 internal _areaAcc;            // integral of totalSupply dt (BUCK*seconds)
    uint64  internal _areaLastUpdate;

    mapping(address => uint256) internal _indexAtLastTouch;

    // keccak256("buck.settling.v1") -- transient-storage re-entry guard slot.
    bytes32 private constant SETTLE_SLOT =
        0x808f796326b23af2b0e4e7824e695a7744c402b093670a86cbd6e7a21dbb4cff;

    event FeeBurned(address indexed from, uint256 amount, uint256 cumIndex);
    event JubileeAccrued(uint256 amount, uint256 newJubileeBalance, uint256 cumIndex);
    event TransferCarrying(address indexed from, address indexed to, uint256 amount);

    // ---- premium model -----------------------------------------------------

    /// @notice Base premium rate (basis points) at zero utilization.
    uint256 public constant BASE_RATE  = 50;     // 0.50%
    /// @notice Additional rate at 100% utilization, scaled quadratically.
    uint256 public constant SCALE_RATE = 450;    // +4.50%
    uint256 internal constant BP = 10000;

    // ---- per-account state -------------------------------------------------

    /// @notice Highest-ever credit limit observed for this account.  mint()
    ///         only ratchets it upward; BUCK_K-driven tightening shows up as a
    ///         premium spike rather than retroactive limit reduction.
    mapping(address => uint256) public storedLimit;

    /// @dev Receipt fragment per (from, to): keccak256 over the recipient's
    ///      identity ciphertext (E_to) sent at approve() time.  A non-zero
    ///      fragment proves Alice has performed the Chaum-Pedersen binding
    ///      to `to` at least once.  Cleared by setReceiptDirty().
    mapping(address => mapping(address => bytes32)) internal _receiptFragments;

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

    constructor(
        address _buckCredit,
        address _buckK,
        address _identity,
        address _insurancePool
    ) ERC20("Alberta Buck", "BUCK") {
        require(_buckCredit    != address(0), "buckCredit=0");
        require(_buckK         != address(0), "buckK=0");
        require(_identity      != address(0), "identity=0");
        require(_insurancePool != address(0), "insurancePool=0");
        buckCredit    = IBuckCredit(_buckCredit);
        buckK         = IBuckK(_buckK);
        identity      = IdentityRegistry(_identity);
        insurancePool = _insurancePool;

        _lastIndexUpdate = uint64(block.timestamp);
        _areaLastUpdate  = uint64(block.timestamp);
    }

    // ---- mint / burn -------------------------------------------------------

    /// @notice Mint BUCKs against the caller's aggregated BUCK_CREDIT value.
    function mint(uint256 amount) external {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");

        uint256 totalCreditValue = buckCredit.totalCurrentValue(msg.sender);
        uint256 currentBuckK     = buckK.currentBuckK();
        uint256 maxLimit         = totalCreditValue * currentBuckK / PRECISION;

        if (maxLimit > storedLimit[msg.sender]) {
            storedLimit[msg.sender] = maxLimit;
        }

        uint256 limit = storedLimit[msg.sender];
        require(ERC20.balanceOf(msg.sender) + amount <= limit, "BUCK: exceeds credit limit");

        uint256 premium = _computePremium(msg.sender, amount, limit);
        _mint(msg.sender, amount - premium);
        if (premium > 0) {
            _mint(insurancePool, premium);
        }

        emit Minted(msg.sender, amount, premium, totalCreditValue, currentBuckK, limit);
    }

    function burn(uint256 amount) external {
        _burn(msg.sender, amount);
    }

    // ---- identity-bound approve --------------------------------------------

    /// @notice Identity-bound approve.  Sets ERC-20 allowance and stores a
    ///         Chaum-Pedersen receipt that `E_bob` re-encrypts the caller's
    ///         identity point M under spender's identity public key.
    function approve(
        address spender,
        uint256 amount,
        IdentityRegistry.ElGamalCT calldata E_bob,
        IdentityRegistry.CPProof calldata pi_CP
    ) external returns (bool) {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");
        require(
            identity.isVerified(spender) || identity.isPublic(spender),
            "BUCK: spender not verified"
        );

        bytes32 receipt;
        if (identity.isPublic(spender)) {
            receipt = _publicReceiptHash(spender);
        } else {
            require(
                identity.verifyApprove(msg.sender, spender, E_bob, pi_CP),
                "BUCK: bad CP proof"
            );
            receipt = _ciphertextHash(E_bob);
        }
        _receiptFragments[msg.sender][spender] = receipt;
        emit ApproveReceipt(msg.sender, spender, receipt);

        _approve(msg.sender, spender, amount);
        return true;
    }

    /// @notice Block the parameterless ERC-20 approve.  Identity-bound
    ///         approve is mandatory so every allowance carries a CP receipt.
    function approve(address, uint256) public pure override returns (bool) {
        revert("BUCK: use identity-bound approve");
    }

    function receiptFragment(address from, address to) external view returns (bytes32) {
        return _receiptFragments[from][to];
    }

    // ---- identity-checked transfers ----------------------------------------

    function transfer(address to, uint256 amount) public override returns (bool) {
        _identityCheckedTransfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount)
        public override returns (bool)
    {
        _spendAllowance(from, msg.sender, amount);
        _identityCheckedTransfer(from, to, amount);
        return true;
    }

    function _identityCheckedTransfer(address from, address to, uint256 amount) internal {
        // System-public accounts (e.g. the Notes pool) are governance-
        // designated audit origins -- they have no PS credential, but they
        // are identifiable, so they qualify as a legitimate sender.  The
        // receipt-hash branch below already anticipates isPublic(from).
        require(
            identity.isVerified(from) || identity.isPublic(from),
            "BUCK: sender not verified"
        );
        require(
            identity.isVerified(to) || identity.isPublic(to),
            "BUCK: recipient not verified"
        );

        bytes32 toHash;
        if (identity.isPublic(to)) {
            toHash = _publicReceiptHash(to);
        } else {
            toHash = _receiptFragments[from][to];
            require(toHash != bytes32(0), "BUCK: missing identity receipt");
        }

        bytes32 fromHash = identity.isPublic(from)
            ? _publicReceiptHash(from)
            : _receiptFragments[to][from];

        _transfer(from, to, amount);
        emit BuckTransferReceipt(from, to, amount, fromHash, toHash);
    }

    // ---- helpers -----------------------------------------------------------

    function _ciphertextHash(IdentityRegistry.ElGamalCT calldata E)
        internal pure returns (bytes32)
    {
        return keccak256(abi.encode(E.R.X, E.R.Y, E.C.X, E.C.Y));
    }

    function _publicReceiptHash(address account) internal view returns (bytes32) {
        IdentityRegistry.ElGamalCT memory E = identity.ciphertextOf(account);
        return keccak256(abi.encode(E.R.X, E.R.Y, E.C.X, E.C.Y, "PUBLIC"));
    }

    /// @dev Premium = mintAmount * (BASE_RATE + util^2 * SCALE_RATE) / BP.
    ///      Utilisation is computed against the *raw* outstanding balance
    ///      (fee-debt counts as drawn credit), not the net spendable.
    function _computePremium(
        address account,
        uint256 mintAmount,
        uint256 limit
    ) internal view returns (uint256) {
        if (limit == 0) return 0;
        uint256 newBalance  = ERC20.balanceOf(account) + mintAmount;
        uint256 utilization = newBalance * PRECISION / limit;
        uint256 utilSq      = utilization * utilization / PRECISION;
        uint256 rate        = BASE_RATE + utilSq * SCALE_RATE / PRECISION;
        return mintAmount * rate / BP;
    }

    // ---- demurrage internals ----------------------------------------------

    function _areaNow() internal view returns (uint256) {
        return _areaAcc + totalSupply() * (block.timestamp - _areaLastUpdate);
    }

    /// @notice Required Jubilee balance at this block: BASE_RATE * area-under-supply.
    function jubileeTarget() public view returns (uint256) {
        return Math.mulDiv(_areaNow(), BASE_RATE_PER_SEC, SCALE);
    }

    /// @notice Current Jubilee actual balance (raw, bypasses balanceOf override).
    function jubileeActual() public view returns (uint256) {
        return ERC20.balanceOf(address(this));
    }

    /// @dev Cumulative demurrage index at this block.  Linear in time at flat
    ///      BASE_RATE_PER_SEC (no rate dynamics).
    function _cumIndexNow() internal view returns (uint256) {
        return _cumIndex + BASE_RATE_PER_SEC * (block.timestamp - _lastIndexUpdate);
    }

    /// @notice Fee owed by `a` at this block (BUCK, with 18 decimals).  Jubilee
    ///         is NOT exempt: it carries its own age basis like any other
    ///         account.  Its fee debt grows on its prior balance and carries
    ///         accumulated demurrage obligation on outflow (transfers from address(this)).
    function feeOwing(address a) public view returns (uint256) {
        uint256 raw = ERC20.balanceOf(a);
        return Math.mulDiv(raw, _cumIndexNow() - _indexAtLastTouch[a], SCALE);
    }

    /// @dev Advance the global cumIndex / areaAcc accumulators to `now`.  Must
    ///      be called BEFORE reading any cumIndex-dependent state in _update or
    ///      transferCarrying.
    function _advanceGlobals() internal {
        _cumIndex        = _cumIndexNow();
        _areaAcc         = _areaNow();
        _lastIndexUpdate = uint64(block.timestamp);
        _areaLastUpdate  = uint64(block.timestamp);
    }

    /// @dev Mint Jubilee deficit (target - rawBalance) to address(this) and
    ///      weighted-merge its idxAtLastTouch toward cumIndex so the fresh
    ///      BUCKs land at age 0 while Jubilee's prior fee debt is preserved.
    ///      Caller must have already called _advanceGlobals().
    function _accrueJubilee() internal {
        uint256 jubRaw = ERC20.balanceOf(address(this));
        uint256 target = jubileeTarget();
        if (target <= jubRaw) return;

        uint256 delta  = target - jubRaw;
        // Weighted-merge: (jubRaw * old_idx + delta * cumIndex) / target.
        // Identity check: feeOwing(jubilee) before == jubRaw*(cum-old_idx)/SCALE
        //                 feeOwing(jubilee) after  == target*(cum-new_idx)/SCALE
        // Both expand to jubRaw*(cum-old_idx)/SCALE -- prior debt preserved.
        _indexAtLastTouch[address(this)] =
            (jubRaw * _indexAtLastTouch[address(this)] + delta * _cumIndex) / target;

        _setSettling(true);
        super._mint(address(this), delta);
        _setSettling(false);

        emit JubileeAccrued(delta, target, _cumIndex);
    }

    /// @dev Burn `a`'s currently-owed fee from its raw balance and reset its
    ///      index to cumIndex.  Caller must have already advanced globals.
    function _settle(address a) internal {
        uint256 fee = feeOwing(a);
        if (fee > 0) {
            _setSettling(true);
            super._burn(a, fee);
            _setSettling(false);
            emit FeeBurned(a, fee, _cumIndex);
        }
        _indexAtLastTouch[a] = _cumIndex;
    }

    function _settling() private view returns (bool x) {
        assembly { x := tload(SETTLE_SLOT) }
    }
    function _setSettling(bool v) private {
        assembly { tstore(SETTLE_SLOT, v) }
    }

    // ---- ERC-20 hook overrides --------------------------------------------

    /// @notice Net spendable BUCK in `a` (raw balance minus accrued fee).
    function balanceOf(address a) public view override returns (uint256) {
        uint256 raw = ERC20.balanceOf(a);
        uint256 fee = feeOwing(a);
        return fee >= raw ? 0 : raw - fee;
    }

    /// @notice Raw BUCK balance of `a` (gross, before subtracting accrued fee).
    function rawBalanceOf(address a) public view returns (uint256) {
        return ERC20.balanceOf(a);
    }

    /// @dev Threads demurrage through every state change (mint, burn, transfer).
    ///      Order: advance globals -> accrue Jubilee -> settle `from` -> merge
    ///      recipient -> super._update.  Re-entry guard short-circuits the
    ///      recursive calls from advance-mint and settle-burn.
    function _update(address from, address to, uint256 value) internal override {
        if (_settling()) {
            super._update(from, to, value);
            return;
        }

        _advanceGlobals();
        _accrueJubilee();

        // Deducting: burn `from`'s accrued fee, reset its index.
        if (from != address(0)) {
            _settle(from);
        }

        // Carrying-style: weighted-merge `to`'s index with cumIndex.  Fresh
        // BUCKs land at age 0; recipient's prior fee debt is preserved.
        if (to != address(0)) {
            uint256 br = ERC20.balanceOf(to);
            if (br + value > 0) {
                _indexAtLastTouch[to] =
                    (br * _indexAtLastTouch[to] + value * _cumIndex) / (br + value);
            }
        }

        super._update(from, to, value);
    }

    // ---- transferCarrying --------------------------------------------------

    /// @notice Transfer `amount` to `to` carrying its accumulated BUCK-age.
    ///         No fee is burned at transfer time; recipient's index is shifted
    ///         backward by a balance-weighted average so total system age is
    ///         preserved.  Recipient eventually burns the same fee on a future
    ///         standard transfer out.
    function transferCarrying(address to, uint256 amount) external returns (bool) {
        address from = msg.sender;
        // Same sender rule as standard transfer: verified-or-public.  The
        // Notes pool is the canonical isPublic sender -- it pays out carried
        // BUCK on spend, absorbing the pool's average demurrage age into
        // the recipient's weighted index.
        require(
            identity.isVerified(from) || identity.isPublic(from),
            "BUCK: sender not verified"
        );
        require(
            identity.isVerified(to) || identity.isPublic(to),
            "BUCK: recipient not verified"
        );
        require(to != address(this), "BUCK: cannot carry to Jubilee");

        _advanceGlobals();
        _accrueJubilee();

        // Weighted-average merge of recipient's index toward sender's
        // idxAtLastTouch -- absorbs the carried age so post-merge fee-debt =
        // sender's pre-debt on `amount` plus recipient's pre-debt on its
        // existing balance.
        uint256 br = ERC20.balanceOf(to);
        uint256 idxFrom = _indexAtLastTouch[from];
        if (br + amount > 0) {
            _indexAtLastTouch[to] =
                (br * _indexAtLastTouch[to] + amount * idxFrom) / (br + amount);
        }
        // Sender's index unchanged: their remaining balance keeps its age.

        // Bypass _update's prologue/settle/merge so the carried merge survives.
        _setSettling(true);
        super._update(from, to, amount);
        _setSettling(false);

        emit TransferCarrying(from, to, amount);
        return true;
    }
}
