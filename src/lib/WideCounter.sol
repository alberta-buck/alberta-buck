// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice Cumulative raw BUCK units plus fractional raw-unit remainder.
/// @dev Each part has a full uint256 slot. Keeping the integral count separate
///      lets cumulative flows exceed the range of a 1e36-scaled uint256 even
///      when every instantaneous scaled balance fits. The whole-unit count
///      still reverts if its own uint256 lifetime range is exceeded.
library WideCounter {
    uint256 internal constant SCALE = 1e36;
    struct Count { uint256 units; uint256 fraction; }

    function add(Count storage self, uint256 scaled) internal {
        if (scaled == 0) return;
        uint256 whole = scaled / SCALE;
        uint256 fraction = self.fraction + scaled % SCALE;
        if (fraction >= SCALE) {
            whole++;
            fraction -= SCALE;
        }
        self.units += whole;
        self.fraction = fraction;
    }

    function plus(Count memory self, uint256 scaled) internal pure returns (Count memory) {
        if (scaled == 0) return self;
        uint256 whole = scaled / SCALE;
        uint256 fraction = self.fraction + scaled % SCALE;
        if (fraction >= SCALE) {
            whole++;
            fraction -= SCALE;
        }
        self.units += whole;
        self.fraction = fraction;
        return self;
    }
}
