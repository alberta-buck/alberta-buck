// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title BuckTypes -- single source of truth for the BUCK monetary types.
/// @notice Both Buck.sol (ERC-20 balances) and BuckCredit.sol (insured face
///         values) denominate amounts in 6-decimal BUCK.  Balances are stored
///         in *signed* int80 slots so an account's raw balance can go
///         negative when the holder spends into NFT-backed credit headroom;
///         BuckCredit's face / floor / activated values use the same wrapper
///         but always remain non-negative.  The demurrage integral packs into
///         uint120.
///
///         Refer to these constants and types from anywhere a BUCK quantity
///         is held, displayed, or constrained -- a future change here
///         propagates everywhere in lockstep.
library BuckTypes {
    /// ERC-20 decimals for BUCK (USDC-compatible).
    uint8   internal constant DECIMALS    = 6;

    /// One BUCK in raw units (10**DECIMALS = 1e6).
    uint256 internal constant PRECISION   = 10 ** uint256(DECIMALS);

    /// Maximum positive value a BuckQty can hold (int80 positive range).
    /// 2^79 - 1 ~= 6.04e23 raw = ~6.04e17 BUCK at 6 decimals.  Halves the
    /// previous uint80 cap; still well above any realistic per-account
    /// holding (~600 trillion BUCK).
    uint256 internal constant MAX_BALANCE = uint256(uint80(uint256(int256(type(int80).max))));

    /// Most negative value a BuckQty can hold (int80 negative range).
    int256  internal constant MIN_BALANCE = int256(type(int80).min);

    /// Storage cap on the BUCK*seconds demurrage integral (uint120).
    uint256 internal constant MAX_BS      = type(uint120).max;

    /// Fixed-point scale used by IBuckK.currentBuckK (commodity-basket
    /// value).  Independent of BUCK's own 6-decimal precision; isolated
    /// here so callers can refer to a named constant rather than 1e18.
    uint256 internal constant BUCKK_SCALE = 1e18;
}

// ─── User-defined value types ─────────────────────────────────────────────

/// @notice BUCK monetary quantity, packed as int80 (signed).
/// @dev    Range = [BuckTypes.MIN_BALANCE, int256(BuckTypes.MAX_BALANCE)].
///         Construct via toBuckQty(uint256) for non-negative inputs (bounds-
///         checked against the positive cap), or toBuckQtySigned(int256) for
///         signed inputs (bounds-checked against both endpoints), or
///         BuckQty.wrap() when the int80 fit is already known.
///
///         BuckCredit always stores non-negative values (face / floor /
///         activated) and reads them via asUint().  Buck's per-account
///         balance is signed: positive = held; negative = NFT-backed debt.
///         Buck reads it via asInt() in signed contexts and via asUint()
///         only when the caller has already verified non-negativity.
type BuckQty is int80;

/// @notice Cumulative BUCK*seconds (the demurrage integral), packed as uint120.
/// @dev    Cap = BuckTypes.MAX_BS.  Construct via toBuckSeconds().  Demurrage
///         on negative balances is clamped to zero by Buck._feeOwing, so this
///         field never accumulates negative-balance debt-interest.
type BuckSeconds is uint120;

// ─── BuckQty operator bindings (free functions, attached globally) ───────
// int80 arithmetic in 0.8.x is checked by default -- + / - revert on signed
// overflow / underflow.  Comparisons are signed.

function _qtyAdd(BuckQty a, BuckQty b) pure returns (BuckQty) {
    return BuckQty.wrap(BuckQty.unwrap(a) + BuckQty.unwrap(b));
}
function _qtySub(BuckQty a, BuckQty b) pure returns (BuckQty) {
    return BuckQty.wrap(BuckQty.unwrap(a) - BuckQty.unwrap(b));
}
function _qtyLt(BuckQty a, BuckQty b) pure returns (bool) { return BuckQty.unwrap(a) <  BuckQty.unwrap(b); }
function _qtyLe(BuckQty a, BuckQty b) pure returns (bool) { return BuckQty.unwrap(a) <= BuckQty.unwrap(b); }
function _qtyGt(BuckQty a, BuckQty b) pure returns (bool) { return BuckQty.unwrap(a) >  BuckQty.unwrap(b); }
function _qtyGe(BuckQty a, BuckQty b) pure returns (bool) { return BuckQty.unwrap(a) >= BuckQty.unwrap(b); }
function _qtyEq(BuckQty a, BuckQty b) pure returns (bool) { return BuckQty.unwrap(a) == BuckQty.unwrap(b); }
function _qtyNe(BuckQty a, BuckQty b) pure returns (bool) { return BuckQty.unwrap(a) != BuckQty.unwrap(b); }

using {
    _qtyAdd as +, _qtySub as -,
    _qtyLt  as <, _qtyLe  as <=, _qtyGt as >, _qtyGe as >=,
    _qtyEq  as ==, _qtyNe as !=
} for BuckQty global;

// ─── BuckSeconds operator bindings ───────────────────────────────────────

function _bsAdd(BuckSeconds a, BuckSeconds b) pure returns (BuckSeconds) {
    return BuckSeconds.wrap(BuckSeconds.unwrap(a) + BuckSeconds.unwrap(b));
}
function _bsLe(BuckSeconds a, BuckSeconds b) pure returns (bool) {
    return BuckSeconds.unwrap(a) <= BuckSeconds.unwrap(b);
}
function _bsEq(BuckSeconds a, BuckSeconds b) pure returns (bool) {
    return BuckSeconds.unwrap(a) == BuckSeconds.unwrap(b);
}
function _bsNe(BuckSeconds a, BuckSeconds b) pure returns (bool) {
    return BuckSeconds.unwrap(a) != BuckSeconds.unwrap(b);
}

using {_bsAdd as +, _bsLe as <=, _bsEq as ==, _bsNe as !=} for BuckSeconds global;

// ─── Method libraries (attached globally) ────────────────────────────────

library BuckQtyLib {
    /// Unwrap to uint256 for cross-type arithmetic.  Reverts if the
    /// underlying int80 is negative -- callers that may see a negative
    /// balance MUST use asInt() (or check isNegative() first).
    function asUint(BuckQty b) internal pure returns (uint256) {
        int80 raw = BuckQty.unwrap(b);
        require(raw >= 0, "BuckQty: negative");
        return uint256(uint80(raw));
    }
    /// Unwrap to int256 for signed arithmetic.  Always safe.
    function asInt(BuckQty b) internal pure returns (int256) {
        return int256(BuckQty.unwrap(b));
    }
    function isZero(BuckQty b) internal pure returns (bool) {
        return BuckQty.unwrap(b) == 0;
    }
    function isNegative(BuckQty b) internal pure returns (bool) {
        return BuckQty.unwrap(b) < 0;
    }
}

library BuckSecondsLib {
    function asUint(BuckSeconds bs) internal pure returns (uint256) {
        return uint256(BuckSeconds.unwrap(bs));
    }
    function isZero(BuckSeconds bs) internal pure returns (bool) {
        return BuckSeconds.unwrap(bs) == 0;
    }
}

using BuckQtyLib     for BuckQty     global;
using BuckSecondsLib for BuckSeconds global;

// ─── Bound-checked constructors ──────────────────────────────────────────

/// @notice Construct a non-negative BuckQty from a uint256.
/// @dev    Reverts if the value exceeds BuckTypes.MAX_BALANCE (the int80
///         positive cap).  Use this for any unsigned input being narrowed
///         into a BuckQty; for signed inputs use toBuckQtySigned().
function toBuckQty(uint256 x) pure returns (BuckQty) {
    require(x <= BuckTypes.MAX_BALANCE, "BuckQty: overflow");
    return BuckQty.wrap(int80(uint80(x)));
}

/// @notice Construct a (possibly negative) BuckQty from an int256.
/// @dev    Reverts if the value falls outside the int80 range.  Used by
///         Buck.sol's signed-balance write paths after a transfer would
///         drive an account negative or back to positive.
function toBuckQtySigned(int256 x) pure returns (BuckQty) {
    require(x <= int256(type(int80).max) && x >= int256(type(int80).min),
            "BuckQty: out of range");
    return BuckQty.wrap(int80(x));
}

function toBuckSeconds(uint256 x) pure returns (BuckSeconds) {
    require(x <= BuckTypes.MAX_BS, "BuckSeconds: overflow");
    return BuckSeconds.wrap(uint120(x));
}

// ─── Shared cross-contract DTOs ──────────────────────────────────────────

/// @notice One NFT's mint-relevant fields, returned in bulk by
///         BuckCredit.batchCreditInfo so Buck's _allocateMint /
///         _allocateBurn loops do not pay an external call per NFT.
///         File-level struct so both Buck.sol and BuckCredit.sol can
///         reference the same definition without an import cycle.
struct CreditSlice {
    address owner;            // ownerOf(tokenId)
    uint256 faceValue;        // 6-decimal BUCK
    uint256 activatedValue;   // 6-decimal BUCK
    uint32  premiumRate;      // basis points
}
