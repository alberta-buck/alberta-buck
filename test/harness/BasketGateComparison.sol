// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketReference} from "../../src/basket/BasketReference.sol";
import {IBasketSignals} from "../../src/basket/IBasketSignals.sol";

/// @notice Experiment only: vary the execution gate while forwarding the
/// same observation, target excursion, quorum and sizing confidence.
contract BasketGateComparison is IBasketSignals {
    BasketReference public immutable source;
    uint8 public immutable mode; // 0 original, 1 band only, 2 shortest-EMA velocity

    constructor(BasketReference source_, uint8 mode_) {
        require(mode_ <= 2, "gate mode");
        source = source_;
        mode = mode_;
    }

    function observe() external { source.observe(); }
    function fresh() external view returns (bool) { return source.fresh(); }
    function isDue() external view returns (bool) { return source.isDue(); }
    function lastDay() external view returns (uint64) { return source.lastDay(); }
    function lastSampleAt() external view returns (uint64) { return source.lastSampleAt(); }
    function rawValue() external view returns (int256) { return source.rawValue(); }
    function consecutiveDays() external view returns (uint32) { return source.consecutiveDays(); }
    function basketValueInBuck() external view returns (int256) { return source.basketValueInBuck(); }
    function marketValues() external view returns (int256, int256) { return source.marketValues(); }
    function excursion(uint256 i) external view returns (int256) { return source.excursion(i); }
    function votes(uint256 i) external view returns (uint256) { return source.votes(i); }
    function turned(uint256 i) external view returns (bool) { return source.turned(i); }

    function running(uint256 i, int256 side) external view returns (bool) {
        if (mode == 0) return source.running(i, side);
        if (mode == 1) return false;
        int256 e = source.excursion(i);
        if (side > 0 ? e <= 0 : e >= 0) return false;
        (, int256[7] memory velocity) = source.ladder(i);
        return side > 0 ? velocity[0] > 0 : velocity[0] < 0;
    }
}
