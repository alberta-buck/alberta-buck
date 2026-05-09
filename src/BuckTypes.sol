// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title BuckTypes -- single source of truth for the BUCK monetary types.
/// @notice Both Buck.sol (ERC-20 balances) and BuckCredit.sol (insured face
///         values) denominate amounts in 6-decimal BUCK and pack them into
///         uint80 storage slots; the demurrage integral packs into uint120.
///         Refer to these constants and types from anywhere a BUCK quantity
///         is held, displayed, or constrained -- a future change here
///         propagates everywhere in lockstep.
library BuckTypes {
    /// ERC-20 decimals for BUCK (USDC-compatible).
    uint8   internal constant DECIMALS    = 6;

    /// One BUCK in raw units (10**DECIMALS = 1e6).
    uint256 internal constant PRECISION   = 10 ** uint256(DECIMALS);

    /// Storage cap on packed BUCK balances.  2^80 - 1 ~= 1.21e24 raw
    /// = 1.21e18 BUCK at 6 decimals -- comfortably above any realistic supply.
    uint256 internal constant MAX_BALANCE = type(uint80).max;

    /// Storage cap on the BUCK*seconds demurrage integral (uint120).
    uint256 internal constant MAX_BS      = type(uint120).max;

    /// Fixed-point scale used by IBuckK.currentBuckK (commodity-basket
    /// value).  Independent of BUCK's own 6-decimal precision; isolated
    /// here so callers can refer to a named constant rather than 1e18.
    uint256 internal constant BUCKK_SCALE = 1e18;
}

// ─── User-defined value types ─────────────────────────────────────────────

/// @notice BUCK monetary quantity, packed as uint80.
/// @dev    Cap = BuckTypes.MAX_BALANCE.  Construct via toBuckQty() for
///         bound-checked uint256 inputs, or BuckQty.wrap() when the bound
///         is already known (e.g. result of checked uint80 arithmetic).
///         Named *Qty rather than `Buck` to avoid clashing with the Buck
///         ERC-20 contract; `BuckSeconds` is unambiguous.
type BuckQty is uint80;

/// @notice Cumulative BUCK*seconds (the demurrage integral), packed as uint120.
/// @dev    Cap = BuckTypes.MAX_BS.  Construct via toBuckSeconds().
type BuckSeconds is uint120;

// ─── BuckQty operator bindings (free functions, attached globally) ───────
// Underlying uint80 arithmetic in 0.8.x is checked by default, so + / -
// revert on overflow / underflow.  Comparisons compare unwrapped values.

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
    /// Unwrap to uint256 for cross-type arithmetic.
    function asUint(BuckQty b) internal pure returns (uint256) {
        return uint256(BuckQty.unwrap(b));
    }
    function isZero(BuckQty b) internal pure returns (bool) {
        return BuckQty.unwrap(b) == 0;
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

/// @notice Construct a BuckQty from a uint256, reverting if the value
///         exceeds the uint80 storage cap.  Use this whenever a uint256
///         (e.g. from an external interface or arithmetic on raw values)
///         is being narrowed back into a BuckQty.
function toBuckQty(uint256 x) pure returns (BuckQty) {
    require(x <= BuckTypes.MAX_BALANCE, "BuckQty: overflow");
    return BuckQty.wrap(uint80(x));
}

function toBuckSeconds(uint256 x) pure returns (BuckSeconds) {
    require(x <= BuckTypes.MAX_BS, "BuckSeconds: overflow");
    return BuckSeconds.wrap(uint120(x));
}
