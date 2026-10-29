// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

/// @notice Accumulate an investment budget toward an observed excess with a
///         one-day half-life, in the same units as the caller's balances.
/// @dev This is a reserve controller, not a measure of portfolio ROI. The
///      caller must checkpoint before its own cash-changing actions and must
///      not backdate excess first observed at the current checkpoint.
library JubileeReserveMath {
    uint256 internal constant ONE = 1e36;
    // floor(1e36 * exp(-ln(2)/86400)), generated independently with Decimal.
    uint256 internal constant PER_SECOND = 999991977495368425989823173345921758;

    function factor(uint256 elapsed) internal pure returns (uint256 result) {
        result = ONE;
        uint256 base = PER_SECOND;
        while (elapsed != 0) {
            if (elapsed & 1 != 0) result = Math.mulDiv(result, base, ONE);
            elapsed >>= 1;
            if (elapsed != 0) base = Math.mulDiv(base, base, ONE);
        }
    }

    /// @notice Grow `budget` toward the previously observed `excess`. The
    ///         result never exceeds that excess; a caller must additionally
    ///         cap it to the new excess when observing a changed balance.
    function accrue(uint256 budget, uint256 excess, uint256 elapsed) internal pure returns (uint256) {
        if (budget >= excess) return excess;
        return budget + Math.mulDiv(excess - budget, ONE - factor(elapsed), ONE);
    }
}
