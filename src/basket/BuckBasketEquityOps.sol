// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketEquity}    from "./BuckBasketEquity.sol";
import {MonetaryDesk}        from "./MonetaryDesk.sol";

/// @title BuckBasketEquityOps -- the equity basket plus the monetary desk.
///
/// @notice doc/BASKET-EQUITY.org 10.1 and 13.7, step 5.  The desk
///         (MonetaryDesk) on the equity shell: NAV is the equity at the TWAP
///         marks, prices the TWAP.  The desk's book is plain balances, and the
///         equity basket counts only its own explicit books (the wallet, the
///         liquidity it placed), so the desk's inventory is outside every
///         receipt's claim by construction, as it was on the pro-rata shell.
contract BuckBasketEquityOps is BuckBasketEquity, MonetaryDesk {

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
    ) BuckBasketEquity(_buck, _controller, _v3Factory, _governance,
                       _defaultFeeTier, _twapWindow, _observationCardinality,
                       _defaultMaxDeviationBp, _minSeedLiquidity) {}

    function _deskNav() internal view override returns (uint256 nav) {
        nav = _equity(MARK_TWAP);
        if (nav == 0) revert NoValue();
    }

    function _deskNavSafe() internal view override returns (uint256) {
        return _equity(MARK_TWAP);
    }

    function _deskPrices() internal view override returns (uint256[] memory prices) {
        uint256 n = constituents.length;
        prices = new uint256[](n);
        for (uint256 i = 0; i < n; i++) prices[i] = _marks(i).pTwap;
    }
}
