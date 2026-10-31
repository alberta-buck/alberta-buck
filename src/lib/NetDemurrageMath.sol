// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

/// @notice Arithmetic for continuously declining BUCK balances and linear
///         issuance-based relief. BuckNet and BuckCredit use these primitives;
///         the legacy Buck contract still uses its earlier accounting.
/// @dev Decay uses exp(-0.02 / (365.25 days)), rounded down to 36 decimal
///      places, and exponentiation by squaring. `amount` can be any unit as
///      long as it and the result fit uint256. For frequently checkpointed
///      accounts, pass fractional raw units rather than rounding every fee to
///      the BUCK's six-decimal external unit. See the implementation plan for
///      the remaining supply, storage and integration invariants.
library NetDemurrageMath {
    uint256 internal constant SCALE = 1e36;
    uint256 internal constant YEAR = 365 days + 6 hours;
    uint256 internal constant RELIEF_SECONDS = 50 * YEAR;
    // floor(exp(-0.02 / YEAR) * 1e36), calculated with 120 decimal digits.
    uint256 internal constant SECOND_FACTOR = 999999999366238243920247977078958586;
    uint256 internal constant CHUNK_SECONDS = 1000 * YEAR;
    // The same exponentiation-by-squaring result for a 1,000-year chunk.
    uint256 internal constant CHUNK_FACTOR = 2061153622438557827965940310;
    // exp(-180) * type(uint256).max < 1; no representable unit survives.
    uint256 internal constant EMPTY_SECONDS = 9000 * YEAR;

    error FeeExceedsNominal();
    error InsufficientNet();
    error InvalidPrincipal();
    error FundExceedsIssuance();

    /// @notice Remaining amount after demurrage, rounded down in its own unit.
    /// @dev Chunk long intervals so factors below 1e-36 do not round to zero
    ///      before they are applied to a large balance. At most eight chunk
    ///      multiplications and 35 exponentiation iterations are needed.
    function decay(uint256 amount, uint256 elapsed) internal pure returns (uint256) {
        if (amount == 0 || elapsed >= EMPTY_SECONDS) return 0;
        while (elapsed >= CHUNK_SECONDS) {
            amount = Math.mulDiv(amount, CHUNK_FACTOR, SCALE);
            if (amount == 0) return 0;
            elapsed -= CHUNK_SECONDS;
        }
        return Math.mulDiv(amount, _factor(elapsed), SCALE);
    }

    /// @notice Net fund cash after an event-free interval, given outstanding
    ///         issuance and net fund cash in the same accounting units.
    /// @dev Demurrage on the fund's own balance is paid to itself, so only the
    ///      loss from *other* net balances increases fund net cash.
    function fundNetAfter(uint256 issuance, uint256 fund, uint256 elapsed) internal pure returns (uint256) {
        if (fund > issuance) revert FundExceedsIssuance();
        uint256 outside = issuance - fund;
        return fund + (outside - decay(outside, elapsed));
    }

    /// @notice The baseline relief for a proportional reduction of one NFT.
    /// @param principal Its outstanding issued BUCK before reduction.
    /// @param buckSeconds Time integral of that NFT's issued principal.
    /// @param reduction Principal selected for reduction, no greater than principal.
    /// @dev The target is capped by the reduction; actual Jubilee relief can
    ///      be different and must be computed from realized available assets.
    function targetRelief(uint256 principal, uint256 buckSeconds, uint256 reduction)
        internal pure returns (uint256)
    {
        if (principal == 0 || reduction > principal) revert InvalidPrincipal();
        uint256 secondsForReduction = Math.mulDiv(buckSeconds, reduction, principal);
        uint256 target = secondsForReduction / RELIEF_SECONDS;
        return target > reduction ? reduction : target;
    }

    /// @notice Net amount in a nominal Carrying transfer using a pro-rata
    ///         floored fee, without crediting that fee to Jubilee a second time.
    function nominalNet(uint256 nominal, uint256 gross, uint256 fee) internal pure returns (uint256) {
        if (fee > gross) revert FeeExceedsNominal();
        if (nominal > gross) revert InsufficientNet();
        if (nominal == 0) return 0;
        return nominal - Math.mulDiv(fee, nominal, gross);
    }

    /// @notice Least nominal Carrying amount which delivers at least `net`
    ///         under the floored fee convention used by nominalNet.
    function nominalForNet(uint256 net, uint256 gross, uint256 fee) internal pure returns (uint256) {
        if (fee > gross) revert FeeExceedsNominal();
        uint256 available = gross - fee;
        if (net > available) revert InsufficientNet();
        if (net == 0) return 0;
        return Math.mulDiv(net - 1, gross, available) + 1;
    }

    function _factor(uint256 elapsed) private pure returns (uint256 factor) {
        factor = SCALE;
        uint256 base = SECOND_FACTOR;
        while (elapsed != 0) {
            if (elapsed & 1 != 0) factor = Math.mulDiv(factor, base, SCALE);
            elapsed >>= 1;
            if (elapsed != 0) base = Math.mulDiv(base, base, SCALE);
        }
    }
}
