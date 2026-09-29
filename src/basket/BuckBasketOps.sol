// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketProRata}   from "./BuckBasketProRata.sol";
import {MonetaryDesk}        from "./MonetaryDesk.sol";

/// @title BuckBasketOps -- the pro-rata basket plus the monetary desk.
///
/// @notice The desk (MonetaryDesk: the quadrants, the bounds, the book, the
///         stabilizer seam) on the pro-rata shell.  The desk reads NAV as the
///         depositor-claim base, 2x the BUCK half of the full-range positions
///         (`poolBuckValues`), and prices at spot from the same read.
contract BuckBasketOps is BuckBasketProRata, MonetaryDesk {

    constructor(
        address _buck,
        address _controller,
        address _v3Factory,
        address _governance,
        uint24  _defaultFeeTier,
        uint32  _twapWindow,
        uint16  _observationCardinality,
        uint256 _defaultMaxDeviationBp,
        uint256 _minSeedLiquidity
    ) BuckBasketProRata(_buck, _controller, _v3Factory, _governance,
                        _defaultFeeTier, _twapWindow, _observationCardinality,
                        _defaultMaxDeviationBp, _minSeedLiquidity) {}

    function _deskNav() internal view override returns (uint256) {
        (, , uint256 B, ) = _venue().poolBuckValues();
        return 2 * B;
    }

    function _deskNavSafe() internal view override returns (uint256) {
        try _venue().poolBuckValues()
            returns (uint256[] memory, uint128[] memory, uint256 B, uint256[] memory)
        {
            return 2 * B;
        } catch {
            return 0;
        }
    }

    function _deskPrices() internal view override returns (uint256[] memory prices) {
        (, , , prices) = _venue().poolBuckValues();
    }
}
