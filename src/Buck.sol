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
    function balanceOf(address owner) external view returns (uint256);
    function ownerOf(uint256 tokenId) external view returns (address);
    function tokenOfOwnerByIndex(address owner, uint256 index) external view returns (uint256);
    function creditInfo(uint256 tokenId)
        external view returns (uint256 faceValue, uint256 activatedValue, uint32 premiumRate);
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

    /// @dev Per-account demurrage state, packed into a single 256-bit slot.
    ///      Field order is fixed: `uint128 buckSeconds` at offset 0-15,
    ///      `uint64 timestamp` at offset 16-23, `uint64 reserved` at 24-31.
    ///
    ///      `buckSeconds` is the cumulative integral of (raw balance * dt)
    ///      crystallised through `timestamp`.  At view time:
    ///        feeOwing(a) = (buckSeconds + raw * (now - timestamp))
    ///                      * BASE_RATE_PER_SEC / SCALE
    ///      so storing the integral (rather than the locked-fee value) keeps
    ///      the rate independent of storage and avoids a `SCALE` rescale on
    ///      every crystallisation.
    ///
    ///      `reserved` is a 64-bit zero placeholder for future per-account
    ///      flags (frozen, account-class, etc.).  Using uint128 (not int128)
    ///      so the i64 balance cap is not yet enforced -- the existing
    ///      Notes / Spend SNARK fixtures encode 1e20-style face values that
    ///      exceed int64 range; a follow-up commit will regenerate those
    ///      fixtures and tighten the cap.
    struct DemurrageState {
        uint128 buckSeconds;
        uint64  timestamp;
        uint64  reserved;
    }
    mapping(address => DemurrageState) internal _state;

    // ---- premium / mutual-insurance pool model -----------------------------
    //
    // Mint(N) delivers N BUCK to the holder AND simultaneously mints a "pool
    // principal" deposit to insurancePool sized so that, at the insurer's
    // assumed annual ROI, the principal's investment yield exactly covers the
    // annual premium on the activated coverage:
    //
    //     pool_principal = annual_premium * POOL_ROI_INV
    //
    // POOL_ROI_INV = 10 (i.e. 10% assumed ROI; principal × 10% = premium).
    // The insurer keeps any return above 10% as profit.
    //
    // Per allocated NFT slice of size `take` at annual rate `r` (bp):
    //     annual_premium = take * r / BP
    //     pool_principal = take * r * POOL_ROI_INV / BP
    //     net to holder  = take - pool_principal
    //                    = take * (BP - r * POOL_ROI_INV) / BP
    //
    // Inverting: to deliver `delivery` net to the holder from one NFT,
    //     take = ceil(delivery * BP / (BP - r * POOL_ROI_INV))
    //
    // An NFT's effective rate (rate × POOL_ROI_INV) must stay strictly under
    // BP -- a 1000bp NFT consumes 100% of its take as principal and would
    // diverge.  Enforced per-allocation.
    //
    // Burn(N) is the inverse: the holder picks NFTs to unwind coverage on,
    // their balance drops by N, and pool_principal proportional to the
    // unwound coverage is burned from insurancePool (returning the
    // mutual-insurance investment).  Sort order on both paths is ascending
    // premiumRate so a mint-burn round-trip is rate-neutral and not
    // arbitrageable.
    //
    // Per-NFT outstanding mint allocations are tracked in `mintsBacked`; cap
    // per NFT is its current activatedValue.  Burn decrements; mint
    // increments.

    uint256 internal constant BP            = 10000;
    uint256 internal constant POOL_ROI_INV  = 10;     // 10% assumed annual ROI

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

    /// @notice Outstanding BUCK coverage backed by a given BuckCredit NFT.
    ///         Increases by `take` on mint (where take = holder_delivery +
    ///         pool_principal); decreases by `unwind` on burn.  Cap is the
    ///         NFT's current activatedValue (insurer's commitment).
    mapping(uint256 => uint256) public mintsBacked;

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

    /// @notice Mint `amount` BUCK to the caller; the mutual-insurance pool
    ///         principal (annual_premium × POOL_ROI_INV) is minted alongside
    ///         to insurancePool.  Coverage is drawn cheapest-first across the
    ///         caller's BuckCredit NFTs.
    function mint(uint256 amount) external {
        _mintAllocated(amount, _selectCheapest(msg.sender));
    }

    /// @notice Mint with a caller-supplied NFT order.  An off-chain optimizer
    ///         can pre-sort by effective cost (rate / depreciation / coverage
    ///         midpoint / ...) and hand the order in; iteration stops once
    ///         `amount` is fully delivered.
    function mint(uint256 amount, uint256[] calldata tokenIds) external {
        _mintAllocated(amount, tokenIds);
    }

    /// @notice Burn `amount` BUCK from the caller.  Coverage is unwound on
    ///         the chosen NFTs cheapest-first; the proportional pool
    ///         principal is burned from insurancePool.
    function burn(uint256 amount) external {
        _burnAllocated(amount, _selectCheapest(msg.sender));
    }

    /// @notice Burn with caller-supplied NFT unwind order.
    function burn(uint256 amount, uint256[] calldata tokenIds) external {
        _burnAllocated(amount, tokenIds);
    }

    function _mintAllocated(uint256 amount, uint256[] memory tokenIds) internal {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");

        // Limit ratchet + early gate using `amount` as a lower bound on the
        // total drawn coverage (totalCoverage >= amount because the pool
        // principal is non-negative).  The post-allocation check below
        // tightens it once the exact figure is known.
        uint256 totalCreditValue = buckCredit.totalCurrentValue(msg.sender);
        uint256 currentBuckK     = buckK.currentBuckK();
        uint256 maxLimit         = totalCreditValue * currentBuckK / PRECISION;
        if (maxLimit > storedLimit[msg.sender]) {
            storedLimit[msg.sender] = maxLimit;
        }
        uint256 limit = storedLimit[msg.sender];
        require(ERC20.balanceOf(msg.sender) + amount <= limit, "BUCK: exceeds credit limit");

        (uint256 totalCoverage, uint256 poolPrincipal) = _allocateMint(amount, tokenIds);

        require(
            ERC20.balanceOf(msg.sender) + totalCoverage <= limit,
            "BUCK: exceeds credit limit"
        );

        _mint(msg.sender, amount);
        if (poolPrincipal > 0) {
            _mint(insurancePool, poolPrincipal);
        }

        emit Minted(msg.sender, totalCoverage, poolPrincipal, totalCreditValue, currentBuckK, limit);
    }

    function _burnAllocated(uint256 amount, uint256[] memory tokenIds) internal {
        (, uint256 poolRefund) = _allocateBurn(amount, tokenIds);
        _burn(msg.sender, amount);
        if (poolRefund > 0) {
            _burn(insurancePool, poolRefund);
        }
    }

    /// @notice Quote the total coverage drawn and pool principal minted to
    ///         deliver `amount` net to a holder iterating `tokenIds` in
    ///         order.  Reverts (insufficient capacity / bad order) for the
    ///         same reasons mint() would.  Pure of state changes.
    function quoteMint(uint256 amount, uint256[] calldata tokenIds)
        external view returns (uint256 totalCoverage, uint256 poolPrincipal)
    {
        return _allocateMintView(amount, tokenIds);
    }

    /// @notice Symmetric quote for burn.
    function quoteBurn(uint256 amount, uint256[] calldata tokenIds)
        external view returns (uint256 totalUnwind, uint256 poolRefund)
    {
        return _allocateBurnView(amount, tokenIds);
    }

    /// @dev Allocate `amount` net delivery across `tokenIds` cheapest-first
    ///      and write mintsBacked.  Per-NFT inversion:
    ///      take = ceil(remaining × BP / (BP − rate × POOL_ROI_INV)).
    ///      The view-only twin `_allocateMintView` runs the same math without
    ///      writing storage; keep them in sync.
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
            uint256 avail = activated - used;
            uint256 denom = BP - effRate;
            uint256 netCap = avail * denom / BP;          // delivery this NFT can provide

            uint256 take;
            uint256 principal_i;
            if (netCap >= remaining) {
                // Closed-form inversion, ceil so delivery >= remaining.
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
            uint256 used  = mintsBacked[tid];
            if (activated <= used) continue;
            uint256 avail = activated - used;
            uint256 denom = BP - effRate;
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

    /// @dev Mirror of _allocateMint.  Walks tokenIds cheapest-first and
    ///      unwinds enough coverage so the holder's balance reduction equals
    ///      `amount`; the proportional pool principal is reported as
    ///      `poolRefund` for the caller to burn from insurancePool.
    function _allocateBurn(uint256 amount, uint256[] memory tokenIds)
        internal returns (uint256 totalUnwind, uint256 poolRefund)
    {
        uint256 remaining = amount;
        for (uint256 i = 0; i < tokenIds.length && remaining > 0; i++) {
            uint256 tid = tokenIds[i];
            require(buckCredit.ownerOf(tid) == msg.sender, "BUCK: not credit owner");
            (, , uint32 rate) = buckCredit.creditInfo(tid);
            uint256 effRate = uint256(rate) * POOL_ROI_INV;
            // effRate < BP is guaranteed by the mint-side check; if a stale
            // NFT survives at effRate >= BP, fall through harmlessly.
            uint256 used = mintsBacked[tid];
            if (used == 0 || effRate >= BP) continue;
            uint256 denom  = BP - effRate;
            uint256 netCap = used * denom / BP;       // holder reduction this NFT can absorb

            uint256 unwind;
            uint256 refund_i;
            if (netCap >= remaining) {
                unwind = (remaining * BP + denom - 1) / denom;
                if (unwind > used) unwind = used;
                refund_i = unwind - remaining;
                remaining = 0;
            } else {
                unwind = used;
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
                unwind = used;
                refund_i = unwind - netCap;
                remaining -= netCap;
            }
            totalUnwind += unwind;
            poolRefund  += refund_i;
        }
        require(remaining == 0, "BUCK: insufficient coverage to unwind");
    }

    /// @dev Build the caller's NFT list sorted ascending by premiumRate.
    ///      Insertion sort -- O(n^2) but n is the per-account NFT count
    ///      (typically a handful), and each iteration costs one storage read
    ///      via tokenOfOwnerByIndex + creditInfo.
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

    /// @notice Decimal scale.  6 matches USDC / USDT and gives ~9.22 trillion
    ///         BUCK of headroom per account when balances are eventually
    ///         capped at int64 (a follow-up that requires SNARK fixture
    ///         regeneration; see alberta-buck-demurrage.org).
    function decimals() public pure override returns (uint8) {
        return 6;
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

    // ---- demurrage views --------------------------------------------------

    /// @notice Fee owed by `a` at this block (BUCK, 18 decimals).
    ///         live_fee = _demurrage[a] + raw * RATE * (now - _timestamp[a]).
    ///         The Jubilee fund (address(this)) accrues demurrage like any
    ///         other account -- under the deferred-Jubilee model its raw
    ///         only grows at mint/burn checkpoints, but its self-demurrage
    ///         on idle accumulated balance is real.
    function feeOwing(address a) public view returns (uint256) {
        DemurrageState storage s = _state[a];
        uint256 raw      = ERC20.balanceOf(a);
        uint256 elapsed  = block.timestamp - s.timestamp;
        // Live integral: stored buckSeconds plus the rectangle since the
        // last crystallisation.  Both terms fit in uint256 trivially.
        uint256 buckSecondsLive = uint256(s.buckSeconds) + (raw * elapsed);
        if (buckSecondsLive == 0) return 0;
        return Math.mulDiv(buckSecondsLive, BASE_RATE_PER_SEC, SCALE);
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

    /// @dev Fold pending balance*dt into the packed _state[a] slot in a
    ///      single read/write pair.  Called before every balance-mutating
    ///      event for `a`.  Idempotent in time -- two calls within the same
    ///      block produce the same state as one.
    function _crystallize(address a) internal {
        DemurrageState memory s = _state[a];
        uint256 raw     = ERC20.balanceOf(a);
        uint256 elapsed = block.timestamp - s.timestamp;
        bool dirty = false;
        if (elapsed != 0 && raw != 0) {
            uint256 newBs = uint256(s.buckSeconds) + raw * elapsed;
            require(newBs <= type(uint128).max, "BUCK: buckSeconds overflow");
            s.buckSeconds = uint128(newBs);
            dirty = true;
        }
        if (s.timestamp != uint64(block.timestamp)) {
            s.timestamp = uint64(block.timestamp);
            dirty = true;
        }
        if (dirty) {
            _state[a] = s;
        }
    }

    /// @dev Crystallise `a` AND add an extra `extraBuckSeconds` to its
    ///      stored integral, in a single SSTORE.  Used by Carrying transfer
    ///      to fold the carried `value * age_basis` into the recipient's
    ///      state alongside the recipient's own crystallisation -- avoids
    ///      the back-to-back SSTORE that would happen if we called
    ///      _crystallize(to) then patched the slot a second time.
    function _crystallizeAndAdd(address a, uint256 extraBuckSeconds) internal {
        DemurrageState memory s = _state[a];
        uint256 raw     = ERC20.balanceOf(a);
        uint256 elapsed = block.timestamp - s.timestamp;
        uint256 newBs   = uint256(s.buckSeconds);
        if (elapsed != 0 && raw != 0) {
            newBs += raw * elapsed;
        }
        newBs += extraBuckSeconds;
        require(newBs <= type(uint128).max, "BUCK: buckSeconds overflow");
        s.buckSeconds = uint128(newBs);
        s.timestamp   = uint64(block.timestamp);
        _state[a] = s;
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
    ///      buckSeconds absorbs `value * (now - sender.timestamp)` in
    ///      addition to its own crystallisation, in one SSTORE.
    function _carryingTransfer(address from, address to, uint256 value) internal {
        // age_basis is the elapsed time on sender's basis at this moment;
        // captured BEFORE the recipient is crystallised since
        // _crystallize{,AndAdd} does not touch _state[from].
        uint256 ageBasis = block.timestamp - _state[from].timestamp;
        uint256 carriedBuckSeconds = (value == 0 || ageBasis == 0)
            ? 0
            : value * ageBasis;
        _crystallizeAndAdd(to, carriedBuckSeconds);
        super._update(from, to, value);
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
