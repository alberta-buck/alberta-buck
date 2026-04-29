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
    // Conservation model.  Demurrage is purely a VIEW-LAYER computation; no
    // BUCK is ever moved or burned to satisfy it.  totalSupply changes ONLY
    // at user-initiated mint() / burn().
    //
    // Per-account state = (raw, idx).  At time t with cumIndex(t) linear in
    // time:
    //   feeOwing(a)  = raw[a] * (cumIndex(t) - idx[a]) / SCALE
    //   balanceOf(a) = raw[a] - feeOwing(a)            -- spendable
    //
    // The Jubilee fund's claim against the system is the sum of every
    // account's feeOwing.  This sum equals BASE_RATE * ∫ totalSupply dt
    // exactly, provided every transfer preserves system fee debt.  That
    // requires *carrying* recipient-merge:
    //   idx[to]_new = (raw[to] * idx[to] + value * idx[from]) / (raw[to] + value)
    // (mints, having no sender, use cumIndex as the basis -- fresh BUCKs at
    // age 0).  The sender's idx is unchanged on outflow; raw[from] simply
    // decreases by value.
    //
    // Algebraic identity (carrying merge conserves system fee debt):
    //   sum_a raw[a] * (cumIndex - idx[a])  is invariant under transfer.
    // Together with d(sum_a raw[a] * (cumIndex - idx[a])) / dt
    //              = BASE_RATE_PER_SEC * totalSupply
    // this gives sum_a feeOwing(a) = BASE_RATE * area_under_supply
    // identically -- the Jubilee target *is* the implied claim, no separate
    // accounting needed.  Hence jubileeTarget() returns the implied balance.
    //
    // The Jubilee address (= address(this)) is exempt from demurrage:
    // feeOwing(address(this)) is defined to be zero.  Jubilee deployment
    // (e.g., to the insurance pool) is performed by minting fresh BUCK; the
    // governance layer caps mint amounts by the unspent jubileeTarget.
    //
    // _areaAcc is updated only when totalSupply changes (mint/burn).  Pure
    // transfers leave it untouched -- between supply changes totalSupply is
    // piecewise constant and the live area is computed lazily as
    //   _areaAcc + totalSupply * (now - _areaLastUpdate).

    uint256 internal constant SCALE              = 1e27;
    uint256 internal constant BASE_RATE_PER_YEAR = 2e25;                              // 0.02 in SCALE
    uint256 internal constant SECONDS_PER_YEAR_  = 365 days + 6 hours;                // 365.25 days
    uint256 internal constant BASE_RATE_PER_SEC  = BASE_RATE_PER_YEAR / SECONDS_PER_YEAR_;

    uint64  internal immutable _genesis;  // cumIndex starts at 0 here, grows linearly.

    uint256 internal _areaAcc;            // ∫ totalSupply dt up to _areaLastUpdate.
    uint64  internal _areaLastUpdate;

    mapping(address => uint256) internal _indexAtLastTouch;

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

        _genesis        = uint64(block.timestamp);
        _areaLastUpdate = uint64(block.timestamp);
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

    // ---- demurrage internals ----------------------------------------------

    /// @dev Live area-under-totalSupply integral at this block.  totalSupply
    ///      is piecewise-constant between mint/burn events, so the area is
    ///      _areaAcc plus the rectangle of the current totalSupply since
    ///      _areaLastUpdate.  Pure transfers do NOT advance _areaAcc.
    function _areaNow() internal view returns (uint256) {
        return _areaAcc + totalSupply() * (block.timestamp - _areaLastUpdate);
    }

    /// @notice The Jubilee fund's claim against the system at this block:
    ///         BASE_RATE * area_under_supply.  Under the conservation model
    ///         this equals sum_a feeOwing(a) identically -- no separate
    ///         storage is needed to track a Jubilee actual.
    function jubileeTarget() public view returns (uint256) {
        return Math.mulDiv(_areaNow(), BASE_RATE_PER_SEC, SCALE);
    }

    /// @notice Literal raw BUCK held at the Jubilee address (= address(this)).
    ///         Under the conservation model no BUCK is ever moved to the
    ///         Jubilee for demurrage; this is non-zero only if an external
    ///         party deliberately transferred BUCK to the contract.  The
    ///         meaningful Jubilee claim is jubileeTarget().
    function jubileeActual() public view returns (uint256) {
        return ERC20.balanceOf(address(this));
    }

    /// @dev Cumulative demurrage index at this block.  Linear in time, no
    ///      rate dynamics; storage-free.
    function _cumIndexNow() internal view returns (uint256) {
        return BASE_RATE_PER_SEC * (block.timestamp - _genesis);
    }

    /// @notice Fee owed by `a` at this block (BUCK, 18 decimals).  Jubilee
    ///         (address(this)) is exempt -- its claim is materialized via
    ///         jubileeTarget(), not as a per-account fee on whatever raw it
    ///         happens to hold.
    function feeOwing(address a) public view returns (uint256) {
        if (a == address(this)) return 0;
        uint256 raw = ERC20.balanceOf(a);
        return Math.mulDiv(raw, _cumIndexNow() - _indexAtLastTouch[a], SCALE);
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

    /// @dev Threads demurrage through every state change.  Conservation model:
    ///      no fee transfer, no fee burn, no advance-mint to Jubilee.  All
    ///      the contract has to do is (i) advance _areaAcc when totalSupply
    ///      is about to change, and (ii) carry-merge the recipient's idx so
    ///      the system fee-debt is conserved across transfers.  Sender side
    ///      is untouched: raw[from] decreases by value (super._update),
    ///      idx[from] keeps its prior basis -- the residual carries its
    ///      original age and the transferred portion's fee debt is
    ///      absorbed by the recipient via the merge.  Sum_a feeOwing(a) ==
    ///      BASE_RATE * area_under_supply at all times.
    ///
    ///      Note: a sender CAN transfer up to raw[from] (super._update's
    ///      ERC-20 guard).  Spending more than balanceOf(from) merely
    ///      transfers some fee debt with the BUCK -- the recipient's idx
    ///      absorbs it.  balanceOf(from) is the cap on "send without
    ///      passing any of my fee debt to the recipient".
    function _update(address from, address to, uint256 value) internal override {
        // Advance _areaAcc only when totalSupply is about to change.
        // (mint: from == 0, burn: to == 0; both adjust totalSupply via
        // super._update.  Pure transfers conserve totalSupply.)
        if (from == address(0) || to == address(0)) {
            _areaAcc        = _areaNow();
            _areaLastUpdate = uint64(block.timestamp);
        }

        // Carrying recipient-merge.  Mints (from == 0) come in at age 0
        // (cumIndex basis); transfers carry the sender's age basis so
        // sum_a feeOwing(a) is preserved.  Jubilee (address(this)) is
        // exempt: feeOwing(jubilee) is identically zero, so its idx is
        // never read -- skip the write.
        if (to != address(0) && to != address(this)) {
            uint256 br          = ERC20.balanceOf(to);
            uint256 incomingIdx = (from == address(0))
                ? _cumIndexNow()
                : _indexAtLastTouch[from];
            if (br + value > 0) {
                _indexAtLastTouch[to] =
                    (br * _indexAtLastTouch[to] + value * incomingIdx) / (br + value);
            }
        }

        super._update(from, to, value);
    }

    // ---- transferCarrying --------------------------------------------------

    /// @notice Alias for `transfer` retained for ABI compatibility with
    ///         BUCK-aware contracts (Notes, etc.).  Under the conservation
    ///         model every transfer already carries the sender's BUCK-age
    ///         basis to the recipient via the merge in _update; the previous
    ///         distinction between deducting and carrying transfers is gone.
    function transferCarrying(address to, uint256 amount) external returns (bool) {
        return transfer(to, amount);
    }
}
