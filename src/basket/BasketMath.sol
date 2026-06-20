// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Math}               from "@openzeppelin/contracts/utils/math/Math.sol";
import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";

/// @title BasketMath -- pure helpers for the BuckBasket pro-rata model.
///
/// @notice Stateless math extracted from the core so it can be unit-tested
///         without a chain.  Pool-state reads stay in the core; only
///         price-/share-/tick-arithmetic lives here.
library BasketMath {

    int24 internal constant MIN_TICK = -887272;
    int24 internal constant MAX_TICK =  887272;

    /// @notice `amount * num / den` with full-width intermediate (no overflow).
    function proRata(uint256 amount, uint256 num, uint256 den)
        internal pure returns (uint256)
    {
        return UniswapV3OracleLib.mulDiv(amount, num, den);
    }

    /// @notice Split BUCK `profit` into the treasury's cut and the
    ///         depositor's cut.  `treasuryBp` is basis points to treasury
    ///         (default 5000 = 50/50).  Depositor takes the remainder so the
    ///         two always sum to `profit` exactly (no rounding leak).
    function splitProfit(uint256 profit, uint16 treasuryBp)
        public pure returns (uint256 toTreasury, uint256 toDepositor)
    {
        toTreasury  = profit * treasuryBp / 10000;
        toDepositor = profit - toTreasury;
    }

    /// @notice Full-range tick bounds snapped to a pool's tick spacing.
    function fullRangeTicks(int24 spacing)
        public pure returns (int24 lower, int24 upper)
    {
        lower = (MIN_TICK / spacing) * spacing;
        upper = (MAX_TICK / spacing) * spacing;
    }

    /// @notice sqrtPriceX96 that makes a pool quote 1 whole TOKEN for
    ///         `priceInBuck` whole BUCK, honouring address ordering.
    function sqrtPriceFromBuckRate(
        bool    buckIsToken0,
        uint256 priceInBuck,        // 18-dec; BUCK per 1 whole TOKEN
        uint8   tokenDecimals
    ) public pure returns (uint160) {
        uint256 buckRaw  = priceInBuck;           // 18-dec
        uint256 tokenRaw = 10 ** tokenDecimals;   // raw
        uint256 amount0  = buckIsToken0 ? buckRaw  : tokenRaw;
        uint256 amount1  = buckIsToken0 ? tokenRaw : buckRaw;
        uint256 ratioX192 = UniswapV3OracleLib.mulDiv(amount1, 1 << 192, amount0);
        uint256 sqrtRoot  = Math.sqrt(ratioX192);
        require(sqrtRoot <= type(uint160).max, "sqrtP:overflow");
        return uint160(sqrtRoot);
    }

    // --- External V3-math wrappers ---------------------------------------- //
    //
    // These are `public` (not `internal`) on purpose: the heavy V3 tick / L
    // arithmetic from UniswapV3OracleLib then lives in this *separately
    // deployed* library and is reached via DELEGATECALL, instead of being
    // inlined into the (size-constrained) basket.  `mulDiv` stays inline (tiny,
    // hot).

    function getSqrtRatioAtTick(int24 tick) public pure returns (uint160) {
        return UniswapV3OracleLib.getSqrtRatioAtTick(tick);
    }

    function getQuoteAtTick(int24 tick, uint128 baseAmount, address baseToken, address quoteToken)
        public pure returns (uint256)
    {
        return UniswapV3OracleLib.getQuoteAtTick(tick, baseAmount, baseToken, quoteToken);
    }

    function consult(address pool, uint32 secondsAgo) public view returns (int24) {
        return UniswapV3OracleLib.consult(pool, secondsAgo);
    }

    function getLiquidityForAmounts(
        uint160 sqrtP, uint160 sqrtA, uint160 sqrtB, uint256 amount0, uint256 amount1
    ) public pure returns (uint128) {
        return UniswapV3OracleLib.getLiquidityForAmounts(sqrtP, sqrtA, sqrtB, amount0, amount1);
    }

    function getLiquidityForAmount0(uint160 sqrtA, uint160 sqrtB, uint256 amount0)
        public pure returns (uint128)
    {
        return UniswapV3OracleLib.getLiquidityForAmount0(sqrtA, sqrtB, amount0);
    }

    function getLiquidityForAmount1(uint160 sqrtA, uint160 sqrtB, uint256 amount1)
        public pure returns (uint128)
    {
        return UniswapV3OracleLib.getLiquidityForAmount1(sqrtA, sqrtB, amount1);
    }

    function getAmount0ForLiquidity(uint160 sqrtA, uint160 sqrtB, uint128 liquidity)
        public pure returns (uint256)
    {
        return UniswapV3OracleLib.getAmount0ForLiquidity(sqrtA, sqrtB, liquidity);
    }

    function getAmount1ForLiquidity(uint160 sqrtA, uint160 sqrtB, uint128 liquidity)
        public pure returns (uint256)
    {
        return UniswapV3OracleLib.getAmount1ForLiquidity(sqrtA, sqrtB, liquidity);
    }
}
