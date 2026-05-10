// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {UniswapV3OracleLib} from "../../src/lib/UniswapV3OracleLib.sol";

interface IUniswapV3Factory {
    function createPool(address tokenA, address tokenB, uint24 fee) external returns (address pool);
    function feeAmountTickSpacing(uint24 fee) external view returns (int24);
}

/// @notice Local Uniswap V3 deployment helpers shared by BuckKController tests.
///
/// `setUpV3()` deploys a fresh `UniswapV3Factory` via `vm.deployCode` so each
/// test starts from a clean factory state.  `_createAndInitPool` creates a
/// pool at a target price expressed as token-amount pairs, sidestepping the
/// need for callers to compute sqrtPriceX96 themselves.  This first-pass
/// fixture is read-only -- pools are initialized but no liquidity is minted,
/// since slot0() spot-price reads work on a freshly initialized pool.
abstract contract UniswapV3Fixture is Test {
    address public v3Factory;

    function setUpV3() internal {
        v3Factory = deployCode("out/UniswapV3Factory.sol/UniswapV3Factory.json");
    }

    /// @dev Compute sqrtPriceX96 such that pool spot price equals
    ///      `quoteAmt`/`baseAmt` (in the tokens' native decimals).
    ///      sqrtPriceX96 = sqrt(amount1 * 2^192 / amount0)
    function _sqrtPriceX96(
        address base, uint256 baseAmt,
        address quote, uint256 quoteAmt
    ) internal pure returns (uint160) {
        bool baseIsToken0 = base < quote;
        uint256 amt0 = baseIsToken0 ? baseAmt  : quoteAmt;
        uint256 amt1 = baseIsToken0 ? quoteAmt : baseAmt;

        uint256 ratioX192 = UniswapV3OracleLib.mulDiv(amt1, 1 << 192, amt0);
        uint256 sqrtRoot  = Math.sqrt(ratioX192);
        require(sqrtRoot <= type(uint160).max, "sqrtP:overflow");
        return uint160(sqrtRoot);
    }

    /// @dev Create a pool for `(base, quote)` at fee tier `fee` and initialize
    ///      it so 1 base unit costs `quoteAmt`/`baseAmt` quote units.
    function _createAndInitPool(
        address base, uint256 baseAmt,
        address quote, uint256 quoteAmt,
        uint24 fee
    ) internal returns (address pool) {
        (address t0, address t1) = base < quote ? (base, quote) : (quote, base);
        pool = IUniswapV3Factory(v3Factory).createPool(t0, t1, fee);
        require(pool != address(0), "fixture:pool=0");
        uint160 sqrtP = _sqrtPriceX96(base, baseAmt, quote, quoteAmt);
        IUniswapV3Pool(pool).initialize(sqrtP);
    }
}
