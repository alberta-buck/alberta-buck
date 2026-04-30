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
    // See alberta-buck-demurrage.org for the full model.  Summary:
    //
    // Per-account state = (_balances[a], _demurrage[a], _timestamp[a]).
    //   live_fee(a)        = _demurrage[a] + _balances[a] * RATE * (now - _timestamp[a])
    //
    // The view-layer semantics depend on identity flavour:
    //   Non-Carrying:
    //     balanceOfFees(a) = min(live_fee(a), _balances[a])    -- locked dust
    //     balanceOf(a)     = _balances[a] - balanceOfFees(a)   -- spendable
    //   Carrying:
    //     balanceOfFees(a) = live_fee(a)                        -- carried on outflow
    //     balanceOf(a)     = _balances[a]                      -- raw (no decay)
    //
    // The Carrying-balanceOf-equals-raw rule is what lets stock ERC-20
    // consumers (Uniswap, AMMs, etc.) co-exist with BUCK across long idle
    // periods: a pair / pool's balanceOf matches its cached reserve and the
    // K invariant remains satisfiable.  The accumulated fees ride with
    // outflows -- a Carrying account's transfer pushes
    // `value * RATE * (now - _timestamp[from])` into the recipient's
    // _demurrage, and balanceOf(recipient) reflects it via the recipient's
    // own (Non-Carrying) view subtraction.
    //
    // Crystallization fires before every balance-mutating event for `a`:
    //   _demurrage[a] += pending_fee;  _timestamp[a] = now.
    // It is idempotent in time -- pure read paths never call it.
    //
    // Mint/burn paths run _accrueJubilee() which super._update-mints
    //   delta = totalSupply * RATE * (now - _jubileeLastUpdate)
    // to address(this), bringing the Jubilee fund to the cumulative claim
    // it has on the system.  Pure transfers do NOT trigger Jubilee accrual.
    // totalSupply therefore grows over time (and unwinds at lien close).
    //
    // Transfer dispatch is via IdentityRegistry.isCarrying(from):
    //   - false  (EOA / user wallet): _nonCarryingTransfer.  Crystallize
    //            both sides; sender's _demurrage retains its locked fees;
    //            recipient gets fresh BUCKs (no carried fee).  Spendable
    //            check `value <= balanceOf(from)` enforced.
    //   - true   (service contract): _carryingTransfer.  Sender's basis
    //            untouched; recipient's _demurrage absorbs
    //            `value * RATE * (now - _timestamp[from])`.  Sender may
    //            transfer up to raw (the carried fee debt rides with the
    //            BUCK; recipient's balanceOf reflects it via the merge).

    uint256 internal constant SCALE              = 1e27;
    uint256 internal constant BASE_RATE_PER_YEAR = 2e25;                              // 0.02 in SCALE
    uint256 internal constant SECONDS_PER_YEAR_  = 365 days + 6 hours;                // 365.25 days
    uint256 internal constant BASE_RATE_PER_SEC  = BASE_RATE_PER_YEAR / SECONDS_PER_YEAR_;

    uint64  internal _jubileeLastUpdate;

    mapping(address => uint256) internal _demurrage;
    mapping(address => uint64)  internal _timestamp;

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

        _jubileeLastUpdate = uint64(block.timestamp);
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
        require(identity.isVerified(spender),    "BUCK: spender not verified");

        // CP fires regardless of spender's identity flavor (Public or
        // Encrypted): the receipt encrypts the caller's identity point M
        // under the spender's pk so the spender's operator (with sk_spender)
        // can later decrypt to identify the caller -- the audit-trail
        // property the contract owner needs for subpoena response.
        require(
            identity.verifyApprove(msg.sender, spender, E_bob, pi_CP),
            "BUCK: bad CP proof"
        );
        bytes32 receipt = _ciphertextHash(E_bob);
        _receiptFragments[msg.sender][spender] = receipt;
        emit ApproveReceipt(msg.sender, spender, receipt);

        // Freeze spender's isCarrying flag in the registry: from this
        // moment on, the carrying-flavour the recipient consented to is
        // immutable.  Idempotent across multiple approvers.
        identity.markApproved(spender);

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
        require(identity.isVerified(from), "BUCK: sender not verified");
        require(identity.isVerified(to),   "BUCK: recipient not verified");

        // toHash carries the CP-encrypted recipient identity from a prior
        // approve.  EOA-to-EOA Encrypted transfers MUST have one (strong
        // privacy: only the recipient's operator can decrypt to learn the
        // sender).  Transfers where either party has a Public Identity (an
        // AMM pool, custodial vault, etc.) are allowed to fall back to a
        // deterministic _identityHash because the public party's operator
        // already has off-chain attestation pinning m to a known counterparty
        // -- the fallback simply makes the same correlation publicly
        // recomputable from the registry, which is a property the public
        // party already accepted by binding a Public Identity.
        bytes32 toHash = _receiptFragments[from][to];
        if (toHash == bytes32(0)) {
            require(
                identity.isPublicIdentity(from) || identity.isPublicIdentity(to),
                "BUCK: missing identity receipt"
            );
            toHash = _identityHash(to);
        }
        // fromHash is best-effort: if the recipient hasn't pre-attested back
        // to the sender, fall back to the sender's deterministic identity
        // hash.  Auditors aggregating transfers can still attribute the
        // counterparty side via the registry; only the directional privacy
        // toHash protects is sacrificed.
        bytes32 fromHash = _receiptFragments[to][from];
        if (fromHash == bytes32(0)) fromHash = _identityHash(from);

        _transfer(from, to, amount);
        emit BuckTransferReceipt(from, to, amount, fromHash, toHash);
    }

    // ---- helpers -----------------------------------------------------------

    function _ciphertextHash(IdentityRegistry.ElGamalCT calldata E)
        internal pure returns (bytes32)
    {
        return keccak256(abi.encode(E.R.X, E.R.Y, E.C.X, E.C.Y));
    }

    /// @dev Deterministic identity hash from the registered (pk, E_addr).
    ///      Used as a fallback receipt fragment when no prior approve-time CP
    ///      receipt exists between two verified counterparties (e.g., the
    ///      passive-receive direction of an AMM swap).
    function _identityHash(address account) internal view returns (bytes32) {
        BN254.G1Point memory pk            = identity.pkOf(account);
        IdentityRegistry.ElGamalCT memory E = identity.ciphertextOf(account);
        return keccak256(abi.encode(pk.X, pk.Y, E.R.X, E.R.Y, E.C.X, E.C.Y));
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

    // ---- demurrage views --------------------------------------------------

    /// @notice Fee owed by `a` at this block (BUCK, 18 decimals).
    ///         live_fee = _demurrage[a] + raw * RATE * (now - _timestamp[a]).
    ///         The Jubilee fund (address(this)) accrues demurrage like any
    ///         other account -- under the deferred-Jubilee model its raw
    ///         only grows at mint/burn checkpoints, but its self-demurrage
    ///         on idle accumulated balance is real.
    function feeOwing(address a) public view returns (uint256) {
        uint256 raw     = ERC20.balanceOf(a);
        uint256 elapsed = block.timestamp - _timestamp[a];
        uint256 pending = (raw == 0 || elapsed == 0)
            ? 0
            : Math.mulDiv(raw, BASE_RATE_PER_SEC * elapsed, SCALE);
        return _demurrage[a] + pending;
    }

    /// @notice Accumulated fees on `a`'s balance.  Semantics differ by
    ///         identity flavour:
    ///           * Non-Carrying:  locked dust inaccessible to the holder.
    ///                            Capped at raw -- the account is never
    ///                            "short" more than it actually holds.
    ///           * Carrying:      the carried-on-outflow fee that rides with
    ///                            transfers.  NOT capped at raw (a very old
    ///                            Carrying account can owe more than it
    ///                            holds; on a full-raw outflow the recipient
    ///                            absorbs the over-debt via _demurrage[to]).
    function balanceOfFees(address a) public view returns (uint256) {
        uint256 fee = feeOwing(a);
        if (identity.isCarrying(a)) {
            return fee;
        }
        uint256 raw = ERC20.balanceOf(a);
        return fee >= raw ? raw : fee;
    }

    /// @notice Spendable BUCK at `a`.  Semantics differ by identity flavour:
    ///           * Non-Carrying:  raw - locked fees.  Decreases over time
    ///                            against an idle holding -- the locked dust
    ///                            stays in the account but the holder cannot
    ///                            spend it.
    ///           * Carrying:      raw, full-stop.  The account's accumulated
    ///                            fees do NOT subtract from balanceOf because
    ///                            they are carried on outflow, not locked
    ///                            inside the account.  This is what AMM
    ///                            pools, Notes pools, and the Jubilee fund
    ///                            need: their balanceOf must match the
    ///                            actual transferable raw, otherwise stock
    ///                            ERC-20 consumers (e.g. Uniswap's K
    ///                            invariant) silently break across long
    ///                            idle periods.
    function balanceOf(address a) public view override returns (uint256) {
        uint256 raw = ERC20.balanceOf(a);
        if (identity.isCarrying(a)) {
            return raw;
        }
        uint256 fee = feeOwing(a);
        return fee >= raw ? 0 : raw - fee;
    }

    /// @notice Raw BUCK balance (gross, OZ-storage view).  Equals
    ///         balanceOf(a) + balanceOfFees(a) at every block.
    function rawBalanceOf(address a) public view returns (uint256) {
        return ERC20.balanceOf(a);
    }

    /// @notice Spendable BUCK held by the Jubilee fund.  Alias for
    ///         balanceOf(address(this)); separate name for clarity in
    ///         tooling that aggregates Jubilee state.
    function jubileeBalance() external view returns (uint256) {
        return balanceOf(address(this));
    }

    /// @notice Raw BUCK held at the Jubilee address.  Grows at mint/burn
    ///         events via _accrueJubilee; never via demurrage transfers
    ///         (there are none under this model).
    function jubileeActual() public view returns (uint256) {
        return ERC20.balanceOf(address(this));
    }

    // ---- demurrage internals ----------------------------------------------

    /// @dev Fold pending fee on `a` into _demurrage[a] and reset _timestamp.
    ///      Called before every balance-mutating event for `a`.  Idempotent
    ///      in time -- two calls within the same block produce the same
    ///      state as one.
    function _crystallize(address a) internal {
        uint256 raw     = ERC20.balanceOf(a);
        uint64  ts      = _timestamp[a];
        uint256 elapsed = block.timestamp - ts;
        if (elapsed != 0 && raw != 0) {
            _demurrage[a] += Math.mulDiv(raw, BASE_RATE_PER_SEC * elapsed, SCALE);
        }
        if (ts != block.timestamp) {
            _timestamp[a] = uint64(block.timestamp);
        }
    }

    /// @dev Mint accumulated cumulative-rate to the Jubilee fund.
    ///      Only the mint/burn paths call this; pure transfers don't.
    ///      Updates _jubileeLastUpdate FIRST so re-entry through super._update
    ///      (which fires our _update override) sees elapsed == 0.
    function _accrueJubilee() internal {
        uint256 elapsed = block.timestamp - _jubileeLastUpdate;
        if (elapsed == 0) return;
        uint256 supply = totalSupply();
        if (supply == 0) {
            _jubileeLastUpdate = uint64(block.timestamp);
            return;
        }
        uint256 delta = Math.mulDiv(supply, BASE_RATE_PER_SEC * elapsed, SCALE);
        _jubileeLastUpdate = uint64(block.timestamp);
        _crystallize(address(this));
        if (delta != 0) {
            // Direct super._update bypasses our _update override (no
            // recursion) and credits raw to the Jubilee.  totalSupply
            // grows by delta.
            super._update(address(0), address(this), delta);
        }
    }

    /// @dev Non-Carrying transfer: sender retains its locked fees;
    ///      recipient gets fresh BUCKs (no inherited fee debt).
    function _nonCarryingTransfer(address from, address to, uint256 value) internal {
        require(value <= balanceOf(from), "BUCK: amount exceeds spendable");
        _crystallize(from);
        _crystallize(to);
        super._update(from, to, value);
    }

    /// @dev Carrying transfer: sender's basis untouched; recipient's
    ///      _demurrage absorbs the proportional age basis on the
    ///      transferred portion.
    function _carryingTransfer(address from, address to, uint256 value) internal {
        // age_basis is the elapsed time on sender's basis at this moment;
        // captured BEFORE crystallizing the recipient since crystallize
        // does not touch _timestamp[from].
        uint256 ageBasis    = block.timestamp - _timestamp[from];
        uint256 carriedFee  = (value == 0 || ageBasis == 0)
            ? 0
            : Math.mulDiv(value, BASE_RATE_PER_SEC * ageBasis, SCALE);
        _crystallize(to);
        super._update(from, to, value);
        if (carriedFee != 0) {
            _demurrage[to] += carriedFee;
        }
    }

    // ---- ERC-20 hook override ---------------------------------------------

    /// @dev Threads demurrage through every state change.
    ///      mint/burn paths run _accrueJubilee + crystallize the user side
    ///      and super._update.  Inter-account transfers dispatch to either
    ///      _nonCarryingTransfer or _carryingTransfer based on the sender's
    ///      registry-side isCarrying flag.
    function _update(address from, address to, uint256 value) internal override {
        if (from == address(0) || to == address(0)) {
            // mint or burn -- accrue Jubilee against the elapsed period,
            // crystallize the user side, and let OZ ERC20 mutate the raw.
            _accrueJubilee();
            if (from != address(0)) {
                require(value <= balanceOf(from), "BUCK: amount exceeds spendable");
                _crystallize(from);
            }
            if (to != address(0)) {
                _crystallize(to);
            }
            super._update(from, to, value);
            return;
        }

        if (identity.isCarrying(from)) {
            _carryingTransfer(from, to, value);
        } else {
            _nonCarryingTransfer(from, to, value);
        }
    }
}
