// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

/// @notice Nonrecursive receipt pricing when Jubilee owns shares of its debtor.
/// @dev Preconditions: assets exclude this claim; principal is actual issuance;
///      cash is the fund's net scaled BUCK; seconds are capped issuance-seconds;
///      shares/basis describe the fund's one receipt, including its gain fee.
///      No input is a promise of liquid relief or collateral for new issuance.
///
///      R(E) = min(principal, floor((cash + netReceipt(E)*Q)*selected/total/Q))
///      E    = max(0, assets - principal + R(E)).
///
///      All rounding matches the fund: floor gross receipt value and gain fee,
///      then floor relief to raw BUCK. R is nondecreasing and increases by at
///      most one when E increases by one. Thus E - RHS(E) is nondecreasing,
///      with a zero in [0, assets]. Bisection finds its FIRST zero in at most
///      256 iterations, selecting the conservative price when ownership or
///      integer rounding makes the solution nonunique. No iterative external
///      calls, guessed starting mark or floating-point convergence are needed.
///
///      Postconditions: equity <= assets, relief <= principal, and the two
///      equations hold exactly in raw units. Unpaid relief must be excluded
///      separately when marking collateral; this function moves no money.
library BasketJubileeMath {
    uint256 internal constant Q = 1e36;
    uint256 internal constant BP = 10_000;

    struct Inputs {
        uint256 assets;
        uint256 principal;
        uint256 cash;
        uint256 selected;
        uint256 total;
        uint256 fundShares;
        uint256 shares;
        uint256 basis;
        uint256 feeBp;
    }

    error InvalidJubileeInputs();
    error JubileeValueOverflow();

    function solve(Inputs memory p) internal pure returns (uint256 equity, uint256 relief) {
        if (p.selected > p.total || p.fundShares > p.shares || p.feeBp > BP) {
            revert InvalidJubileeInputs();
        }
        if (p.principal == 0 || p.selected == 0) return (_equity(p, 0), 0);
        // No self-holding: relief is independent of this basket's price.
        if (p.fundShares == 0) {
            relief = _relief(p, 0);
            return (_equity(p, relief), relief);
        }
        uint256 high = p.assets;
        while (equity < high) {
            uint256 mid = equity + (high - equity) / 2;
            if (mid >= _equity(p, _relief(p, mid))) high = mid;
            else equity = mid + 1;
        }
        relief = _relief(p, equity);
    }

    function _relief(Inputs memory p, uint256 equity) private pure returns (uint256) {
        uint256 mark = p.fundShares == 0 ? 0 : Math.mulDiv(equity, p.fundShares, p.shares);
        if (mark > p.basis) mark -= Math.mulDiv(mark - p.basis, p.feeBp, BP);
        if (mark > (type(uint256).max - p.cash) / Q) revert JubileeValueOverflow();
        uint256 relief = Math.mulDiv(p.cash + mark * Q, p.selected, p.total) / Q;
        return Math.min(relief, p.principal);
    }

    function _equity(Inputs memory p, uint256 relief) private pure returns (uint256) {
        // Avoid overflowing assets + relief, including for full-width tests.
        if (p.assets >= p.principal) return p.assets - p.principal + relief;
        uint256 shortfall = p.principal - p.assets;
        return relief > shortfall ? relief - shortfall : 0;
    }
}
