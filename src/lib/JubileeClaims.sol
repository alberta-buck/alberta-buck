// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckTypes} from "../BuckTypes.sol";

/// @notice Aggregate the separately capped issuance ages used to apportion
///         Jubilee assets. Internal library: its host MUST authenticate every
///         update and obtain principal/seconds from the actual BuckCredit NFT.
/// @dev Amounts are raw BUCK-seconds, retaining fractions until the caller
///      computes its payout ratio. For each NFT, contribution is its
///      claimable seconds, capped at principal * 50 years. Unsaturated
///      principal is the time derivative. A min-heap tracks the timestamp at
///      which each NFT saturates. Mature entries are processed permissionlessly
///      in bounded batches; updates and exact views revert while a backlog
///      remains, so nobody can silently use an overstated denominator.
library JubileeClaims {
    uint256 internal constant YEAR = 365 days + 6 hours;
    uint256 internal constant MATURITY = 50 * YEAR;

    error AlreadyInitialized();
    error NotInitialized();
    error PendingMaturities();
    error InvalidPosition();
    error AggregateOverflow();
    error TimestampOverflow();
    error ZeroStepLimit();

    struct Position {
        uint256 principal;
        uint256 secondsAtCheckpoint;
        uint256 heapIndex; // zero when absent or already saturated
        uint64 checkpoint;
    }

    struct Maturity {
        uint256 tokenId;
        uint256 overshoot; // ceil-to-second increment past the position's cap
        uint64 at;
    }

    struct Book {
        mapping(uint256 => Position) positions;
        Maturity[] heap; // one-indexed; index zero is a sentinel
        uint256 secondsAtCheckpoint;
        uint256 unsaturatedPrincipal;
        uint256 totalPrincipal;
        uint64 checkpoint;
        bool initialized;
    }

    function initialize(Book storage self) internal {
        if (self.initialized) revert AlreadyInitialized();
        self.initialized = true;
        self.checkpoint = _now();
        self.heap.push();
    }

    /// @notice Replace one NFT's recorded contribution with its current
    ///         BuckCredit values. The current implementation uses a single
    ///         average age within an NFT, capped before additional issuance.
    /// @dev Reverts if matured entries need processing. The caller's credit
    ///      update and this call must be in the same atomic transaction.
    function update(Book storage self, uint256 tokenId, uint256 principal, uint256 claimableSeconds) internal {
        _requireCurrent(self);
        if (principal > BuckTypes.MAX_BALANCE || claimableSeconds > principal * MATURITY) revert InvalidPosition();
        _advance(self, _now());
        Position storage p = self.positions[tokenId];
        if (p.principal != 0) {
            uint256 old = _positionSeconds(p, block.timestamp);
            self.secondsAtCheckpoint -= old;
            self.totalPrincipal -= p.principal;
            if (p.heapIndex != 0) {
                self.unsaturatedPrincipal -= p.principal;
                _remove(self, p.heapIndex);
            }
        }
        // Leave one principal unit of headroom for each ceil-to-second
        // maturity overshoot before it is subtracted from the total.
        if (principal > type(uint256).max / (MATURITY + 1) - self.totalPrincipal) revert AggregateOverflow();
        self.totalPrincipal += principal;
        self.secondsAtCheckpoint += claimableSeconds;
        p.principal = principal;
        p.secondsAtCheckpoint = claimableSeconds;
        p.checkpoint = _now();
        if (principal != 0 && claimableSeconds < principal * MATURITY) {
            self.unsaturatedPrincipal += principal;
            uint256 gap = principal * MATURITY - claimableSeconds;
            uint256 elapsed = Math.ceilDiv(gap, principal);
            uint256 due = block.timestamp + elapsed;
            if (due > type(uint64).max) revert TimestampOverflow();
            _insert(self, Maturity(tokenId, elapsed * principal - gap, uint64(due)));
        }
    }

    /// @notice Process at most `maxSteps` matured NFTs and checkpoint to now
    ///         only when the backlog is empty. Anyone may call the host's
    ///         permissionless wrapper to make progress.
    function process(Book storage self, uint256 maxSteps) internal returns (uint256 processed, bool current) {
        if (!self.initialized) revert NotInitialized();
        if (maxSteps == 0) revert ZeroStepLimit();
        uint64 now_ = _now();
        while (processed < maxSteps && _due(self, now_)) {
            Maturity memory m = self.heap[1];
            _advance(self, m.at);
            Position storage p = self.positions[m.tokenId];
            self.secondsAtCheckpoint -= m.overshoot;
            self.unsaturatedPrincipal -= p.principal;
            _remove(self, 1);
            ++processed;
        }
        current = !_due(self, now_);
        if (current) _advance(self, now_);
    }

    /// @notice Exact current aggregate claimable BUCK-seconds, with no
    ///         per-NFT integer-relief rounding. Reverts on an unprocessed
    ///         maturity instead of using a stale or uncapped denominator.
    function totalSeconds(Book storage self) internal view returns (uint256) {
        _requireCurrent(self);
        return self.secondsAtCheckpoint + self.unsaturatedPrincipal * (block.timestamp - uint256(self.checkpoint));
    }

    function pending(Book storage self) internal view returns (bool) {
        if (!self.initialized) revert NotInitialized();
        return _due(self, block.timestamp);
    }

    function nextMaturity(Book storage self) internal view returns (uint256 tokenId, uint256 at) {
        if (!self.initialized) revert NotInitialized();
        if (self.heap.length == 1) return (0, 0);
        Maturity storage m = self.heap[1];
        return (m.tokenId, m.at);
    }

    function positionSeconds(Book storage self, uint256 tokenId) internal view returns (uint256) {
        if (!self.initialized) revert NotInitialized();
        return _positionSeconds(self.positions[tokenId], block.timestamp);
    }

    function _positionSeconds(Position storage p, uint256 now_) private view returns (uint256) {
        uint256 value = p.secondsAtCheckpoint + p.principal * (now_ - uint256(p.checkpoint));
        uint256 cap = p.principal * MATURITY;
        return value < cap ? value : cap;
    }

    function _advance(Book storage self, uint64 to) private {
        self.secondsAtCheckpoint += self.unsaturatedPrincipal * (uint256(to) - self.checkpoint);
        self.checkpoint = to;
    }

    function _requireCurrent(Book storage self) private view {
        if (!self.initialized) revert NotInitialized();
        if (_due(self, block.timestamp)) revert PendingMaturities();
    }

    function _due(Book storage self, uint256 now_) private view returns (bool) {
        return self.heap.length > 1 && self.heap[1].at <= now_;
    }

    function _insert(Book storage self, Maturity memory m) private {
        self.heap.push(m);
        uint256 index = self.heap.length - 1;
        self.positions[m.tokenId].heapIndex = index;
        while (index > 1 && _less(self.heap[index], self.heap[index / 2])) {
            _swap(self, index, index / 2);
            index /= 2;
        }
    }

    function _remove(Book storage self, uint256 index) private {
        uint256 last = self.heap.length - 1;
        uint256 removedId = self.heap[index].tokenId;
        self.positions[removedId].heapIndex = 0;
        if (index != last) {
            self.heap[index] = self.heap[last];
            self.positions[self.heap[index].tokenId].heapIndex = index;
        }
        self.heap.pop();
        if (index == last) return;
        if (index > 1 && _less(self.heap[index], self.heap[index / 2])) {
            while (index > 1 && _less(self.heap[index], self.heap[index / 2])) {
                _swap(self, index, index / 2);
                index /= 2;
            }
        } else {
            while (index * 2 < self.heap.length) {
                uint256 child = index * 2;
                if (child + 1 < self.heap.length && _less(self.heap[child + 1], self.heap[child])) ++child;
                if (!_less(self.heap[child], self.heap[index])) break;
                _swap(self, index, child);
                index = child;
            }
        }
    }

    function _swap(Book storage self, uint256 a, uint256 b) private {
        Maturity memory first = self.heap[a];
        self.heap[a] = self.heap[b];
        self.heap[b] = first;
        self.positions[self.heap[a].tokenId].heapIndex = a;
        self.positions[self.heap[b].tokenId].heapIndex = b;
    }

    function _less(Maturity storage a, Maturity storage b) private view returns (bool) {
        return a.at < b.at || (a.at == b.at && a.tokenId < b.tokenId);
    }

    function _now() private view returns (uint64) {
        if (block.timestamp > type(uint64).max) revert TimestampOverflow();
        return uint64(block.timestamp);
    }
}
