// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IBuckNetView} from "../IBuckNetView.sol";

/// @title CarryRatio -- the one conversion between nominal and net BUCK.
///
/// @notice Buck's ERC-20 view is NOMINAL: balanceOf, Transfer amounts, and
///         everything a Uniswap pool computes (its reserves, ticks, quotes and
///         swap amounts). An ordinary account's nominal BUCK is full value. A
///         CARRYING account -- a pool, the router, the basket, the wheel, the
///         Jubilee fund, an insurance pool -- carries its accrued demurrage
///         instead of paying it, so its nominal BUCK is worth net / gross of
///         full-value BUCK, and a transfer out of it delivers exactly that
///         share (BuckCash.move: net x nominal / gross, at 1e36 scale).
///
///         Anything valued or priced in BUCK is net. Convert here, once:
///         - with the ratio of the account the BUCK LEAVES: a pool's for BUCK,
///           or a TOKEN's BUCK price, out of that pool; the paying wallet's for
///           BUCK a Carrying wallet sends;
///         - a price in BUCK per TOKEN is an amount of BUCK and converts the
///           same way; a ratio of two prices from the same pool, or a pool's
///           spot against its own TWAP, needs no conversion (r cancels);
///         - an empty account (gross 0) carries nothing: its quoted amounts
///           are already net. A caller that must refuse an empty pool says so;
///         - conversions are for quotes, marks and sizing; an executed amount
///           is measured as the change in net balance (move rounds), never
///           recomputed from a ratio read before it.
///         Nominal stays only where Uniswap or the account cap needs it:
///         amounts handed to a pool, a pool's own slippage guard, a Uniswap
///         simulation, and the gross balance cap.
library CarryRatio {
    uint256 internal constant Q = 1e36;

    error NoNetValue();

    /// @notice An account's BUCK as Buck books it: gross (nominal) and net,
    ///         both raw BUCK x Q. Read once; convert any number of amounts.
    function read(address buck, address account) internal view returns (uint256 gross, uint256 net) {
        return IBuckNetView(buck).accountBalancesScaled(account);
    }

    /// @notice Net value of `nominal` BUCK (raw, or a price in BUCK) leaving
    ///         an account with this (gross, net), rounded down.
    function toNet(uint256 gross, uint256 net, uint256 nominal) internal pure returns (uint256) {
        return gross == 0 || gross == net ? nominal : Math.mulDiv(nominal, net, gross);
    }

    /// @notice What BuckCash.move delivers when the account sends `nominal`
    ///         raw BUCK: net raw BUCK x Q, rounded down.
    function toNetScaled(uint256 gross, uint256 net, uint256 nominal) internal pure returns (uint256) {
        return gross == 0 ? nominal * Q : Math.mulDiv(nominal * Q, net, gross);
    }

    /// @notice Nominal raw BUCK the account must send to deliver at least
    ///         `value` raw net BUCK, rounded up. Reverts if it has no net value.
    function toNominal(uint256 gross, uint256 net, uint256 value) internal pure returns (uint256) {
        if (gross == 0 || gross == net) return value;
        if (net == 0) revert NoNetValue();
        return Math.mulDiv(value, gross, net, Math.Rounding.Ceil);
    }
}
