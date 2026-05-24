// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC721/extensions/ERC721Enumerable.sol";

import {BuckTypes, BuckQty, toBuckQty} from "./BuckTypes.sol";

/// @notice Hook surface BuckCredit calls on Buck whenever an NFT mutation
///         (mint / burn / transfer / activate) changes a holder's
///         totalCurrentValue.  Buck uses it to invalidate its per-block
///         credit-limit cache for the affected holders so that subsequent
///         creditLimit() reads compute against the fresh NFT state.
interface IBuckHook {
    function onCreditMutation(address from, address to) external;
}

/// @title BuckCredit — ERC-721 Insured Asset NFT
/// @notice Each token represents an insurer's offer of parametric insurance on a
///         real-world asset, with deterministic depreciation and piecemeal activation.
///
/// When insurance is executed, it protects a certain asset valuation, and
/// each piecemeal contract covers a certain fraction of the asset (eg. 65%), up to a total
/// of the asset's current value.
///
/// When claimed, the insurance contracts each pay the insured fraction of a drop in its value from
/// the insured valuation, net deductible.
///
/// All monetary fields (faceValue, depreciationFloor, activatedValue) are
/// 6-decimal BUCK amounts and packed into uint80 slots — same storage type as
/// Buck.sol's ERC-20 balances.  Bounds and precision come from BuckTypes so a
/// future change propagates to both contracts in lockstep.

contract BuckCredit is ERC721Enumerable {

    enum DepreciationType {
        NONE,              // Non-depreciating (land, gold, crypto)
        LINEAR,            // Constant annual reduction
        DECLINING_BALANCE  // Percentage of remaining value per year
    }

    /// @dev Field declaration order is chosen to pack into 3 storage slots:
    ///      slot 0: insurer (20) + assetClass (1) + createdAt (6)            = 27
    ///      slot 1: faceValue (10) + depreciationFloor (10) + depType (1)
    ///              + depRate (4) + depStartAt (6)                           = 31
    ///      slot 2: premiumRate (4) + lastUpdated (6) + activatedValue (10)
    ///              + lastActivatedAt (6)                                    = 26
    struct CreditParams {
        // Immutable (set at creation)
        address insurer;            // Vendor who can update this credit
        uint8   assetClass;         // Asset classification (immutable)
        uint48  createdAt;          // Creation timestamp

        // Insurer-mutable (reappraisal, schedule changes)
        BuckQty faceValue;          // Maximum insured value           (uint80 BUCK, 6 decimals)
        BuckQty depreciationFloor;  // Minimum value after depreciation (uint80 BUCK, 6 decimals)

        DepreciationType depType;   // Depreciation model
        uint32  depRate;            // Annual rate in basis points (10000 = 100%)
        uint48  depStartAt;         // When depreciation begins

        uint32  premiumRate;        // Annual premium: basis points of activated value
        uint48  lastUpdated;        // Timestamp of last insurer update

        // Client-mutable (activation)
        BuckQty activatedValue;     // Currently activated portion (<= faceValue) (uint80 BUCK, 6 decimals)
        uint48  lastActivatedAt;    // Timestamp of last activation
    }

    // ── Depreciation constants ──────────────────────────────────────
    uint256 internal constant BP                = 10_000;
    uint256 internal constant SECONDS_PER_YEAR  = 365 days + 6 hours;   // matches Buck.sol
    /// Cap declining-balance compounding to bound gas; by then the value is
    /// indistinguishable from `floor` for any rate >= a few hundred bps.
    uint256 internal constant MAX_DEP_YEARS     = 100;

    mapping(uint256 => CreditParams) public credits;
    uint256 private _nextTokenId;

    /// @notice Buck contract that receives credit-mutation callbacks for
    ///         cache invalidation.  Wired one-shot post-deployment via
    ///         setBuck(...); zero-address means callbacks are skipped (so
    ///         BuckCredit can be deployed and exercised before Buck exists,
    ///         e.g. in older fixtures).
    address public buck;

    // --- Events ---
    event CreditCreated(uint256 indexed tokenId, address indexed insurer,
                        address indexed owner, uint256 faceValue);
    event CreditUpdated(uint256 indexed tokenId, address indexed insurer,
                        uint256 newFaceValue, uint32 newDepRate, uint32 newPremiumRate);
    event CreditActivated(uint256 indexed tokenId, address indexed owner,
                          uint256 additionalValue, uint256 totalActivated);
    event BuckSet(address indexed buck);

    constructor() ERC721("BuckCredit", "BUCK_CREDIT") {}

    /// @notice One-shot wiring of the Buck contract for credit-mutation
    ///         hooks.  Callable by anyone (the Buck address is public and
    ///         the function is idempotent once set), but immutable after
    ///         first set.  Mirrors Buck.setBasket(...) for the symmetric
    ///         BuckBasket wiring style.
    function setBuck(address _buck) external {
        require(buck == address(0), "BuckCredit: buck already set");
        require(_buck != address(0), "BuckCredit: buck=0");
        buck = _buck;
        emit BuckSet(_buck);
    }

    /// @dev Override the OZ ERC721 _update hook so any NFT state change
    ///      (mint / burn / transfer) invalidates Buck's per-block credit-
    ///      limit cache for both the previous and new owners.  ERC721Enumerable
    ///      itself overrides _update; we call super to preserve its
    ///      enumeration bookkeeping.
    function _update(address to, uint256 tokenId, address auth)
        internal override returns (address from)
    {
        from = super._update(to, tokenId, auth);
        address b = buck;
        if (b != address(0)) {
            IBuckHook(b).onCreditMutation(from, to);
        }
    }

    /// @notice Insurer creates a new BUCK_CREDIT NFT for a client.
    /// @dev faceValue / depreciationFloor are accepted as uint256 for ABI
    ///      ergonomics but must fit in BuckTypes.MAX_BALANCE (uint80 cap)
    ///      since they are stored alongside the BUCK supply.
    function createCredit(
        address client,
        uint8 assetClass,
        uint256 faceValue,
        uint256 depreciationFloor,
        DepreciationType depType,
        uint32 depRate,
        uint48 depStartAt,
        uint32 premiumRate
    ) external returns (uint256) {
        require(depreciationFloor <= faceValue, "floor > face");

        uint256 tokenId = _nextTokenId++;
        _mint(client, tokenId);

        credits[tokenId] = CreditParams({
            insurer: msg.sender,
            assetClass: assetClass,
            createdAt: uint48(block.timestamp),
            faceValue: toBuckQty(faceValue),
            depreciationFloor: toBuckQty(depreciationFloor),
            depType: depType,
            depRate: depRate,
            depStartAt: depStartAt,
            premiumRate: premiumRate,
            lastUpdated: uint48(block.timestamp),
            activatedValue: BuckQty.wrap(0),
            lastActivatedAt: 0
        });

        emit CreditCreated(tokenId, msg.sender, client, faceValue);
        return tokenId;
    }

    // ── Depreciation ────────────────────────────────────────────────

    /// @notice Current depreciated value of the activated portion of this credit.
    /// @dev Pure computation from on-chain state — no oracle needed.
    function currentValue(uint256 tokenId) public view returns (uint256) {
        CreditParams storage c = credits[tokenId];
        if (c.activatedValue.isZero()) return 0;

        uint256 face = c.faceValue.asUint();
        uint256 depreciatedFace = _depreciate(
            face, c.depType, c.depRate,
            c.depreciationFloor.asUint(), c.depStartAt
        );

        // Activated portion depreciates proportionally.
        return depreciatedFace * c.activatedValue.asUint() / face;
    }

    /// @dev Discrete-time depreciation.  No transcendental approximations —
    ///      DECLINING_BALANCE compounds the per-year factor (BP - rate)/BP
    ///      whole-year by whole-year, then linearly interpolates across the
    ///      trailing partial year.  Matches the convention most accounting
    ///      systems use for declining-balance schedules and avoids the
    ///      accuracy / range-reduction headaches of a fixed-point exp(-x).
    function _depreciate(
        uint256 faceValue,
        DepreciationType depType,
        uint32 depRate,        // basis points per year
        uint256 floor,
        uint48 startAt
    ) internal view returns (uint256) {
        if (depType == DepreciationType.NONE || block.timestamp <= startAt) {
            return faceValue;
        }
        if (faceValue <= floor) return floor;

        uint256 elapsed     = block.timestamp - startAt;
        uint256 depreciable = faceValue - floor;

        if (depType == DepreciationType.LINEAR) {
            uint256 loss = depreciable * uint256(depRate) * elapsed
                           / (SECONDS_PER_YEAR * BP);
            if (loss >= depreciable) return floor;
            return faceValue - loss;
        }

        if (depType == DepreciationType.DECLINING_BALANCE) {
            if (depRate == 0)        return faceValue;
            if (depRate >= BP)       return floor;

            uint256 wholeYears = elapsed / SECONDS_PER_YEAR;
            if (wholeYears >= MAX_DEP_YEARS) return floor;

            uint256 keep = BP - uint256(depRate);
            uint256 v    = depreciable;
            for (uint256 i = 0; i < wholeYears; i++) {
                v = v * keep / BP;
                if (v == 0) return floor;
            }
            // Linear interpolation across the remaining partial year:
            //   v(t) = v - (v - v_next) * fracSec / SECONDS_PER_YEAR
            uint256 fracSec = elapsed - wholeYears * SECONDS_PER_YEAR;
            if (fracSec != 0) {
                uint256 vNext = v * keep / BP;
                v = v - (v - vNext) * fracSec / SECONDS_PER_YEAR;
            }
            return floor + v;
        }

        return faceValue; // fallback
    }

    // ── Activation ──────────────────────────────────────────────────

    /// @notice Client activates additional credit, up to the current face value.
    function activate(uint256 tokenId, uint256 amount) external {
        address owner = ownerOf(tokenId);
        require(owner == msg.sender, "Not credit owner");
        _activate(tokenId, owner, amount);
    }

    /// @notice Activate `amount` of coverage on behalf of `holder`, restricted
    ///         to the registered Buck contract.  Buck calls this from
    ///         _allocateMint so a single `Buck.mint(amount, [tid])` call
    ///         expands the holder's NFT-backed credit headroom directly,
    ///         without requiring a separate `activate()` step.
    function activateFromBuck(uint256 tokenId, address holder, uint256 amount) external {
        require(msg.sender == buck && buck != address(0), "BuckCredit: not buck");
        require(ownerOf(tokenId) == holder, "BuckCredit: not holder");
        _activate(tokenId, holder, amount);
    }

    /// @notice Deactivate `amount` of coverage on behalf of `holder`, restricted
    ///         to Buck.  Mirror of activateFromBuck for the burn-side unwind.
    function deactivateFromBuck(uint256 tokenId, address holder, uint256 amount) external {
        require(msg.sender == buck && buck != address(0), "BuckCredit: not buck");
        require(ownerOf(tokenId) == holder, "BuckCredit: not holder");
        CreditParams storage c = credits[tokenId];
        uint256 current = c.activatedValue.asUint();
        require(amount <= current, "BuckCredit: deactivate > active");
        c.activatedValue  = toBuckQty(current - amount);
        c.lastActivatedAt = uint48(block.timestamp);

        IBuckHook(buck).onCreditMutation(holder, address(0));
        emit CreditActivated(tokenId, holder, 0, current - amount);
    }

    function _activate(uint256 tokenId, address holder, uint256 amount) internal {
        if (amount == 0) return;
        CreditParams storage c = credits[tokenId];
        uint256 newActivated = c.activatedValue.asUint() + amount;
        require(newActivated <= c.faceValue.asUint(), "Exceeds face value");

        c.activatedValue  = toBuckQty(newActivated);
        c.lastActivatedAt = uint48(block.timestamp);

        // activatedValue feeds totalCurrentValue(), which gates Buck's
        // credit limit -- invalidate the cache for this holder.
        address b = buck;
        if (b != address(0)) {
            IBuckHook(b).onCreditMutation(holder, address(0));
        }

        emit CreditActivated(tokenId, holder, amount, newActivated);
    }

    /// @notice Compact (faceValue, activatedValue, premiumRate) view used by
    ///         Buck.mint() to walk a holder's NFTs without unpacking the full
    ///         CreditParams tuple per token.  Returned in uint256 form for
    ///         arithmetic ergonomics on the consumer side.
    function creditInfo(uint256 tokenId)
        external view returns (uint256 faceValue, uint256 activatedValue, uint32 premiumRate)
    {
        CreditParams storage c = credits[tokenId];
        return (c.faceValue.asUint(), c.activatedValue.asUint(), c.premiumRate);
    }

    /// @notice Aggregate current value of all BuckCredits owned by an account.
    /// @dev Called by Buck.mint() to compute the credit limit.
    /// 
    /// The sum of all currently available BuckCredit insurable assets, at their
    /// present value.
    function totalCurrentValue(address account) external view returns (uint256) {
        uint256 total = 0;
        uint256 count = balanceOf(account);
        for (uint256 i = 0; i < count; i++) {
            total += currentValue(tokenOfOwnerByIndex(account, i));
        }
        return total;
    }

    // ── Insurer Updates ─────────────────────────────────────────────

    /// @notice Insurer updates credit parameters (reappraisal, schedule change).
    function updateCredit(
        uint256 tokenId,
        uint256 newFaceValue,
        uint256 newDepreciationFloor,
        DepreciationType newDepType,
        uint32 newDepRate,
        uint48 newDepStartAt,
        uint32 newPremiumRate
    ) external {
        CreditParams storage c = credits[tokenId];
        require(msg.sender == c.insurer,                "Not insurer");
        require(newDepreciationFloor <= newFaceValue,   "floor > face");

        BuckQty newFace = toBuckQty(newFaceValue);  // bound-check up front
        if (newFaceValue < c.activatedValue.asUint()) {
            c.activatedValue = newFace;
        }

        c.faceValue         = newFace;
        c.depreciationFloor = toBuckQty(newDepreciationFloor);
        c.depType           = newDepType;
        c.depRate           = newDepRate;
        c.depStartAt        = newDepStartAt;
        c.premiumRate       = newPremiumRate;
        c.lastUpdated       = uint48(block.timestamp);

        // Insurer reappraisal can change totalCurrentValue of the holder;
        // invalidate Buck's credit-limit cache for the current owner.
        address b = buck;
        if (b != address(0)) {
            IBuckHook(b).onCreditMutation(ownerOf(tokenId), address(0));
        }

        emit CreditUpdated(tokenId, msg.sender, newFaceValue, newDepRate, newPremiumRate);
    }
}
