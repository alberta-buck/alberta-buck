// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {UniswapV3OracleLib} from "../../src/lib/UniswapV3OracleLib.sol";

interface IUniswapV3Factory {
    function createPool(address tokenA, address tokenB, uint24 fee) external returns (address pool);
    function feeAmountTickSpacing(uint24 fee) external view returns (int24);
}

interface IUniswapV3MintCallback {
    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata data) external;
}

interface IUniswapV3SwapCallback {
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external;
}

/// @notice Local Uniswap V3 deployment helpers shared by BuckKController tests.
///
/// `setUpV3()` deploys a fresh `UniswapV3Factory` via `vm.deployCode` so each
/// test starts from a clean factory state.  `_createAndInitPool` initializes a
/// pool at a target spot price; `_mintFullRange` seeds liquidity; `_moveSpotTo`
/// drives the pool spot price to a chosen sqrtPriceX96 by swapping against
/// the existing liquidity.
///
/// Mint and swap callbacks just push the owed tokens from this fixture's
/// balance to the calling pool.  Fixture lacks per-pool authentication
/// (callbacks accept any caller), which is fine in tests but unsafe in prod.
abstract contract UniswapV3Fixture is Test, IUniswapV3MintCallback, IUniswapV3SwapCallback {
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

    /// @dev Create + initialize a pool for `(base, quote)` at fee tier `fee`
    ///      so 1 base unit costs `quoteAmt`/`baseAmt` quote units.
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

    /// @dev Mint a full-range liquidity position into `pool` for this fixture
    ///      account.  Caller MUST have already minted enough of token0 and
    ///      token1 to this fixture's balance (the callback transfers them
    ///      to the pool).  `liquidity` is the V3 L value, NOT a raw token
    ///      amount; both token amounts are derived by the pool from L and
    ///      the current sqrtPrice.
    function _mintFullRange(address pool, uint128 liquidity) internal {
        int24 spacing  = IUniswapV3Pool(pool).tickSpacing();
        // Largest in-range usable ticks for the spacing.
        int24 minTick  = (UniswapV3OracleLib.MIN_TICK / spacing) * spacing;
        int24 maxTick  = (UniswapV3OracleLib.MAX_TICK / spacing) * spacing;

        bytes memory data = abi.encode(IUniswapV3Pool(pool).token0(), IUniswapV3Pool(pool).token1());
        IUniswapV3Pool(pool).mint(address(this), minTick, maxTick, liquidity, data);
    }

    /// @dev Bump the pool's `observationCardinalityNext` so it can store
    ///      enough observations to satisfy a TWAP consult of `secondsAgo`.
    function _bumpCardinality(address pool, uint16 next) internal {
        IUniswapV3Pool(pool).increaseObservationCardinalityNext(next);
    }

    /// @dev Write an oracle observation without moving the pool's spot price.
    ///      `_modifyPosition` writes an observation whenever the current tick
    ///      is in-range; calling `burn(MIN..MAX, 0)` on a full-range position
    ///      this fixture already owns is the cheapest way to "touch".
    function _touchPool(address pool) internal {
        int24 spacing = IUniswapV3Pool(pool).tickSpacing();
        int24 minTick = (UniswapV3OracleLib.MIN_TICK / spacing) * spacing;
        int24 maxTick = (UniswapV3OracleLib.MAX_TICK / spacing) * spacing;
        IUniswapV3Pool(pool).burn(minTick, maxTick, 0);
    }

    /// @dev Walk forward `secondsAgo` seconds in `nTouches` steps, writing an
    ///      observation each step.  After this completes, `consult(pool,
    ///      secondsAgo)` returns a real time-weighted mean (not just the
    ///      "extrapolate from latest" degenerate path).  Pre-condition:
    ///      this fixture owns a full-range position on `pool`.
    function _warmupTwap(address pool, uint32 secondsAgo, uint8 nTouches) internal {
        require(nTouches > 0, "warmup:nTouches=0");
        uint256 step = uint256(secondsAgo) / nTouches + 1;
        for (uint8 i = 0; i < nTouches; i++) {
            vm.warp(block.timestamp + step);
            _touchPool(pool);
        }
    }

    /// @dev Drive `pool`'s spot price to `targetSqrtX96` by swapping into the
    ///      pool until it reaches the target.  Caller must pre-mint the
    ///      input token; we accept "huge" amount-specified and rely on
    ///      sqrtPriceLimit to stop the swap at the target.
    function _moveSpotTo(address pool, uint160 targetSqrtX96) internal {
        (uint160 cur,,,,,,) = IUniswapV3Pool(pool).slot0();
        if (cur == targetSqrtX96) return;
        bool zeroForOne = targetSqrtX96 < cur;

        bytes memory data = abi.encode(IUniswapV3Pool(pool).token0(), IUniswapV3Pool(pool).token1());
        IUniswapV3Pool(pool).swap(
            address(this),
            zeroForOne,
            type(int128).max,   // huge "exact-input"; capped by sqrtPriceLimit
            targetSqrtX96,
            data
        );
    }

    /// @dev Drive `pool` so that 1 base unit quotes at `quoteAmt` quote units.
    function _moveSpotToPrice(
        address pool,
        address base, uint256 baseAmt,
        address quote, uint256 quoteAmt
    ) internal {
        _moveSpotTo(pool, _sqrtPriceX96(base, baseAmt, quote, quoteAmt));
    }

    // sqrtRatio bounds copied from Uniswap V3 TickMath (canonical mainnet pool init values)
    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342;

    /// @dev Swap `amountIn` of `tokenIn` into `pool`, receiving `tokenOut`.
    ///      Uses an extreme sqrtPriceLimit so the entire input is consumed.
    function _swapExactInput(address pool, address tokenIn, address tokenOut, uint256 amountIn)
        internal returns (uint256 amountOut)
    {
        address t0 = IUniswapV3Pool(pool).token0();
        address t1 = IUniswapV3Pool(pool).token1();
        bool zeroForOne = tokenIn == t0;
        require(zeroForOne || tokenIn == t1, "swap:invalid tokenIn");

        uint160 sqrtLimit = zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1;
        bytes memory data = abi.encode(t0, t1);

        uint256 outBefore = IERC20(tokenOut).balanceOf(address(this));
        IUniswapV3Pool(pool).swap(
            address(this),
            zeroForOne,
            int256(amountIn),
            sqrtLimit,
            data
        );
        amountOut = IERC20(tokenOut).balanceOf(address(this)) - outBefore;
    }

    /// @dev Swap into `pool` until exactly `amountOut` of `tokenOut` is received.
    ///      Returns the actual `tokenIn` amount consumed.
    function _swapExactOutput(address pool, address tokenIn, address tokenOut, uint256 amountOut)
        internal returns (uint256 amountIn)
    {
        address t0 = IUniswapV3Pool(pool).token0();
        address t1 = IUniswapV3Pool(pool).token1();
        bool zeroForOne = tokenIn == t0;
        require(zeroForOne || tokenIn == t1, "swap:invalid tokenIn");

        uint160 sqrtLimit = zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1;
        bytes memory data = abi.encode(t0, t1);

        uint256 inBefore = IERC20(tokenIn).balanceOf(address(this));
        IUniswapV3Pool(pool).swap(
            address(this),
            zeroForOne,
            -int256(amountOut),
            sqrtLimit,
            data
        );
        amountIn = inBefore - IERC20(tokenIn).balanceOf(address(this));
    }

    // -------------------------------------------------------------------- //
    //  V3 callbacks                                                         //
    //  Both unchecked w.r.t. msg.sender -- safe in tests only.              //
    // -------------------------------------------------------------------- //

    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata data) external override {
        (address t0, address t1) = abi.decode(data, (address, address));
        if (amount0Owed > 0) IERC20(t0).transfer(msg.sender, amount0Owed);
        if (amount1Owed > 0) IERC20(t1).transfer(msg.sender, amount1Owed);
    }

    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external override {
        (address t0, address t1) = abi.decode(data, (address, address));
        if (amount0Delta > 0) IERC20(t0).transfer(msg.sender, uint256(amount0Delta));
        if (amount1Delta > 0) IERC20(t1).transfer(msg.sender, uint256(amount1Delta));
    }
}
