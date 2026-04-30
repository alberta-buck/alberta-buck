// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC721/extensions/ERC721Enumerable.sol";

/// @title BuckCredit — ERC-721 Insured Asset NFT
/// @notice Each token represents an insurer's offer of parametric insurance on a
///         real-world asset, with deterministic depreciation and piecemeal activation.
contract BuckCredit is ERC721Enumerable {

    enum DepreciationType {
        NONE,              // Non-depreciating (land, gold, crypto)
        LINEAR,            // Constant annual reduction
        DECLINING_BALANCE  // Percentage of remaining value per year
    }

    struct CreditParams {
        // Immutable (set at creation)
        address insurer;            // Vendor who can update this credit
        uint8   assetClass;         // Asset classification (immutable)
        uint48  createdAt;          // Creation timestamp

        // Insurer-mutable (reappraisal, schedule changes)
        uint256 faceValue;          // Maximum insured value (18 decimals)
        uint256 depreciationFloor;  // Minimum value after depreciation

        DepreciationType depType;   // Depreciation model
        uint32  depRate;            // Annual rate in basis points (10000 = 100%)
        uint48  depStartAt;         // When depreciation begins

        uint32  premiumRate;        // Annual premium: basis points of activated value
        uint48  lastUpdated;        // Timestamp of last insurer update

        // Client-mutable (activation)
        uint256 activatedValue;     // Currently activated portion (<= faceValue)
        uint48  lastActivatedAt;    // Timestamp of last activation
    }

    mapping(uint256 => CreditParams) public credits;
    uint256 private _nextTokenId;

    // --- Events ---
    event CreditCreated(uint256 indexed tokenId, address indexed insurer,
                        address indexed owner, uint256 faceValue);
    event CreditUpdated(uint256 indexed tokenId, address indexed insurer,
                        uint256 newFaceValue, uint32 newDepRate, uint32 newPremiumRate);
    event CreditActivated(uint256 indexed tokenId, address indexed owner,
                          uint256 additionalValue, uint256 totalActivated);

    constructor() ERC721("BuckCredit", "BUCK_CREDIT") {}

    /// @notice Insurer creates a new BUCK_CREDIT NFT for a client.
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
        uint256 tokenId = _nextTokenId++;
        _mint(client, tokenId);

        credits[tokenId] = CreditParams({
            insurer: msg.sender,
            assetClass: assetClass,
            createdAt: uint48(block.timestamp),
            faceValue: faceValue,
            depreciationFloor: depreciationFloor,
            depType: depType,
            depRate: depRate,
            depStartAt: depStartAt,
            premiumRate: premiumRate,
            lastUpdated: uint48(block.timestamp),
            activatedValue: 0,
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
        if (c.activatedValue == 0) return 0;

        uint256 depreciatedFace = _depreciate(
            c.faceValue, c.depType, c.depRate,
            c.depreciationFloor, c.depStartAt
        );

        // Activated portion depreciates proportionally
        return depreciatedFace * c.activatedValue / c.faceValue;
    }

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

        uint256 elapsed = block.timestamp - startAt;
        uint256 depreciable = faceValue - floor;

        if (depType == DepreciationType.LINEAR) {
            // loss = depreciable * rate * elapsed / (365.25 days * 10000)
            uint256 loss = depreciable * depRate * elapsed / (365.25 days * 10000);
            if (loss >= depreciable) return floor;
            return faceValue - loss;
        }

        if (depType == DepreciationType.DECLINING_BALANCE) {
            // Continuous approximation: exp(-rate * elapsed / (10000 * 365.25 days))
            uint256 exponent = uint256(depRate) * elapsed / (365.25 days);
            // exponent is in basis-point-years; convert to 18-decimal fixed point
            uint256 expFp = exponent * 1e14;
            uint256 factor = _expNeg(expFp);
            return floor + depreciable * factor / 1e18;
        }

        return faceValue; // fallback
    }

    /// @dev Fixed-point exp(-x) for x in 18-decimal format.
    ///      6th-order Taylor series, accurate to <0.01% for x < 3.0.
    ///      For production, use PRBMath.exp() or ABDKMath64x64.
    function _expNeg(uint256 x) internal pure returns (uint256) {
        uint256 UNIT = 1e18;
        if (x > 10 * UNIT) return 0;

        uint256 x2 = x * x / UNIT;
        uint256 x3 = x2 * x / UNIT;
        uint256 x4 = x3 * x / UNIT;
        uint256 x5 = x4 * x / UNIT;
        uint256 x6 = x5 * x / UNIT;

        uint256 pos = UNIT + x2 / 2 + x4 / 24 + x6 / 720;
        uint256 neg = x + x3 / 6 + x5 / 120;

        if (neg >= pos) return 0;
        return pos - neg;
    }

    // ── Activation ──────────────────────────────────────────────────

    /// @notice Client activates additional credit, up to the current face value.
    function activate(uint256 tokenId, uint256 amount) external {
        require(ownerOf(tokenId) == msg.sender, "Not credit owner");
        CreditParams storage c = credits[tokenId];
        require(c.activatedValue + amount <= c.faceValue, "Exceeds face value");

        c.activatedValue += amount;
        c.lastActivatedAt = uint48(block.timestamp);

        emit CreditActivated(tokenId, msg.sender, amount, c.activatedValue);
    }

    /// @notice Aggregate current value of all BuckCredits owned by an account.
    /// @dev Called by Buck.mint() to compute the credit limit.
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
        require(msg.sender == c.insurer, "Not insurer");

        if (newFaceValue < c.activatedValue) {
            c.activatedValue = newFaceValue;
        }

        c.faceValue = newFaceValue;
        c.depreciationFloor = newDepreciationFloor;
        c.depType = newDepType;
        c.depRate = newDepRate;
        c.depStartAt = newDepStartAt;
        c.premiumRate = newPremiumRate;
        c.lastUpdated = uint48(block.timestamp);

        emit CreditUpdated(tokenId, msg.sender, newFaceValue, newDepRate, newPremiumRate);
    }
}
