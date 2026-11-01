// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckTypes} from "../BuckTypes.sol";

/// @notice Bounded integer inversion for issuing a requested net BUCK amount.
/// @dev This is per facility and does not authenticate an owner, reserve or
///      insurer. The caller supplies one consistent snapshot and enforces all
///      monetary and identity rules separately.
library BuckNetMintMath {
    uint256 private constant K_SCALE = 1e18;
    uint256 private constant BP = 10_000;
    uint256 private constant MAX_STEPS = 256;

    enum Status { Found, Impossible, WorkLimit }

    struct Snapshot {
        uint256 face;
        uint256 activated;
        uint256 mark;
        uint256 principal;
        uint256 k;
        uint256 effectiveRate;
        uint256 accountRoom;
    }

    struct Plan {
        uint256 coverage;
        uint256 premium;
        uint256 iterations;
        Status status;
    }

    error InvalidSnapshot();

    /// @notice Find the least additional face coverage that can issue target.
    /// @dev Preconditions: mark <= face <= MAX_BALANCE, activated <= face,
    ///      principal and accountRoom <= MAX_BALANCE, and effectiveRate < BP.
    ///      The premium is floor((presentAfter-presentBefore)*rate/BP), while
    ///      the facility's limit is floor(presentAfter*K/K_SCALE).
    ///
    ///      Each iteration asks for the least face covering principal, target
    ///      and the premium already seen. Any solution needs at least that
    ///      face, so its premium cannot be smaller. An unchanged premium
    ///      proves this face is the least solution even when separate floors
    ///      make spendable capacity locally non-monotone. A lower bound from
    ///      the unfloored inequality accelerates the case K > premium rate.
    ///      WorkLimit means the bound was reached, not that no solution exists.
    function plan(Snapshot memory s, uint256 target) internal pure returns (Plan memory result) {
        uint256 max = BuckTypes.MAX_BALANCE;
        if (s.face > max || s.mark > s.face || s.activated > s.face || s.principal > max
            || s.accountRoom > max || s.effectiveRate >= BP || target == 0) revert InvalidSnapshot();
        if (s.face == 0 || s.mark == 0 || s.k == 0 || target > s.accountRoom
            || target > max - s.principal) {
            result.status = Status.Impossible;
            return result;
        }

        uint256 requiredBase = s.principal + target;
        uint256 maxCapacity = _capacity(s.mark, s.k);
        if (requiredBase > maxCapacity) {
            result.status = Status.Impossible;
            return result;
        }
        uint256 beforeValue = Math.mulDiv(s.mark, s.activated, s.face);
        uint256 premium;
        uint256 rateScaled = s.effectiveRate * (K_SCALE / BP);
        if (s.k > rateScaled) {
            // For any feasible present value V:
            // (K-rate)*V > (principal+target-1)*K_SCALE - rate*beforeValue.
            // These products fit in uint256 because the validated BUCK
            // values are at most 79 bits and K_SCALE is below 2^60.
            uint256 lhs = (requiredBase - 1) * K_SCALE;
            uint256 rhs = rateScaled * beforeValue;
            if (lhs >= rhs) {
                uint256 lower = (lhs - rhs) / (s.k - rateScaled) + 1;
                if (lower > s.mark) {
                    result.status = Status.Impossible;
                    return result;
                }
                uint256 initialFace = Math.mulDiv(lower, s.face, s.mark, Math.Rounding.Ceil);
                if (initialFace < s.activated) initialFace = s.activated;
                uint256 initialValue = Math.mulDiv(s.mark, initialFace, s.face);
                premium = Math.mulDiv(initialValue - beforeValue, s.effectiveRate, BP);
            }
        }

        for (uint256 step; step < MAX_STEPS; ++step) {
            result.iterations = step + 1;
            if (premium > s.accountRoom - target || premium > maxCapacity - requiredBase) {
                result.status = Status.Impossible;
                return result;
            }
            uint256 required = requiredBase + premium;
            uint256 value = Math.mulDiv(required, K_SCALE, s.k, Math.Rounding.Ceil);
            uint256 totalFace = Math.mulDiv(value, s.face, s.mark, Math.Rounding.Ceil);
            if (totalFace < s.activated) totalFace = s.activated;
            if (totalFace > s.face) {
                result.status = Status.Impossible;
                return result;
            }
            uint256 present = Math.mulDiv(s.mark, totalFace, s.face);
            uint256 nextPremium = Math.mulDiv(present - beforeValue, s.effectiveRate, BP);
            if (nextPremium == premium) {
                result.coverage = totalFace - s.activated;
                result.premium = premium;
                result.status = Status.Found;
                return result;
            }
            // The required face is a nondecreasing lower bound, so the
            // corresponding premium cannot fall between iterations.
            assert(nextPremium > premium);
            premium = nextPremium;
        }
        result.status = Status.WorkLimit;
    }

    /// @dev Saturate before multiplication so even an unusually large K
    ///      cannot overflow. NFT principal is capped at MAX_BALANCE.
    function _capacity(uint256 present, uint256 k) private pure returns (uint256) {
        if (present == 0) return 0;
        uint256 saturation = Math.ceilDiv(BuckTypes.MAX_BALANCE * K_SCALE, present);
        if (k >= saturation) return BuckTypes.MAX_BALANCE;
        return present * k / K_SCALE;
    }
}
