// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20}         from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {Math}           from "@openzeppelin/contracts/utils/math/Math.sol";

import {BN254}            from "./BN254.sol";
import {BuckTypes, BuckQty, BuckSeconds, CreditSlice, toBuckQty, toBuckQtySigned, toBuckSeconds} from "./BuckTypes.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";

/// @title Buck — identity-bound ERC-20 with signed balances, demurrage, and
///        Jubilee relief (alberta-buck-ethereum.org, "BUCK: ERC-20 Token").
///
/// Every account's transfer-path state is one packed slot (AccountState):
/// a SIGNED balance -- positive: BUCK held; negative: a lien, credit drawn
/// against the account's BuckCredits -- and the seconds that balance has
/// accrued (fee-seconds while positive, issuance-seconds while negative).
///
/// *Issuance.*  BUCK are issued only against a lien: `mint` activates
/// BuckCredit coverage, raising the account's credit limit to
/// K x (the credits' present value), and a transfer that spends past the
/// held BUCK draws the rest on credit.  `burn` releases coverage.  A K cut
/// lowers limits, never liens: an account whose lien exceeds its limit
/// simply cannot draw more.
///
/// *Demurrage and relief.*  Held BUCK accrue a 2%/yr fee, locked inside the
/// balance (balanceOf = raw - fee) and taken out of circulation ("realized")
/// only when the BUCK carrying it would shed it -- a spend past held BUCK
/// into credit, or aged BUCK arriving at an account below zero.  Liens
/// accrue relief at the same 2%/yr, paid from the Jubilee fund when the
/// lien is repaid, at burn, or when the holder settles.  The fund accrues
/// 2%/yr on the BUCK issued, so it always holds the relief it owes.
///
/// Invariants the code maintains (tests: BuckDemurrage, BuckSignedBalance,
/// JubileeBasis -- the last fuzzes all of them):
///   I0  _totalSupply == sum over non-Jubilee a of max(0, raw(a))
///   J   sum_a max(0, raw(a)) == totalSupply + jubileeActual
///   S   totalSupply == sum(liens) + reliefRealized - feesRealized
///   I1  raw(a) == 0  =>  buckSeconds(a) == 0
///   M   mintsBacked[tid] == BuckCredit.activatedValue(tid)
///
/// Nothing derived is stored.  An account's credit limit, and therefore its
/// balanceOf, is recomputed from live BuckCredit state on every read.
interface IBuckK {
    function currentBuckK() external view returns (uint256);
    /// @dev State-changing accessor.  Runs a PID cycle if `dT` has elapsed,
    ///      otherwise returns the cached value.  Buck mints/burns call this
    ///      so user activity drives (and amortizes) PID work.
    function compute() external returns (uint256);
    /// @dev Counter-cyclical insurance funding factor (18-dec; 1e18 == 1.0).
    ///      Buck.mint gates on `balanceOf(minter) >= poolPrincipal *
    ///      fundingFactor / 1e18`, the minter's balance read before the
    ///      mint: it must already hold (as positive BUCK or unused credit) a
    ///      reserve scaled to the insurance deposit the mint pays.  A
    ///      zero-premium mint pays no deposit and is exempt.  The Static
    ///      controller returns 0 (gate disabled).
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
    /// @dev Interim: one address receives every mint's premium deposit and
    ///      pays every refund; deployments bind it Carrying
    ///      through the registry.  Intended: each credit's insurer holds its
    ///      credits' deposits in a Carrying premium pool, and this parameter
    ///      goes (alberta-buck-ethereum.org, "The Insurance Pool: an Interim
    ///      Stand-in").
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

    /// @dev AccountState.flags bit 0: this account's demurrage is routed to
    ///      `demurragePayer[account]`.  The only flag the hot path tests.
    uint16  internal constant FLAG_SPONSORED     = 0x0001;

    // ---- reentrancy guard ---------------------------------------------------
    //
    // Transient storage (EIP-1153; the build targets cancun).  TSTORE / TLOAD
    // are 100 gas flat with no cold tier, no refund accounting, and no
    // persistent slot, so the guard occupies no storage slot.  Measured cost
    // is ~600 gas on a guarded call against ~5150 for a storage-slot guard.
    //
    // Deliberately a contract-level mutex rather than a bit in AccountState.
    // A per-account bit is cheaper still (~390 gas, since the slot is written
    // anyway) but guards the wrong thing: an attacker reenters from whatever
    // address they like, so locking `msg.sender` stops nothing, and locking
    // every account an operation touches means publishing a lock SSTORE per
    // account before each call-out.  Worse, a persistent bit sharing a slot
    // with the balance is silently cleared by any of this contract's
    // read-struct-into-memory / write-struct-back sequences, and a path that
    // sets it and returns without clearing bricks that account forever.
    // Transient state cannot survive the transaction, so it cannot brick
    // anything.
    bool private transient _entered;

    /// @dev Blocks reentry into any BUCK state-mutating entry point.
    ///      BuckCredit never calls back into Buck -- the call graph between
    ///      the two contracts runs one way -- so the guard makes no exemption
    ///      for it.
    ///
    ///      It is not applied to the plain 2-arg `approve`, which touches only
    ///      the allowance map and calls nothing: re-entering it grants an
    ///      attacker no capability a separate transaction would not, and it is
    ///      the single hottest entry point for router / Permit2 integration.
    ///      The 4-arg identity-bound `approve` IS guarded -- it calls into the
    ///      registry.
    modifier nonReentrant() {
        require(!_entered, "BUCK: reentrant");
        _entered = true;
        _;
        _entered = false;
    }

    // ---- packed per-account state ------------------------------------------
    //
    //   balance      int80    raw stored balance, signed: > 0 BUCK held,
    //                         < 0 a lien (credit drawn).  Range +-6.04e23 raw
    //                         (+-6.04e17 BUCK at 6 decimals).  Spendable for a
    //                         non-Carrying account: held - feeOwing + unused
    //                         credit (`balanceOf`).
    //
    //   buckSeconds  uint120  cumulative integral of (|balance| * dt)
    //                         crystallised through `timestamp`, read by the
    //                         sign of `balance`:
    //                           balance > 0  fee-seconds: the demurrage the
    //                                        held BUCK owe,
    //                                        feeOwing = (bs + balance*elapsed)
    //                                                   * BASE_RATE_PER_SEC / SCALE
    //                           balance < 0  issuance-seconds: the lien over
    //                                        time, on which relief accrues
    //                           balance = 0  always 0 (invariant I1)
    //                         A balance changes sign only at a write, after
    //                         `_crystallize` has folded the old side through
    //                         now, so the old side's seconds are settled
    //                         there and the new side's start at 0 (see
    //                         `_debit` / `_credit`).
    //
    //   timestamp    uint40   last crystallisation (seconds since epoch).
    //                         2^40 sec ≈ year 36812 -- safe past 2038.
    //
    //   flags        uint16   bit 0     FLAG_SPONSORED -- this account's
    //                                   demurrage routes to demurragePayer[a].
    //                         bits 1-15 reserved.
    //
    //                         This word is scarce: it rides in the slot the
    //                         transfer path already loads and stores, which
    //                         is exactly what makes a bit here free to test
    //                         and therefore worth spending only on hot-path
    //                         dispatch.  Cold-path bookkeeping belongs in its
    //                         own slot -- the count of accounts a payer
    //                         carries lives in `sponseeCount`, not here.
    //                         Note this is also deliberately NOT where a
    //                         reentrancy guard lives -- see `_entered`.

    struct AccountState {
        BuckQty     balance;       // uint80 underlying; cap = BuckTypes.MAX_BALANCE
        BuckSeconds buckSeconds;   // uint120 underlying; cap = BuckTypes.MAX_BS
        uint40      timestamp;
        uint16      flags;
    }
    mapping(address => AccountState) internal _state;

    // ---- ERC-20 supply, allowances, identity receipts ---------------------

    uint256 internal _totalSupply;
    mapping(address => mapping(address => uint256)) private _allowances;
    /// @dev keccak256(E_to) per (from, to), laid down by the identity-bound
    ///      approve (a Chaum-Pedersen re-encryption of `from`'s identity to
    ///      `to`'s key): the receipt `_identityCheckedTransfer` requires of a
    ///      private party.
    mapping(address => mapping(address => bytes32)) internal _receiptFragments;

    // ---- mint-side bookkeeping (rare path) ---------------------------------

    /// @notice Outstanding BUCK coverage backed by a given BuckCredit NFT,
    ///         in its face units.  Equal to its `activatedValue` at every
    ///         observation point: activation happens only in `_allocateMint`
    ///         and deactivation only in `_allocateBurn`, each moving both.
    mapping(uint256 => uint256) public mintsBacked;

    /// @notice Pool principal currently held against a given BuckCredit NFT.
    /// @dev    The deposit is *returnable*.  Its yield at the insurer's
    ///         assumed ROI funds the premium in perpetuity -- that is what
    ///         makes a policy a one-time purchase rather than a recurring
    ///         expense -- so what the holder actually pays for cover is the
    ///         opportunity cost of the deposit, not the deposit.  Releasing
    ///         coverage returns it pro rata on the face units released.
    ///
    ///         It has to be stored rather than recomputed: the deposit for a
    ///         credit is the sum over past draws of `V_i * effRate_i / BP`,
    ///         and both the appraisal and the premium rate can have moved
    ///         between them, so `mintsPrincipal / mintsBacked` is a weighted
    ///         average that no amount of present-day state can reconstruct.
    mapping(uint256 => uint256) public mintsPrincipal;

    // ---- the Jubilee ---------------------------------------------------------
    //
    // Demurrage accrues on BUCK in circulation; relief pays back exactly that
    // to whoever issued them.  Every BUCK is issued against a lien (a
    // negative balance), and only two things move the sum of the non-Jubilee
    // signed balances -- relief paid out of the fund, and fees realized (taken
    // out of circulation) -- so
    //
    //     totalSupply = sum(liens) + reliefRealized - feesRealized
    //
    // and `totalIssued()` (= sum(liens)), which the fund accrues on, is
    // computable from these counters without touching the transfer hot path.
    //
    // The fund is the account at address(this), treated as Carrying.  Every
    // operation that moves the BUCK issued calls `_accrueJubilee` first, which
    // adds totalIssued * RATE * elapsed to the fund's raw balance by a direct
    // slot write (not a mint: totalSupply is unchanged), so each period
    // accrues at the base that held through it.  Invariant:
    //
    //     sum_a max(0, signedRaw(a)) == totalSupply + jubileeActual
    //
    // Every BUCK has exactly one fee owner: a non-Carrying holder, whose fee
    // is locked inside its raw balance (balanceOf = raw - feeOwing), or a
    // Carrying one, whose fee rides out with its BUCK (a carrying transfer
    // hands the recipient liveBs * value / raw of its buck-seconds).

    /// @dev Timestamp through which the fund's accrual has been applied.
    uint64 internal _jubileeLastUpdate;
    /// @notice Cumulative fees realized: demurrage taken out of circulation
    ///         when the BUCK carrying it repaid a lien or were spent past into
    ///         credit (`FeeRealized` events sum to it).
    uint256 public feesRealized;
    /// @notice Cumulative relief paid out of the fund to issuers
    ///         (`JubileeRedeemed` events sum to it).
    uint256 public reliefRealized;

    // ---- delegated demurrage (fee payer) -----------------------------------
    //
    // An account may route its demurrage exposure to a designated payer, so
    // that an Identity's several accounts concentrate their fee erosion in
    // one place instead of each one's balance being eaten from underneath it.
    //
    // The mechanism is a *transfer of buckSeconds*, not a discount.  Buck's
    // demurrage is a lien, never a movement: an account's fee is locked
    // inside its own raw balance (balanceOf = raw - fee), and the fees so
    // locked are the demurrage the fund's relief pays back to issuers.
    // Destroy buckSeconds anywhere and that demurrage goes uncollected while
    // its relief is still paid -- silent inflation.  So delegation moves the
    // (balance * dt) rectangle from the sponsored account's slot into the
    // payer's slot at crystallisation.  The total is conserved exactly; only
    // its owner moves.
    //
    // Absorption is capped at the payer's own capacity to carry a lien --
    // the point where feeOwing(payer) would exceed rawBalance(payer).  Past
    // that the lien would be uncollectible and delegation WOULD become an
    // escape hatch from demurrage.  Whatever the payer cannot carry stays
    // with the sponsored account, exactly where it would have been.
    //
    // Settlement is lazy, on the sponsored account's next touch -- the same
    // cadence at which Buck accrues everything else.  Two consequences worth
    // stating plainly:
    //
    //   - Between touches the payer's own feeOwing does not yet include its
    //     sponsees' pending rectangles, because finding them would mean
    //     enumerating sponsees.  `settleDemurrage(account)` is a
    //     permissionless poke that forces the transfer, so a payer (or an
    //     indexer) can bring its books current whenever it wants.
    //   - A payer that spends itself down before settlement absorbs less
    //     than it would have, and the shortfall stays with the sponsored
    //     account.  So a delegation is best-effort, and its failure mode is
    //     exactly "no delegation at all" -- never a loss to anyone else, and
    //     never demurrage that goes uncollected.
    //
    // No spend decision is ever made on a stale number: every balance-moving
    // path crystallises the account it is about to debit, immediately before
    // reading its balance.

    /// @notice The account that carries `a`'s demurrage, once both sides have
    ///         consented.  Zero when `a` pays its own.
    mapping(address => address) public demurragePayer;

    /// @notice Pending election: `a` has named this account, which has not
    ///         yet accepted.  Cleared on accept.
    mapping(address => address) public demurragePayerRequest;

    /// @notice How many accounts name `a` as their demurrage payer.  Touched
    ///         only when a delegation is armed or released, so it lives here
    ///         rather than in the packed flags word: a non-zero count is what
    ///         makes `a` a payer, which is a question only those two cold
    ///         paths ever ask.  No second flag bit is needed for it.
    mapping(address => uint32) public sponseeCount;

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
    event JubileeRedeemed(address indexed account, uint256 relief);
    /// @notice A fee taken out of circulation at `account`: the demurrage
    ///         locked in its balance when it spent past its held BUCK into
    ///         credit, or the fee aged BUCK carried into its lien.  Sums to
    ///         `feesRealized`.
    event FeeRealized(address indexed account, uint256 fee);
    event DemurragePayerRequested(address indexed account, address indexed payer);
    event DemurragePayerSet(address indexed account, address indexed payer);
    event DemurragePayerCleared(address indexed account, address indexed payer);

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
    ///         headroom* (creditLimit - used) for non-Carrying accounts.
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
            uint256 fee = _feeOwing(a, rawU);
            held = fee >= rawU ? 0 : rawU - fee;
        }
        uint256 used = raw < 0 ? uint256(-raw) : 0;
        uint256 limit = creditLimit(a);
        uint256 unusedCredit = limit > used ? limit - used : 0;
        return held + unusedCredit;
    }

    /// @notice Signed view of an account's balance after demurrage.  Returns
    ///         the negative raw value directly for used-credit accounts
    ///         (those that have spent into BuckCredit-backed headroom).  Does
    ///         NOT include credit headroom -- use `balanceOf` for the
    ///         ERC-20 visible "what can I spend" semantic.
    function signedBalanceOf(address a) public view returns (int256) {
        AccountState storage s = _state[a];
        int256 raw = s.balance.asInt();
        if (raw <= 0) return raw;          // credit used accrues no demurrage (clamped in _feeOwing)
        if (identity.isCarrying(a)) return raw;
        uint256 fee = _feeOwing(a, uint256(raw));
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

    /// @notice Live credit limit (NFT-backed BUCK headroom) for account `a`:
    ///         the sum of the depreciated activated values of the BuckCredits
    ///         `a` holds, scaled by the current PID multiplier.
    ///
    ///         `totalCurrentValue(a) * currentBuckK / BUCKK_SCALE`
    ///
    /// @dev    Read live, every time, deliberately.  This number is supposed
    ///         to move: it rises when the holder acquires or activates more
    ///         credit, and falls as their insured assets depreciate on
    ///         schedule, are reappraised, or as BUCK_K moves under them.
    ///         `balanceOf` is built on it, so a stale answer here is a wrong
    ///         balance, and there is no invalidation signal that covers all
    ///         three inputs -- BUCK_K in particular changes inside any mint or
    ///         burn that advances the PID, from any account, with nothing to
    ///         announce it.
    ///
    ///         The cost is real and lands where it should: an account holding
    ///         BuckCredit NFTs pays a scan of its own credits on every
    ///         outbound transfer, proportional to how many it holds.  An
    ///         account with no credits pays one external call that returns
    ///         zero, and a Carrying account never reaches here at all.
    function creditLimit(address a) public view returns (uint256) {
        uint256 cv = buckCredit.totalCurrentValue(a);
        if (cv == 0) return 0;
        return cv * buckK.currentBuckK() / BUCKK_SCALE;
    }

    // ---- delegated demurrage (fee payer) API -------------------------------

    /// @notice True if `a`'s demurrage is being carried by another account.
    function isSponsored(address a) external view returns (bool) {
        return _state[a].flags & FLAG_SPONSORED != 0;
    }

    /// @notice Fold `a`'s elapsed (balance * dt) rectangle into stored state
    ///         now, rather than waiting for its next transfer.  For a
    ///         sponsored account this is what hands the exposure to its payer,
    ///         so a payer can keep its own books current instead of waiting on
    ///         its sponsees to move BUCK.
    /// @dev    Permissionless and idempotent within a block: it grants no
    ///         capability that an ordinary transfer does not already exercise,
    ///         and it moves no value.
    function settleDemurrage(address a) external nonReentrant {
        _accrueJubilee();
        _crystallize(a);
    }

    /// @notice Step 1 of 2: name `payer` as the account you want to carry your
    ///         demurrage.  Takes effect only once `payer` accepts.
    /// @dev    Pass address(0) to withdraw a pending request.
    function requestDemurragePayer(address payer) external nonReentrant {
        require(payer != msg.sender, "BUCK: self payer");
        demurragePayerRequest[msg.sender] = payer;
        emit DemurragePayerRequested(msg.sender, payer);
    }

    /// @notice Step 2 of 2: accept liability for `account`'s demurrage.
    /// @dev    Both sides must consent.  The payer's consent is what keeps
    ///         this from being an attack -- unilateral delegation would let
    ///         anyone dump unbounded fee exposure onto any balance.  The
    ///         sponsored side's consent keeps a third party from silently
    ///         changing how a contract's balance behaves.
    ///
    ///         Constraints, and why:
    ///           - Both parties verified: the feature exists to let one
    ///             Identity's accounts pool their exposure; it stays inside
    ///             the identity system.
    ///           - Neither party Carrying: a Carrying account's balanceOf
    ///             ignores its fee entirely (it hands its age basis to
    ///             recipients instead), so a Carrying payer's lien would not
    ///             bite -- that is precisely the escape hatch this design
    ///             exists to avoid.
    ///           - No chains: a payer may not itself be sponsored, and a
    ///             sponsored account may not be a payer.  Routing is one hop
    ///             by construction, so chains would not recurse, but they
    ///             make "who is actually paying" unanswerable by inspection.
    function acceptDemurragePayer(address account) external nonReentrant {
        require(demurragePayerRequest[account] == msg.sender, "BUCK: not requested");
        require(account != msg.sender,                        "BUCK: self payer");
        require(identity.isVerified(account),                 "BUCK: account not verified");
        require(identity.isVerified(msg.sender),              "BUCK: payer not verified");
        require(!identity.isCarrying(account),                "BUCK: account is Carrying");
        require(!identity.isCarrying(msg.sender),             "BUCK: payer is Carrying");

        // Everything accrued so far stays where it accrued.
        _accrueJubilee();
        _crystallize(account);
        _crystallize(msg.sender);

        AccountState memory as_ = _state[account];
        require(as_.flags & FLAG_SPONSORED == 0, "BUCK: already sponsored");
        require(sponseeCount[account] == 0,      "BUCK: account is a payer");
        require(_state[msg.sender].flags & FLAG_SPONSORED == 0, "BUCK: payer is sponsored");

        as_.flags |= FLAG_SPONSORED;
        _state[account] = as_;
        sponseeCount[msg.sender] += 1;

        demurragePayer[account]        = msg.sender;
        demurragePayerRequest[account] = address(0);
        emit DemurragePayerSet(account, msg.sender);
    }

    /// @notice End a delegation.  Callable by either side -- the sponsored
    ///         account may always walk away, and a payer may always stop the
    ///         bleeding.
    function clearDemurragePayer(address account) external nonReentrant {
        address p = demurragePayer[account];
        require(p != address(0), "BUCK: not sponsored");
        require(msg.sender == account || msg.sender == p, "BUCK: not a party");

        // Crystallise first, so the rectangle accrued under the delegation
        // lands on the payer rather than snapping back onto `account`.
        _accrueJubilee();
        _crystallize(account);

        AccountState memory as_ = _state[account];
        as_.flags &= ~FLAG_SPONSORED;
        _state[account] = as_;

        uint32 n = sponseeCount[p];
        if (n != 0) sponseeCount[p] = n - 1;

        demurragePayer[account] = address(0);
        emit DemurragePayerCleared(account, p);
    }

    // ---- mint / burn -------------------------------------------------------

    function mint(uint256 amount) external nonReentrant {
        _mintAllocated(amount, _selectCheapest(msg.sender));
    }

    function mint(uint256 amount, uint256[] calldata tokenIds) external nonReentrant {
        _mintAllocated(amount, tokenIds);
    }

    /// @notice Burn `amount` BUCK.  Coverage is unwound most-expensive-first
    ///         so the dearest insurance is released first, returning the
    ///         largest pool principal per BUCK burned and freeing expensive
    ///         capacity for re-use.
    function burn(uint256 amount) external nonReentrant {
        _burnAllocated(amount, _selectMostExpensive(msg.sender));
    }

    function burn(uint256 amount, uint256[] calldata tokenIds) external nonReentrant {
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

    /// @dev Mint: activate coverage and pay its deposit, in one step.  For
    ///      each NFT in `tokenIds` (cheapest premium first), `_allocateMint`
    ///      activates the present value V_i that settles its share of
    ///      `amount` net of the insurance deposit, per the inversion
    ///      V = ceil(net * BP / (BP - premiumRate * POOL_ROI_INV)), with
    ///      principal_i = V_i - net_i; it calls `activateFromBuck` for the face
    ///      units carrying V_i and adds them to `mintsBacked[tid]` (M).
    ///
    ///      The deposit, poolPrincipal = sum principal_i, moves from the
    ///      minter to the insurance pool as a draw like any other (`_debit`):
    ///      past the minter's held BUCK it is drawn on credit.
    ///
    ///      End state, for a minter that held no BUCK, at one K:
    ///          coverage activated = sum V_i = amount + poolPrincipal
    ///          creditLimit rises by K x sum V_i (at the credits' present value)
    ///          signedRawBalance  = -poolPrincipal
    ///          balanceOf         = K x (amount + poolPrincipal) - poolPrincipal
    ///      which is `amount` exactly when K = 1.  `amount` is the coverage to
    ///      place, net of the deposit; the spendable it yields scales with K.
    function _mintAllocated(uint256 amount, uint256[] memory tokenIds) internal {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");

        // PID cadence: compute() advances the PID if dT elapsed (cheap cached
        // read otherwise).  totalCreditValue and currentBuckK are captured
        // for the Minted event; the live credit-limit is recomputed below
        // after _allocateMint has run activateFromBuck.
        uint256 totalCreditValue = buckCredit.totalCurrentValue(msg.sender);
        uint256 currentBuckK     = buckK.compute();

        // Counter-cyclical funding-factor reserve, captured against the
        // minter's PRE-activation balanceOf (held + unused credit) so the
        // credit this mint is about to activate cannot itself satisfy the
        // reserve.  The reserve is scaled to the *insurance principal* this
        // mint pays into the pool -- NOT the gross BUCK minted:
        //     balanceOf(minter) >= poolPrincipal * fundingFactor / 1e18
        // poolPrincipal is the premium-funding overhead (sum of take_i -
        // amount_i).  A zero-premium credit yields poolPrincipal == 0 --
        // zero-cost "insurance" -- so the requirement is zero and the mint is
        // exempt (the SimLP bootstrap and any uninsured pledge mint freely).
        // Where it bites: you cannot bootstrap *insured* credit from an
        // inadequate reserve while BUCK is undervalued (factor > 1).  Static
        // controller returns factor 0 (gate off); the PID controller drives
        // the factor up when BUCK is undervalued and back to ~1.0 at parity.
        // Captured before _allocateMint because that activates the pledged
        // credit; enforced after, once poolPrincipal is known.
        uint256 factor            = buckK.fundingFactor();
        uint256 preFundingBalance = factor > 0 ? balanceOf(msg.sender) : 0;

        // Walk the holder's NFTs cheapest-first; activates `take_i` on each
        // via BuckCredit.activateFromBuck, writes mintsBacked[tid] += take_i,
        // returns aggregate (totalCoverage = sum take, poolPrincipal = sum
        // principal).  The cap per NFT is `faceValue - mintsBacked` (auto-
        // activation may walk into unactivated capacity).
        (uint256 totalCoverage, uint256 poolPrincipal) = _allocateMint(amount, tokenIds);

        // Reserve scales with the insurance principal; poolPrincipal == 0
        // (zero-cost insurance) => zero requirement => exempt.
        if (factor > 0 && poolPrincipal > 0) {
            uint256 required = poolPrincipal * factor / BUCKK_SCALE;
            require(preFundingBalance >= required,
                    "BUCK: insufficient mint funding");
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
            // A draw like any other: past the minter's held BUCK, its fee is
            // paid first and the lien is exactly the credit drawn.
            _debit(msg.sender, poolPrincipal);
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

    /// @dev Burn flow: symmetric to mint -- deactivates and closes NFT-
    ///      backed credit positions.  For each NFT in `tokenIds` (most-
    ///      expensive-first by default), _allocateBurn computes the
    ///      unwind take per NFT, decrements mintsBacked, and calls
    ///      BuckCredit.deactivateFromBuck(tid, holder, unwind_i) so the
    ///      activatedValue (and thus creditLimit) shrinks in lockstep.
    ///      Insurance pool's raw shrinks by `poolRefund` (= sum
    ///      principal); holder's signed raw grows by `poolRefund`.
    ///
    ///      Solvency check: after the burn, the holder's lien (net of the
    ///      refund and any relief paid) must still fit under the shrunken
    ///      credit limit.  To release coverage that backs a lien, repay
    ///      first.
    ///
    ///      End-state arithmetic:
    ///          activatedValue, mintsBacked   -= sum unwind_i  (per NFT)
    ///          creditLimit                   -= sum unwind_i * buckK / 1e18
    ///          signedRawBalance              += poolRefund
    ///          balanceOf change              = -(sum amount_i)
    ///                                          (the user's "spendable"
    ///                                          shrinks by exactly amount)
    function _burnAllocated(uint256 amount, uint256[] memory tokenIds) internal {
        require(identity.isVerified(msg.sender), "BUCK: sender not verified");
        // Burn activity amortizes the PID; the K value isn't consumed here.
        buckK.compute();

        (uint256 totalUnwind, uint256 poolRefund) = _allocateBurn(amount, tokenIds);

        _accrueJubilee();
        if (poolRefund > 0) {
            int256 poolRaw = _state[insurancePool].balance.asInt();
            require(int256(poolRefund) <= poolRaw, "BUCK: pool underfunded");
            // The refund leaves the pool as any transfer from it would: a
            // Carrying pool (as the registry binds it) hands the refunded
            // share of its accrued age back to the holder.
            if (identity.isCarrying(insurancePool)) {
                _carryingTransfer(insurancePool, msg.sender, poolRefund);
            } else {
                _crystallize(insurancePool);
                _subBalance(insurancePool, poolRefund);
                _crystallize(msg.sender);
                _credit(msg.sender, poolRefund, 0);
            }
            // Per-side Transfer event: pool -> holder for the refund.
            emit Transfer(insurancePool, address(0), poolRefund);
        }

        // Jubilee settlement: the relief accrued on the holder's lien (its
        // issuance-seconds, ~2%/yr of the BUCK it put into circulation) pays
        // out of the fund and shrinks the lien, before the solvency check
        // reads it.  A holder already at or above zero was paid its relief
        // when it crossed (see `_credit`).
        _crystallize(msg.sender);
        _realizeRelief(msg.sender);

        // Post-burn solvency: the holder's used credit must not exceed their
        // shrunken creditLimit.  Computed after settlement so signed raw
        // already reflects the refund (climb toward zero), and read live so
        // it reflects the coverage just deactivated.
        //
        // A holder who is already at their limit cannot burn: releasing
        // coverage costs more limit than the refund repays.  That is the
        // intended shape -- you repay the credit, then release the coverage,
        // as with any loan.  Doing nothing is also a supported outcome: the
        // insurance is paid up in perpetuity and stays in force, and the
        // Jubilee relief accruing on the lien shrinks what closing it costs,
        // year on year, without the holder doing anything.
        int256 signedRaw = signedBalanceOf(msg.sender);
        uint256 used     = signedRaw < 0 ? uint256(-signedRaw) : 0;
        require(used <= creditLimit(msg.sender),
                "BUCK: post-burn credit used exceeds limit");

        // Silence the unused-variable warning while keeping the metric
        // available for future event emission.
        totalUnwind;
    }

    // ---- mint/burn allocator (per-NFT cheapest-first inversion) ------------

    /// @dev Walk `tokenIds` cheapest-first and allocate enough coverage to
    ///      deliver `amount` net to msg.sender.  Per-NFT inversion:
    ///         take = ceil(remaining * BP / (BP - rate * POOL_ROI_INV)).
    ///      Writes mintsBacked.  Returns (totalCoverage, poolPrincipal).
    /// @dev The per-credit inversion, shared by all four allocator paths so
    ///      they cannot drift apart.  Everything here happens in *present
    ///      insured value*; face units are only the denomination the credit
    ///      records coverage in.
    ///
    ///      A credit's `activatedValue` is a slice of the asset *as appraised
    ///      at issue*.  What is actually insured today is that slice scaled
    ///      by `rho = depFace / face`, and that -- not the face slice -- is
    ///      what the premium is charged on: you pay for the cover you have,
    ///      not for the cover the asset used to be worth.  So:
    ///
    ///          V = ceil(net * BP / denom)      present value that settles `net`
    ///          principal = V - net            = V * effRate / BP
    ///          units = ceil(V * face / depFace)   face units that carry V
    ///
    ///      Charging on present value is also the only formulation that does
    ///      not fall over.  Charging on the face slice instead gives
    ///      `units = net * BP / (rho * BP - effRate)`, which is unsatisfiable
    ///      once `rho * BP <= effRate` -- a 200bp credit would become
    ///      unmintable at any price below 20 % of face, because depreciation
    ///      had eaten the premium margin.  Here `denom` is independent of
    ///      `rho`, so any credit with a non-zero appraisal still works, and
    ///      the cost per BUCK delivered stays `net * effRate / denom` no
    ///      matter how old the asset is -- which is why cheapest-first by
    ///      `premiumRate` remains the right selector.
    ///
    ///      When `depFace == face` this reduces to `units = V` exactly, so
    ///      non-depreciating credits behave precisely as before.
    ///
    ///      Precondition: `depFace > 0`.  A credit appraised at zero insures
    ///      nothing, so both callers skip it before reaching here.
    ///
    /// @param net      spendable still to be placed (mint) or released (burn)
    /// @param capUnits face-denominated coverage this credit has available:
    ///                 `faceValue - mintsBacked` drawing, the outstanding
    ///                 backing unwinding
    /// @return units     face units to activate / deactivate
    /// @return principal pool principal to pay / refund
    /// @return settled   how much of `net` this credit accounts for
    function _drawSlice(
        uint256 net,
        uint256 capUnits,
        uint256 face,
        uint256 depFace,
        uint256 denom
    ) internal pure returns (uint256 units, uint256 principal, uint256 settled) {
        // Bounds: face, depFace, capUnits and net are all <= MAX_BALANCE
        // (~6.04e23), so every product below stays far inside uint256.
        uint256 capV   = capUnits * depFace / face;   // present value available
        uint256 netCap = capV * denom / BP;           // net spendable it settles

        if (netCap >= net) {
            uint256 v = (net * BP + denom - 1) / denom;
            if (v >= capV) {
                // Rounding can push v one unit past the capacity it was
                // derived from; take the whole credit rather than over-ask.
                units = capUnits;
                v     = capV;
            } else {
                units = (v * face + depFace - 1) / depFace;
                if (units > capUnits) units = capUnits;
            }
            principal = v - net;
            settled   = net;
        } else {
            units     = capUnits;
            principal = capV - netCap;
            settled   = netCap;
        }
    }

    /// @dev The release side, and deliberately NOT the mirror of `_drawSlice`.
    ///
    ///      Drawing prices coverage: how much deposit does this much cover
    ///      cost.  Releasing does not re-price anything -- it hands back the
    ///      deposit the coverage is carrying, pro rata on the face units let
    ///      go.  The two differ the moment the appraisal moves, and the
    ///      difference is the whole point: a holder who bought cover at one
    ///      appraisal and releases it at a lower one gets their whole deposit
    ///      back, not the fraction the shrunken cover would cost today.
    ///
    ///      Re-pricing on the way out would charge them twice for the same
    ///      depreciation.  The pool has already been compensated for holding
    ///      an over-sized deposit against shrinking cover: it earned its
    ///      assumed ROI on the full deposit the whole time while owing
    ///      premium only on what was still insured.  Keeping the surplus
    ///      principal as well would be helping itself twice from one decline.
    ///
    ///      So the net spendable a credit can release is its present cover
    ///      less the deposit that comes back with it:
    ///
    ///          capV   = backedUnits * depFace / face
    ///          netCap = capV - deposit
    ///          units  = ceil(net * backedUnits / netCap)
    ///          refund = deposit * units / backedUnits
    ///
    ///      `capV <= deposit` means the position has no spendable left in it
    ///      -- the holder is underwater and must repay before releasing, the
    ///      same rule the post-burn solvency check enforces.  The credit
    ///      settles nothing and the caller moves on.
    ///
    ///      Note what falls out when the holder holds no loose BUCK:
    ///      `netCap == creditLimit - used == balanceOf`, so burning exactly
    ///      their spendable closes the position and squares the deposit.
    ///
    /// @param backedUnits face units outstanding on this credit
    /// @param deposit     pool principal held against them
    function _releaseSlice(
        uint256 net,
        uint256 backedUnits,
        uint256 face,
        uint256 depFace,
        uint256 deposit
    ) internal pure returns (uint256 units, uint256 refund, uint256 settled) {
        uint256 capV = backedUnits * depFace / face;
        if (capV <= deposit) return (0, 0, 0);
        uint256 netCap = capV - deposit;

        if (netCap >= net) {
            units   = (net * backedUnits + netCap - 1) / netCap;
            if (units > backedUnits) units = backedUnits;
            settled = net;
        } else {
            units   = backedUnits;
            settled = netCap;
        }
        refund = deposit * units / backedUnits;
    }

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
            // A credit appraised at zero insures nothing, so it can carry no
            // coverage and no premium.  Skip it rather than activate face
            // units that would deliver no headroom.
            if (s.depreciatedFace == 0) continue;

            (uint256 take, uint256 principal_i, uint256 settled) = _drawSlice(
                remaining, s.faceValue - used, s.faceValue, s.depreciatedFace,
                BP - effRate
            );
            remaining -= settled;
            mintsBacked[tid]   = used + take;
            mintsPrincipal[tid] += principal_i;   // returnable deposit
            // Activate `take` more coverage on this NFT -- but only the
            // delta needed to satisfy `activatedValue >= mintsBacked`.
            // In production (no public BuckCredit.activate()), the
            // invariant `mintsBacked == activatedValue` holds at every
            // observation point, so the delta is always `take`.  In
            // test harnesses (BuckCreditHarness.forceActivate), the
            // holder may have pre-activated past mintsBacked, in which
            // case the existing activatedValue covers the new
            // mintsBacked = used + take and this call is a no-op
            // (delta = 0).  Either way the post-mint invariant
            // `mintsBacked <= activatedValue <= faceValue` holds.
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
            if (s.depreciatedFace == 0) continue;      // mirrors _allocateMint
            (uint256 take, uint256 principal_i, uint256 settled) = _drawSlice(
                remaining, s.faceValue - used, s.faceValue, s.depreciatedFace,
                BP - effRate
            );
            remaining     -= settled;
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
            // `mintsBacked` and `activatedValue` are equal by construction (M)
            // -- activation happens only in _allocateMint, and updateCredit
            // may not reappraise below activated coverage.  Take the lesser
            // anyway: an unwind larger than the coverage on the token reverts
            // inside deactivateFromBuck, and a burn that reverts is a holder
            // who cannot close a position.  Whatever future edit puts these
            // two out of step should cost the system a rounding, not the
            // holder their exit.
            uint256 used = mintsBacked[tid];
            if (s.activatedValue < used) used = s.activatedValue;
            // Silently skip fully-unused or over-rate NFTs rather than reverting: a reappraisal
            // that pushes premiumRate above the pool-ROI threshold must not strand a burn.
            if (used == 0 || effRate >= BP || s.depreciatedFace == 0) continue;

            (uint256 unwind, uint256 refund_i, uint256 settled) = _releaseSlice(
                remaining, used, s.faceValue, s.depreciatedFace, mintsPrincipal[tid]
            );
            if (unwind == 0) continue;              // nothing left to release here
            remaining           -= settled;
            mintsBacked[tid]    -= unwind;
            mintsPrincipal[tid] -= refund_i;
            // Deactivate exactly `unwind` coverage on this NFT.  Mirror
            // of the activateFromBuck call in _allocateMint.  Burning
            // is THE deactivation; it shrinks activatedValue (and thus
            // creditLimit) in lockstep with mintsBacked and refunds the
            // proportional pool principal.  (Relief is not the coverage's:
            // it accrues on the holder's lien, in this contract.)
            buckCredit.deactivateFromBuck(tid, msg.sender, unwind);
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
            CreditSlice memory s = slices[i];
            uint256 effRate = uint256(s.premiumRate) * POOL_ROI_INV;
            uint256 used = mintsBacked[tid];
            if (s.activatedValue < used) used = s.activatedValue;
            // mirrors the _allocateBurn skips, not a revert
            if (used == 0 || effRate >= BP || s.depreciatedFace == 0) continue;
            (uint256 unwind, uint256 refund_i, uint256 settled) = _releaseSlice(
                remaining, used, s.faceValue, s.depreciatedFace, mintsPrincipal[tid]
            );
            if (unwind == 0) continue;
            remaining   -= settled;
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
        // A draw (past the sender's held BUCK) or a repayment (into an
        // account below zero) moves the BUCK issued, which the fund accrues
        // on: checkpoint it first, at the rate that held until now.
        if (value > _heldOf(from) || _state[to].balance.asInt() < 0) _accrueJubilee();
        _debit(from, value);
        _credit(to, value, 0);     // the sender keeps its fee: nothing rides out
    }

    /// @dev Carrying transfer: proportionally apportions the sender's live
    ///      buckSeconds (crystallised + current rectangle) to the recipient.
    ///      Carrying accounts hold no NFT-backed credit (creditLimit == 0)
    ///      and cannot go negative; the `value <= raw` assertion enforces
    ///      this.  The recipient side is `_credit`: a holder takes the age
    ///      in, an account below zero pays the fee on arrival.
    function _carryingTransfer(address from, address to, uint256 value) internal {
        // A repayment moves the BUCK issued: checkpoint the fund before any
        // supply write (the sender's comes first).
        if (_state[to].balance.asInt() < 0) _accrueJubilee();

        // ---- from ----
        AccountState memory fs = _state[from];
        int256 rawSigned = fs.balance.asInt();
        require(rawSigned >= int256(value), "BUCK: Carrying amount exceeds raw");
        uint256 raw = uint256(rawSigned);

        uint256 elapsed = block.timestamp - uint256(fs.timestamp);
        uint256 liveBs  = fs.buckSeconds.asUint() + raw * elapsed;
        uint256 carried = raw > 0 ? liveBs * value / raw : 0;

        // Carrying accounts only ever hold raw >= 0, so the from-side delta
        // is a simple unsigned subtraction.
        uint256 newFromRaw = raw - value;
        fs.balance     = toBuckQtySigned(int256(newFromRaw));
        fs.buckSeconds = toBuckSeconds(liveBs - carried);
        fs.timestamp   = uint40(block.timestamp);
        _state[from] = fs;
        _totalSupply -= value;     // from's positive contribution dropped by value

        // ---- to ----
        _crystallize(to);
        _credit(to, value, carried);
    }

    // ---- the two sides of a balance change ---------------------------------
    //
    // Every BUCK carries its fee to the end; every issuer earns back the fee
    // its issuance collected.  A fee is a lien inside a positive balance and
    // never moves -- until the BUCK carrying it would shed it.  Then it is
    // realized: taken out of circulation.  That happens in exactly two
    // places: a spend that reaches past the held BUCK into credit (`_debit`),
    // and aged BUCK arriving at an account below zero (`_credit`).
    //
    // With the fee realized at zero, an account below zero never holds
    // fee-seconds, so while negative its `buckSeconds` counts issuance-seconds
    // instead (see `_crystallize`): the lien integrated over time, on which
    // relief accrues.  Read by sign: fee-seconds above zero, issuance-seconds
    // below.

    /// @dev Held BUCK: the positive balance less the fee locked in it.
    ///      Caller has crystallised `a`.
    function _heldOf(address a) internal view returns (uint256) {
        int256 raw = _state[a].balance.asInt();
        if (raw <= 0) return 0;
        uint256 fee = Math.mulDiv(_state[a].buckSeconds.asUint(), BASE_RATE_PER_SEC, SCALE);
        return fee >= uint256(raw) ? 0 : uint256(raw) - fee;
    }

    /// @dev Debit `value` from non-Carrying `a`.  Caller has crystallised
    ///      `a`, checked the spend against balanceOf, and -- if the spend
    ///      reaches into credit -- checkpointed the fund.
    ///
    ///      Spent from held BUCK (value <= raw - fee), the fee stays locked in
    ///      what remains.  Spent past them, the fee is paid first (realized:
    ///      out of circulation) and the rest is drawn, so the resulting lien
    ///      is exactly the credit drawn, value - (raw - fee), and the seconds
    ///      restart at 0 as issuance-seconds.
    function _debit(address a, uint256 value) internal {
        if (value == 0) return;
        AccountState memory s = _state[a];
        int256 old = s.balance.asInt();
        if (old > 0) {
            uint256 raw = uint256(old);
            uint256 fee = Math.mulDiv(s.buckSeconds.asUint(), BASE_RATE_PER_SEC, SCALE);
            if (fee > raw) fee = raw;
            if (value <= raw - fee) {
                s.balance = toBuckQtySigned(old - int256(value));
                // I1.  Spending down to exactly zero means fee == 0 here: the
                // seconds left are dust (under one raw unit of fee).
                if (value == raw) s.buckSeconds = toBuckSeconds(0);
                _state[a] = s;
                _totalSupply -= value;
                return;
            }
            if (fee != 0) {
                feesRealized += fee;
                emit FeeRealized(a, fee);
            }
            s.buckSeconds = toBuckSeconds(0);
            s.balance     = toBuckQtySigned(old - int256(fee) - int256(value));
            _state[a] = s;
            _totalSupply -= raw;       // the whole positive contribution goes
            return;
        }
        // Already at or below zero: deeper into credit.  The seconds are
        // issuance-seconds (or 0 at zero, by I1) and keep counting.
        s.balance = toBuckQtySigned(old - int256(value));
        _state[a] = s;
    }

    /// @dev Credit `value` BUCK carrying `carriedBs` of age to `a`.  Caller
    ///      has crystallised `a` and, if `a` is below zero, checkpointed the
    ///      fund.
    ///
    ///      A holder (at or above zero: no lien) takes the BUCK and their age
    ///      in, as ever: the age routes to its payer if it has one.
    ///
    ///      Below zero the BUCK repay a lien, and they pay their fee on
    ///      arrival: they repay `value - fee`, the fee realized (an account
    ///      below zero holds issuance-seconds, not fee-seconds, so the age
    ///      cannot ride in).  If the receipt repays the whole lien, the
    ///      relief accrued on it pays out with it, and the account starts at
    ///      or above zero with no seconds (I1 at exactly zero).
    function _credit(address a, uint256 value, uint256 carriedBs) internal {
        if (value == 0) return;
        AccountState memory s = _state[a];
        int256 old = s.balance.asInt();
        if (old >= 0) {
            // A holder: fee-seconds (0 at zero, by I1) plus the carried age.
            if (carriedBs != 0) {
                if (s.flags & FLAG_SPONSORED != 0) carriedBs = _routeToPayer(a, carriedBs);
                s.buckSeconds = toBuckSeconds(s.buckSeconds.asUint() + carriedBs);
            }
            s.balance = toBuckQtySigned(old + int256(value));
            _state[a] = s;
            _totalSupply += value;
            return;
        }
        // Below zero: the seconds are issuance-seconds.
        uint256 fee = carriedBs == 0 ? 0 : Math.mulDiv(carriedBs, BASE_RATE_PER_SEC, SCALE);
        if (fee > value) fee = value;
        if (fee != 0) {
            feesRealized += fee;
            emit FeeRealized(a, fee);
        }
        int256 nw = old + int256(value - fee);
        uint256 bs = s.buckSeconds.asUint();                 // issuance-seconds
        if (nw >= 0) {
            nw += int256(_payRelief(a, bs, uint256(-old)));
            bs = 0;
        }
        s.balance     = toBuckQtySigned(nw);
        s.buckSeconds = toBuckSeconds(bs);
        _state[a] = s;
        if (nw > 0) _totalSupply += uint256(nw);
    }

    /// @dev Pay `a` the relief on `bs` issuance-seconds, capped at the lien
    ///      being relieved and at what the fund holds.  Moves fund BUCK to
    ///      the caller's books (the caller credits `a`); the fund side is a
    ///      direct slot write, as its accrual is, so both sides of
    ///          sum_a max(0, signedRaw(a)) == totalSupply + jubileeActual
    ///      move together.
    function _payRelief(address a, uint256 bs, uint256 lien) internal returns (uint256 relief) {
        if (bs == 0 || lien == 0) return 0;
        relief = Math.mulDiv(bs, BASE_RATE_PER_SEC, SCALE);
        if (relief > lien) relief = lien;
        _crystallize(address(this));
        AccountState memory js = _state[address(this)];
        int256 jubRaw = js.balance.asInt();
        uint256 avail = jubRaw > 0 ? uint256(jubRaw) : 0;
        if (relief > avail) relief = avail;
        if (relief == 0) return 0;
        js.balance = toBuckQtySigned(jubRaw - int256(relief));
        _state[address(this)] = js;
        reliefRealized += relief;
        emit JubileeRedeemed(a, relief);
    }

    /// @dev Realize the relief accrued on `a`'s lien without waiting for it
    ///      to be repaid: the lien shrinks by it.  Caller has crystallised
    ///      `a` and checkpointed the fund.  The issuance-seconds it pays for
    ///      are consumed; any the fund could not pay stay, to be paid later.
    function _realizeRelief(address a) internal {
        AccountState memory s = _state[a];
        int256 raw = s.balance.asInt();
        if (raw >= 0) return;
        uint256 lien = uint256(-raw);
        uint256 bs   = s.buckSeconds.asUint();
        uint256 relief = _payRelief(a, bs, lien);
        if (relief == 0) return;
        if (relief == lien) {
            bs = 0;                    // relieved in full: the cap forfeits the rest
        } else {
            uint256 used = Math.mulDiv(relief, SCALE, BASE_RATE_PER_SEC, Math.Rounding.Ceil);
            bs = used >= bs ? 0 : bs - used;
        }
        s.balance     = toBuckQtySigned(raw + int256(relief));
        s.buckSeconds = toBuckSeconds(bs);
        _state[a] = s;
    }

    // ---- demurrage views ---------------------------------------------------

    function feeOwing(address a) public view returns (uint256) {
        int256 raw = _state[a].balance.asInt();
        if (raw <= 0) return 0;             // no demurrage on used credit or empty
        return _feeOwing(a, uint256(raw));
    }

    function balanceOfFees(address a) public view returns (uint256) {
        uint256 fee = feeOwing(a);
        if (identity.isCarrying(a)) return fee;
        int256 raw = _state[a].balance.asInt();
        if (raw <= 0) return 0;
        uint256 rawU = uint256(raw);
        return fee >= rawU ? rawU : fee;
    }

    /// @notice Unsigned raw balance.  Clamps negative (using credit) accounts to 0
    ///         so legacy ERC-20-style readers see a non-negative number;
    ///         use `signedRawBalanceOf` if you need to distinguish used credit.
    function rawBalanceOf(address a) external view returns (uint256) {
        int256 raw = _state[a].balance.asInt();
        return raw <= 0 ? 0 : uint256(raw);
    }

    /// @notice Signed raw balance (negative = NFT-backed used credit).
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

    // ---- Jubilee lien relief ------------------------------------------------
    //
    // Relief accrues on BUCK actually issued: ~2%/yr of an account's lien
    // (its negative balance), integrated in the same `buckSeconds` field that
    // counts fee-seconds while the account is positive.  Undrawn credit earns
    // nothing.  It pays out of the fund when the lien is repaid (`_credit`),
    // when the holder burns (`_burnAllocated`), or when the holder asks
    // (`settleRelief`), and is capped at the lien: a lien carried ~50 years
    // closes free.

    /// @notice Relief accrued on `a`'s lien and not yet paid, in BUCK:
    ///         what closing the lien would be discounted by.
    function reliefOf(address a) public view returns (uint256) {
        AccountState storage s = _state[a];
        int256 raw = s.balance.asInt();
        if (raw >= 0) return 0;
        uint256 lien = uint256(-raw);
        uint256 bs = s.buckSeconds.asUint() + lien * (block.timestamp - uint256(s.timestamp));
        uint256 relief = Math.mulDiv(bs, BASE_RATE_PER_SEC, SCALE);
        return relief > lien ? lien : relief;
    }

    /// @notice What closing `a`'s lien costs: the lien net of its accrued
    ///         relief.  THE liability-side quote for a credit position -- it
    ///         declines year by year while the lien is carried, and is never
    ///         called due.
    function redeemCost(address a) external view returns (uint256) {
        int256 raw = _state[a].balance.asInt();
        if (raw >= 0) return 0;
        return uint256(-raw) - reliefOf(a);
    }

    /// @notice Pay the caller the relief accrued on its lien now: the lien
    ///         shrinks by it.  Only the holder may: relief accrues linearly
    ///         on the lien, so paying it early (a smaller lien accruing
    ///         thereafter) is the holder's choice, never a third party's.
    function settleRelief() external nonReentrant {
        _accrueJubilee();
        _crystallize(msg.sender);
        _realizeRelief(msg.sender);
    }

    /// @notice The BUCK issued -- the fund's accrual base: the sum of the
    ///         liens, from the identity above.
    function totalIssued() public view virtual returns (uint256) {
        int256 base = int256(_totalSupply) - int256(reliefRealized) + int256(feesRealized);
        return base > 0 ? uint256(base) : 0;
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
    function _feeOwing(address a, uint256 raw) internal view returns (uint256) {
        AccountState storage s = _state[a];
        uint256 buckSecondsLive = s.buckSeconds.asUint();
        uint256 elapsed = block.timestamp - uint256(s.timestamp);
        if (elapsed != 0 && raw != 0) {
            uint256 delta = raw * elapsed;
            // A sponsored account's live rectangle is destined for its payer;
            // only the slice the payer has no room for stays here.  Note the
            // guard: `elapsed == 0` is the state every spend check sees --
            // `_nonCarryingTransfer` crystallises `from` immediately before
            // reading `balanceOf(from)` -- so the transfer hot path never
            // reaches the payer lookup, and unsponsored accounts never test
            // more than a mask on a word already in memory.
            if (s.flags & FLAG_SPONSORED != 0) {
                (, , uint256 take) = _payerRoom(a, delta);
                delta -= take;
            }
            buckSecondsLive += delta;
        }
        if (buckSecondsLive == 0) return 0;
        return Math.mulDiv(buckSecondsLive, BASE_RATE_PER_SEC, SCALE);
    }

    /// @dev How much of `deltaBs` buck-seconds `a`'s designated payer can take
    ///      on, and the payer's own live buck-seconds so the writing twin can
    ///      commit without recomputing.
    ///
    ///      The cap is the payer's *lien capacity*: the buck-seconds at which
    ///      feeOwing(payer) would equal rawBalance(payer).  Beyond it the fee
    ///      is uncollectible -- balanceOf clamps at zero and the surplus is
    ///      demurrage that nobody ever pays, which is the one outcome that
    ///      would make delegation a way out of demurrage rather than a way to
    ///      relocate it.  Capping here keeps a delegated account's lien no
    ///      less collectible than an undelegated one's.
    function _payerRoom(address a, uint256 deltaBs)
        internal view returns (address p, uint256 pBs, uint256 take)
    {
        if (deltaBs == 0) return (address(0), 0, 0);
        p = demurragePayer[a];
        if (p == address(0)) return (p, 0, 0);

        AccountState storage ps = _state[p];
        int256 pRawSigned = ps.balance.asInt();
        if (pRawSigned <= 0) return (p, 0, 0);   // nothing to lien against
        uint256 pRaw = uint256(pRawSigned);

        // Payer's own live integral, then its capacity ceiling.
        pBs = ps.buckSeconds.asUint()
            + pRaw * (block.timestamp - uint256(ps.timestamp));
        uint256 maxBs = pRaw * SCALE / BASE_RATE_PER_SEC;
        if (pBs >= maxBs) return (p, pBs, 0);    // payer is tapped out

        uint256 room = maxBs - pBs;
        take = deltaBs <= room ? deltaBs : room;
    }

    /// @dev Writing twin of `_payerRoom`: move as much of `deltaBs` onto the
    ///      payer as it can carry, returning the remainder that stays with
    ///      `a`.  Writing the payer's slot also crystallises the payer's own
    ///      rectangle (it is folded into `pBs`), which is exactly right --
    ///      the payer is being touched, so it settles its own clock too.
    ///
    ///      `_state[p]` is written here and `_state[a]` by the caller
    ///      afterwards, so p == a would silently discard this write;
    ///      acceptDemurragePayer forbids self-payment for that reason.
    function _routeToPayer(address a, uint256 deltaBs) internal returns (uint256) {
        (address p, uint256 pBs, uint256 take) = _payerRoom(a, deltaBs);
        if (take == 0) return deltaBs;
        AccountState memory ps = _state[p];
        ps.buckSeconds = toBuckSeconds(pBs + take);
        ps.timestamp   = uint40(block.timestamp);
        _state[p]      = ps;
        return deltaBs - take;
    }

    /// @dev Fold the elapsed (balance * dt) rectangle into buckSeconds and
    ///      bump the timestamp.  Idempotent in time: a second call within
    ///      the same block is a no-op.  No balance change.  Accounts using
    ///      credit (signed raw < 0) accrue no demurrage -- the rectangle
    ///      uses only the positive portion of the balance.
    function _crystallize(address a) internal {
        AccountState memory s = _state[a];
        int256 rawSigned = s.balance.asInt();
        uint256 elapsed  = block.timestamp - uint256(s.timestamp);
        bool dirty = false;
        if (elapsed != 0 && rawSigned != 0) {
            uint256 delta;
            if (rawSigned > 0) {
                // Fee-seconds.  Delegated demurrage: hand the rectangle to
                // this account's payer as far as the payer can carry it.
                // Conserved, never destroyed -- whatever the payer has no room
                // for stays here.  Free for the unsponsored: the flags word is
                // already in memory.
                delta = uint256(rawSigned) * elapsed;
                if (s.flags & FLAG_SPONSORED != 0) {
                    delta = _routeToPayer(a, delta);
                }
            } else {
                // Issuance-seconds: the lien over time, on which the holder's
                // relief accrues.  The holder's own; never routed to a payer.
                delta = uint256(-rawSigned) * elapsed;
            }
            if (delta != 0) {
                s.buckSeconds = toBuckSeconds(s.buckSeconds.asUint() + delta);
            }
            dirty = true;
        }
        if (uint256(s.timestamp) != block.timestamp) {
            s.timestamp = uint40(block.timestamp);
            dirty = true;
        }
        if (dirty) _state[a] = s;
    }

    /// @dev System-level Jubilee accrual.  Adds totalIssued*RATE*elapsed to
    ///      Jubilee's balance directly -- NOT a mint, totalSupply unchanged.
    ///      The fund accrues on the BUCK *issued* -- the base relief accrues
    ///      on -- not on totalSupply, which also counts BUCK whose issuer has
    ///      already been relieved of them; so it always holds the relief it
    ///      owes.  Every operation that moves the BUCK issued (a mint, a
    ///      burn, a draw, a repayment, a relief payment) calls this first, so
    ///      each period accrues at the base that held through it.
    function _accrueJubilee() internal {
        uint256 elapsed = block.timestamp - uint256(_jubileeLastUpdate);
        if (elapsed == 0) return;
        uint256 supply = totalIssued();
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
    // the payer's credit); BUCK leaves circulation when a negative-raw
    // account climbs toward zero (the used credit is being paid down).

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
        // I1.  (Only the insurance pool's paths reach here, and never below
        // zero; a non-Carrying pool paid down to zero forgives its fee.)
        if (newSigned == 0) s.buckSeconds = toBuckSeconds(0);
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
