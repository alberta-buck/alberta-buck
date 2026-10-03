// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BuckBasketUniswapV3} from "./BuckBasketUniswapV3.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";

/// @notice Venue for the replacement machine. Missing TWAP history is an
///         unavailable price, including on K's raw reference path. Pools
///         must be seeded and aged before the basket accepts valued entries.
contract ValidatedBasketVenue is BuckBasketUniswapV3 {
    error UnavailablePrice();

    function basketValueInBuck() public view override returns (int256) {
        if (twapWindow == 0) revert UnavailablePrice();
        return super.basketValueInBuck();
    }

    function _valuationTick(Constituent storage c, int24) internal view override returns (int24) {
        if (twapWindow == 0) revert UnavailablePrice();
        return UniswapV3OracleLib.consult(c.pool, twapWindow);
    }

    function _readPoolPrice(Constituent storage c, uint32 window)
        internal view override returns (uint256)
    {
        int24 tick;
        if (window == 0) (, tick,,,,,) = IUniswapV3Pool(c.pool).slot0();
        else {
            if (IUniswapV3Pool(c.pool).liquidity() == 0) revert UnavailablePrice();
            tick = UniswapV3OracleLib.consult(c.pool, window);
        }
        return UniswapV3OracleLib.getQuoteAtTick(tick, uint128(10 ** c.decimals), c.token, address(buck));
    }

    function positionMint(uint256 i, uint256 tokenAmount, uint256 buckAmount)
        public override onlySelf returns (uint128 liquidity, uint256 tokenUsed, uint256 buckUsed)
    {
        Constituent storage c = constituents[i];
        if (twapWindow == 0 || defaultMaxDeviationBp == 0 || defaultMaxDeviationBp > 1000) revert UnavailablePrice();
        _checkSpot(c, _readPoolPrice(c, twapWindow), defaultMaxDeviationBp);
        return super.positionMint(i, tokenAmount, buckAmount);
    }

    /// @dev Both execution price and terminal spot must stay within the
    ///      configured TWAP band. A breached bound rolls back the whole step,
    ///      including callbacks, burns and books. Fees are inside the bound.
    function monetaryLeg(uint256 i, bool sellBuck, uint256 amountIn)
        public override onlySelf returns (uint256 spent, uint256 received)
    {
        if (amountIn == 0) return (0, 0);
        Constituent storage c = constituents[i];
        if (twapWindow == 0 || IUniswapV3Pool(c.pool).liquidity() == 0) revert UnavailablePrice();
        uint256 tolerance = defaultMaxDeviationBp;
        if (tolerance == 0 || tolerance > 1000) revert Slippage();
        uint256 p = _readPoolPrice(c, twapWindow);
        _checkSpot(c, p, tolerance);
        (spent, received) = super.monetaryLeg(i, sellBuck, amountIn);
        uint256 fairOut = sellBuck
            ? UniswapV3OracleLib.mulDiv(spent, 10 ** c.decimals, p)
            : UniswapV3OracleLib.mulDiv(spent, p, 10 ** c.decimals);
        if (received < fairOut * (10000 - tolerance) / 10000) revert Slippage();
        _checkSpot(c, p, tolerance);
    }

    function _checkSpot(Constituent storage c, uint256 p, uint256 tolerance) internal view {
        uint256 spot = _readPoolPrice(c, 0);
        uint256 delta = spot > p ? spot - p : p - spot;
        if (p == 0 || delta * 10000 > p * tolerance) revert Slippage();
    }
}
