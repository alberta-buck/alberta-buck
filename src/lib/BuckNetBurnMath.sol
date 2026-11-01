// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckTypes} from "../BuckTypes.sol";

/// @notice Bounded inversion of one ordinary owner's balance and coverage.
/// @dev This is a candidate, not authorization or a complete burn quote. The
///      caller supplies one coherent post-refresh snapshot and independently
///      checks identity, NFT ownership, pool binding,
///      maturity and that the snapshot remains current during execution.
library BuckNetBurnMath {
    uint256 private constant Q = 1e36;
    uint256 private constant MAX_STEPS = 256;

    enum Status { Found, Impossible, WorkLimit }

    struct Snapshot {
        uint256 principal;
        uint256 issuedSeconds;
        uint256 totalSeconds;
        uint256 fundValueScaled;
        uint256 reliefLimitScaled;
        uint256 premiumPrincipal;
        uint256 poolGrossScaled;
        uint256 poolNetScaled;
        uint256 poolAvailableNominal;
        uint256 walletScaled;
    }

    struct Coverage {
        uint256 face;
        uint256 active;
        uint256 mark;
        uint256 k;
    }

    struct Plan {
        uint256 principal;
        uint256 reliefScaled;
        uint256 refundNominal;
        uint256 refundNetScaled;
        uint256 coverageReduction;
        uint256 iterations;
        Status status;
    }

    error InvalidSnapshot();

    /// @notice Find the least principal whose settlement decreases the
    ///         ordinary owner's visible balance by at least `target`.
    /// @dev Preconditions: all inputs describe the same instant, fund value
    ///      includes its net cash and the selected published position mark,
    ///      and reliefLimitScaled is the smaller of the operation cash cap
    ///      and remaining shared block budget. The pool amount is the lesser
    ///      of nominal liquidity and allowance. Externally check pool identity
    ///      and settlement rules. A zero target requires no settlement.
    ///
    ///      Let W be the exact pre-burn wallet and Q one raw unit. Since
    ///      balanceOf is floor(W/Q), a candidate p meets target N precisely
    ///      when p*Q >= (N-1)*Q + W%Q + 1 + relief(p) + refundNet(p).
    ///      Relief and refund are nondecreasing in p even though the visible
    ///      balance decrease need not be. Each iteration jumps to the least p
    ///      meeting this lower bound with the values already seen. No skipped
    ///      p can satisfy the inequality. At a monetary fixed point, also
    ///      check the same proportional face reduction and K shortfall rule
    ///      as settlement. If K fails by d raw units, every principal less
    ///      than p+d also fails: its remaining capacity cannot exceed the
    ///      capacity at p. If only owner funds fail, try the next raw unit.
    ///      Neither condition is assumed monotone. The first passing point is
    ///      the least candidate under these checks. Exhausting the work bound
    ///      does not prove infeasibility.
    function plan(Snapshot memory s, Coverage memory c, uint256 target)
        internal pure returns (Plan memory result)
    {
        uint256 max = BuckTypes.MAX_BALANCE;
        uint256 cap = max * Q;
        if (s.principal > max || s.premiumPrincipal > max || s.walletScaled > cap
            || s.poolGrossScaled > cap || s.poolNetScaled > s.poolGrossScaled
            || s.issuedSeconds > s.totalSeconds || c.face > max || c.mark > c.face
            || c.active > c.face) revert InvalidSnapshot();
        if (target == 0) {
            result.status = Status.Found;
            return result;
        }
        if (s.principal == 0 || target > s.walletScaled / Q || target > s.principal) {
            result.status = Status.Impossible;
            return result;
        }

        uint256 base = (target - 1) * Q + s.walletScaled % Q + 1;
        uint256 present = c.active == 0 ? 0 : Math.mulDiv(c.mark, c.active, c.face);
        uint256 capacityBefore = Math.mulDiv(present, c.k, 1e18);
        uint256 shortfallBefore = s.principal > capacityBefore ? s.principal - capacityBefore : 0;
        uint256 p = target;
        for (uint256 step; step < MAX_STEPS; ++step) {
            result.iterations = step + 1;
            uint256 refund = Math.mulDiv(s.premiumPrincipal, p, s.principal);
            if (refund > s.poolAvailableNominal || refund > s.poolGrossScaled / Q) {
                result.status = Status.Impossible;
                return result;
            }
            uint256 refundNet = refund == 0 ? 0 : Math.mulDiv(s.poolNetScaled, refund * Q, s.poolGrossScaled);
            if (refundNet > cap - s.walletScaled) {
                result.status = Status.Impossible;
                return result;
            }
            uint256 selected = Math.mulDiv(s.issuedSeconds, p, s.principal);
            uint256 relief = selected == 0 ? 0 : Math.min(p * Q, Math.mulDiv(s.fundValueScaled, selected, s.totalSeconds));
            if (relief > s.reliefLimitScaled) {
                result.status = Status.Impossible;
                return result;
            }
            uint256 needed = Math.ceilDiv(base + relief + refundNet, Q);
            if (needed <= p) {
                uint256 faceReduction = Math.mulDiv(c.active, p, s.principal);
                uint256 remainingFace = c.active - faceReduction;
                uint256 remainingValue = remainingFace == 0 ? 0 : Math.mulDiv(c.mark, remainingFace, c.face);
                uint256 capacityAfter = Math.mulDiv(remainingValue, c.k, 1e18);
                uint256 left = s.principal - p;
                uint256 shortfallAfter = left > capacityAfter ? left - capacityAfter : 0;
                if (p * Q <= s.walletScaled + refundNet + relief && shortfallAfter <= shortfallBefore) {
                    result.principal = p;
                    result.reliefScaled = relief;
                    result.refundNominal = refund;
                    result.refundNetScaled = refundNet;
                    result.coverageReduction = faceReduction;
                    result.status = Status.Found;
                    return result;
                }
                if (p == s.principal) {
                    result.status = Status.Impossible;
                    return result;
                }
                if (shortfallAfter > shortfallBefore) p += shortfallAfter - shortfallBefore;
                else ++p;
                continue;
            }
            if (needed > s.principal) {
                result.status = Status.Impossible;
                return result;
            }
            p = needed;
        }
        result.status = Status.WorkLimit;
    }

    /// @notice Find the greatest requested balance decrease up to `requested`
    ///         for this facility, and its least-principal candidate.
    /// @dev This is useful to an ordered multi-facility allocator. Feasibility
    ///      is monotone in the requested decrease: a settlement achieving N
    ///      also achieves every smaller positive target. Search that predicate,
    ///      not the non-monotone result of adjacent principal amounts. A K
    ///      constraint can force the actual visible decrease above the returned
    ///      target; a caller must observe and account for that actual change.
    ///      Any inner WorkLimit is propagated, never treated as impossible.
    ///      At most 79 binary-search iterations plus the initial and final
    ///      probes run a bounded inner search. Gas and supported batch sizes
    ///      require measurement before integration.
    function planAtMost(Snapshot memory s, Coverage memory c, uint256 requested)
        internal pure returns (uint256 target, Plan memory result)
    {
        if (requested == 0) return (0, plan(s, c, 0));
        uint256 upper = Math.min(requested, Math.min(s.principal, s.walletScaled / Q));
        if (upper == 0) return (0, plan(s, c, requested));
        result = plan(s, c, upper);
        if (result.status == Status.Found) return (upper, result);
        if (result.status == Status.WorkLimit) return (0, result);

        uint256 lo;
        uint256 hi = upper - 1;
        while (lo < hi) {
            uint256 mid = lo + (hi - lo + 1) / 2;
            Plan memory candidate = plan(s, c, mid);
            if (candidate.status == Status.WorkLimit) return (0, candidate);
            if (candidate.status == Status.Found) lo = mid;
            else hi = mid - 1;
        }
        if (lo == 0) {
            result.status = Status.Impossible;
            return (0, result);
        }
        result = plan(s, c, lo);
        return (lo, result);
    }
}
