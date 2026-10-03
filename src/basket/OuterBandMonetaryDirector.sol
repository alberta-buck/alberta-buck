// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketSignals} from "./BasketSignals.sol";
import {IMonetaryDirector} from "./MonetaryDesk.sol";

/// @notice An outer-band desk policy. The inner region is left to the direct
///         K controller. Defaults belong in deployment configuration, not in
///         a universal claim about stable gains or profitable interventions.
///         Sample this adapter in the wheel: it samples the price service and
///         advances hysteresis/persistence even when the desk does no work.
contract OuterBandMonetaryDirector is IMonetaryDirector {
    BasketSignals public immutable signals;
    uint16 public immutable enterBp;
    uint16 public immutable releaseBp;
    uint16 public immutable gainBp;
    uint16 public immutable capBp;
    uint16 public immutable persistDays;
    int8 public side;                       // +1 cheap BUCK, -1 dear BUCK
    uint32 public overDays;
    uint64 public seenDay;
    uint64 public seenSample;

    constructor(address signals_, uint16 enter_, uint16 release_, uint16 gain_, uint16 cap_, uint16 persist_) {
        require(signals_ != address(0) && release_ > 0 && enter_ > release_ && enter_ <= 5000
                && gain_ <= 10000 && cap_ <= 500 && persist_ > 0, "desk policy");
        signals = BasketSignals(signals_);
        enterBp = enter_; releaseBp = release_; gainBp = gain_; capBp = cap_; persistDays = persist_;
    }

    function epochNow() external view returns (uint32) { return uint32(block.timestamp / 1 days); }
    function lastDay() external view returns (uint64) { return signals.lastDay(); }
    function lastSampleAt() external view returns (uint64) { return seenSample; }
    function isDue() external view returns (bool) {
        return signals.isDue() || signals.lastSampleAt() != seenSample;
    }

    function observe() external {
        signals.observe();
        if (!signals.fresh()) { side = 0; overDays = 0; return; }
        uint64 sampled = signals.lastSampleAt();
        // Re-evaluate quality even when timestamps match (some L2 blocks
        // share a timestamp, and a constituent change can re-prime a sample).
        // seenDay, rather than call count, controls persistence increments.
        seenSample = sampled;
        uint64 day = signals.lastDay();
        int256 error = signals.rawValue() - 1e18;
        int8 next = _side(error);
        bool consecutive = day == seenDay + 1 && signals.consecutiveDays() > 1;
        // Count real, same-side daily observations outside the entry band.
        // Returning into the release band, reversing sign or missing a day
        // clears persistence. Repeated calls in a day cannot advance it.
        if (next == 0 || _abs(error) < uint256(enterBp) * 1e14) overDays = 0;
        else if (next != side || signals.consecutiveDays() == 1 || day > seenDay + 1) overDays = 1;
        else if (consecutive) ++overDays;
        side = next;
        seenDay = day;
    }

    function _side(int256 error) private view returns (int8) {
        uint256 magnitude = _abs(error);
        int8 direction = error > 0 ? int8(1) : int8(-1);
        uint256 threshold = direction == side ? releaseBp : enterBp;
        return magnitude > threshold * 1e14 ? direction : int8(0);
    }

    function monetaryEffort() external view returns (int32 effortBp, bool outright) {
        if (!signals.fresh()) return (0, false);
        // Re-read the market, so cached advice cannot trade after a reversal
        // or an unavailable TWAP. The raw error must still agree with the
        // observed side. K independently reads that same unoffset raw value.
        try signals.marketValues() returns (int256 raw, int256 spot) {
            int256 e = raw - 1e18;
            int8 direction = _side(e);
            int8 sampledSide = _side(signals.rawValue() - 1e18);
            if (direction == 0 || direction != sampledSide) return (0, false);
            int256 spotError = spot - 1e18;
            if ((spotError > 0 ? int8(1) : int8(-1)) != direction
                || _abs(spotError) <= uint256(releaseBp) * 1e14) return (0, false);
            uint256 magnitude = _abs(e) < _abs(spotError) ? _abs(e) : _abs(spotError);
            uint256 excess = magnitude / 1e14 - releaseBp;
            uint256 amount = excess * gainBp / 10000;
            if (amount > capBp) amount = capBp;
            effortBp = direction > 0 ? -int32(uint32(amount)) : int32(uint32(amount));
            outright = direction == side && overDays >= persistDays
                && signals.consecutiveDays() >= persistDays;
        } catch { return (0, false); }
    }

    /// @notice Used by the desk after execution. A terminal spot inside the
    ///         release band rolls the operation back, even though the TWAP
    ///         has not yet moved. The desk cannot spend through K's inner band.
    function executionAllowed(int32 effort) external view returns (bool) {
        if (!signals.fresh() || effort == 0) return false;
        try signals.marketValues() returns (int256 raw, int256 spot) {
            int256 e = raw - 1e18;
            int256 s = spot - 1e18;
            uint256 threshold = uint256(releaseBp) * 1e14;
            return effort < 0 ? e > int256(threshold) && s > int256(threshold)
                              : e < -int256(threshold) && s < -int256(threshold);
        } catch { return false; }
    }

    function _abs(int256 x) private pure returns (uint256) { return uint256(x < 0 ? -x : x); }
}
