// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title BuckTypes -- single source of truth for the BUCK monetary type.
/// @notice Both Buck.sol (ERC-20 balances) and BuckCredit.sol (insured face
///         values) denominate amounts in 6-decimal BUCK and pack them into
///         uint80 storage slots. Anything that holds, displays, or constrains
///         a BUCK quantity should refer to these constants -- changing them
///         here propagates everywhere.
library BuckTypes {
    /// ERC-20 decimals for BUCK (USDC-compatible).
    uint8   internal constant DECIMALS    = 6;

    /// One BUCK in raw units (10**DECIMALS = 1e6).
    uint256 internal constant PRECISION   = 10 ** uint256(DECIMALS);

    /// Storage cap for packed BUCK balances. 2^80 - 1 ~= 1.21e24 raw
    /// = 1.21e18 BUCK at 6 decimals -- comfortably above any realistic supply.
    uint256 internal constant MAX_BALANCE = type(uint80).max;

    /// Fixed-point scale used by IBuckK.currentBuckK() (commodity-basket
    /// value).  Independent of BUCK's own 6-decimal precision; isolated here
    /// so callers can refer to a named constant rather than a magic 1e18.
    uint256 internal constant BUCKK_SCALE = 1e18;
}
