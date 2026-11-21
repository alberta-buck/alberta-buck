// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IUniswapV3Pool}     from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math}               from "@openzeppelin/contracts/utils/math/Math.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {CarryRatio}         from "../lib/CarryRatio.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {BuckBasketStorage, IUniswapV3MintCallback,
        IUniswapV3SwapCallback} from "./BuckBasketStorage.sol";

/// @title BuckBasketUniswapV3 -- the Uniswap V3 venue facet for a BuckBasket.
///
/// @notice The AMM-specific half of the basket.  Deployed once and reached by
///         the shell (`BuckBasketEquity`, and the desk) via `delegatecall`, so every method
///         runs in the shell's storage context (`address(this)` is the basket,
///         all custody stays at the shell).  Implements the venue seam
///         (`IBuckBasketVenue`) plus the V3 mint/swap callbacks; the heavy V3
///         tick/L/sqrt arithmetic is plain inline `UniswapV3OracleLib` here (the
///         facet has its own 24 KB budget), so the shell carries none of it.
///
/// @dev    Mutating venue methods are `onlySelf`: they may only be invoked by the
///         basket itself (the shell's `IBuckBasketVenue(address(this)).fn(...)`
///         self-call, routed in by the shell fallback) -- never directly through
///         the fallback by an external caller, which would corrupt accounting.
///         The callbacks instead authenticate the calling pool via the shared
///         `_callbackPool` / `_swapCallbackPool` guards.
/// @dev The basket's and the desk's own flag, read by the venue they delegatecall.
interface INetAccountingShell {
    function netAccounting() external view returns (bool);
}

contract BuckBasketUniswapV3 is
    BuckBasketStorage, IBuckBasketVenue, IUniswapV3MintCallback, IUniswapV3SwapCallback
{
    int24 internal constant MIN_TICK = -887272;
    int24 internal constant MAX_TICK =  887272;

    /// @dev Only the basket itself (a routed self-call) may drive venue mutations.
    modifier onlySelf() {
        if (msg.sender != address(this)) revert NotSelf();
        _;
    }

    // --- Pool setup ------------------------------------------------------- //

    /// @notice Register an equity constituent through the shell's governed
    ///         addBasketToken self-call. Registration belongs beside pool setup,
    ///         rather than consuming the share/credit shell's runtime budget.
    /// @dev Pre: only the shell can invoke this mutator; its public wrapper
    ///      authorizes governance. The guards and weighting rules are identical
    ///      to the former shell implementation. Post: append one pool and its
    ///      index, renormalize weights/quantities at original price anchors and
    ///      reprime K. No shares, wallet cash or credit principal are changed.
    function registerToken(address token, uint8 decimals, uint256 initialPriceInBuck,
                           uint256 weightBp, uint24 feeTier)
        external override onlySelf returns (address pool)
    {
        if (!(token != address(0) && token != address(buck))) revert BadToken();
        if (!(weightBp <= 10000)) revert BadWeight();
        if (!(indexOf[token] == 0)) revert AlreadyPresent();
        if (!(initialPriceInBuck > 0)) revert BadPrice();

        uint256 N = constituents.length + 1;
        uint256 newW = weightBp > 0 ? weightBp : 10000 / N;
        uint256 oldW = 10000 - newW;
        uint256 oldSumW = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            c.targetWeightBp = UniswapV3OracleLib.mulDiv(c.targetWeightBp, oldW, 10000);
            if (!(c.targetWeightBp > 0 && c.targetWeightBp < 10000)) revert InvalidRescale();
            oldSumW += c.targetWeightBp;
            // Quantities must use the same rounded weights as the new leg.
            // Scaling the old quantity by oldW while assigning the rounding
            // remainder to newW makes the reference cost exceed one BUCK.
            // Retain the original anchor price, never today's pool price.
            c.basketAmount = UniswapV3OracleLib.mulDiv(
                c.targetWeightBp * 1e14, 1e18, c.initialPriceInBuck);
        }
        if (constituents.length > 0) newW = 10000 - oldSumW;

        uint256 basketAmount = UniswapV3OracleLib.mulDiv(
            UniswapV3OracleLib.mulDiv(newW, 1e18, 10000), 1e18, initialPriceInBuck);
        (address p, int24 lo, int24 hi, bool b0) =
            _setupPool(token, decimals, initialPriceInBuck, feeTier);
        pool = p;
        constituents.push(Constituent({
            token: token, decimals: decimals, basketAmount: basketAmount,
            initialPriceInBuck: initialPriceInBuck, feeTier: feeTier, pool: p,
            tickLower: lo, tickUpper: hi, buckIsToken0: b0, targetWeightBp: newW,
            treasuryLiquidity: 0
        }));
        indexOf[token] = constituents.length;
        controller.reprime();
        emit BasketTokenAdded(token, newW, initialPriceInBuck, p);
    }

    function _setupPool(address token, uint8 decimals, uint256 initialPriceInBuck, uint24 feeTier)
        internal
        returns (address pool, int24 tickLower, int24 tickUpper, bool buckIsToken0)
    {
        pool = _findOrCreatePool(token, feeTier);
        buckIsToken0 = address(buck) < token;
        uint160 sqrtPriceX96 = _sqrtPriceFromBuckRate(buckIsToken0, initialPriceInBuck, decimals);
        try IUniswapV3Pool(pool).initialize(sqrtPriceX96) {} catch {}
        IUniswapV3Pool(pool).increaseObservationCardinalityNext(observationCardinality);

        int24 spacing = v3Factory.feeAmountTickSpacing(feeTier);
        (tickLower, tickUpper) = _fullRangeTicks(spacing);
    }

    // --- Value reads ------------------------------------------------------ //

    function basketValueInBuck() public view virtual override returns (int256) {
        if (constituents.length == 0) return int256(1e18);
        int256 total = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 priceInBuck = _readPoolPrice(c, twapWindow);
            total += int256(UniswapV3OracleLib.mulDiv(c.basketAmount, priceInBuck, 1e18));
        }
        return total;
    }

    // --- The work wheel ------------------------------------------------------ //
    //
    // Hosted here rather than in the shell for the shell's size budget: reached through the shell's
    // fallback like every venue entry point, msg.sender preserved by the delegatecall.

    function setWheel(address w) external {
        if (!(msg.sender == governance)) revert NotGovernance();
        wheel = w;
        emit WheelSet(w);
    }

    // --- Equity primitives (BuckBasketEquity) ------------------------------ //

    /// @inheritdoc IBuckBasketVenue
    /// @dev In NET BUCK when the shell keeps net accounting (canonical Buck):
    ///      V3's prices and position amounts are in the pool's nominal BUCK,
    ///      and the pool is a Carrying account, so every field is taken at its
    ///      net / gross (lib/CarryRatio.sol); a pool with no BUCK marks
    ///      nothing. A legacy shell reads the quoted marks. This is the one
    ///      place the basket's and the desk's marks are netted.
    function marks(uint256 i, uint128 liquidity)
        external view override returns (Marks memory m)
    {
        m = _quotedMarks(i, liquidity);
        if (!_netShell()) return m;
        (uint256 g, uint256 n) = CarryRatio.read(address(buck), constituents[i].pool);
        if (g == 0) return Marks(0, 0, 0, 0, 0, 0, 0);
        if (g == n) return m;
        m.posLow = CarryRatio.toNet(g, n, m.posLow);
        m.posTwap = CarryRatio.toNet(g, n, m.posTwap);
        m.posHigh = CarryRatio.toNet(g, n, m.posHigh);
        m.pLow = CarryRatio.toNet(g, n, m.pLow);
        m.pTwap = CarryRatio.toNet(g, n, m.pTwap);
        m.pHigh = CarryRatio.toNet(g, n, m.pHigh);
        m.depth = CarryRatio.toNet(g, n, m.depth);
    }

    /// @dev Whether the shell this venue serves (it runs by delegatecall, so
    ///      address(this) is the basket or the desk) keeps net accounting. A
    ///      shell without the flag (the pro-rata basket) is legacy.
    function _netShell() internal view returns (bool) {
        try INetAccountingShell(address(this)).netAccounting() returns (bool net) { return net; } catch { return false; }
    }

    /// @dev The marks as V3 quotes them, in the pool's nominal BUCK.
    function _quotedMarks(uint256 i, uint128 liquidity) internal view returns (Marks memory m) {
        Constituent storage c = constituents[i];
        (uint160 sSpot, int24 tSpot,,,,,) = IUniswapV3Pool(c.pool).slot0();
        int24 tTwap = _valuationTick(c, tSpot);
        uint160 sTwap = UniswapV3OracleLib.getSqrtRatioAtTick(tTwap);
        uint128 one = uint128(10 ** c.decimals);
        uint256 pSpot = UniswapV3OracleLib.getQuoteAtTick(tSpot, one, c.token, address(buck));
        m.pTwap = UniswapV3OracleLib.getQuoteAtTick(tTwap, one, c.token, address(buck));
        (m.pHigh, m.pLow) = pSpot > m.pTwap ? (pSpot, m.pTwap) : (m.pTwap, pSpot);
        uint128 L = IUniswapV3Pool(c.pool).liquidity();
        if (L > 0) {
            m.depth = c.buckIsToken0 ? UniswapV3OracleLib.mulDiv(uint256(L), 1 << 96, sSpot)
                                     : UniswapV3OracleLib.mulDiv(uint256(L), sSpot, 1 << 96);
        }
        if (liquidity == 0) return m;
        uint256 vSpot = 2 * _buckSideAt(c, liquidity, sSpot);
        m.posTwap = 2 * _buckSideAt(c, liquidity, sTwap);
        (m.posHigh, m.posLow) = vSpot > m.posTwap ? (vSpot, m.posTwap) : (m.posTwap, vSpot);
    }

    /// @dev Legacy deployments permit cold-pool marks. The validated venue
    ///      overrides this hook and requires the complete TWAP history.
    function _valuationTick(Constituent storage c, int24 spot) internal view virtual returns (int24) {
        try this.consultTickExternal(c.pool, twapWindow) returns (int24 t) { return t; }
        catch { return spot; }
    }

    function positionMint(uint256 i, uint256 tokenAmount, uint256 buckAmount)
        public virtual override onlySelf
        returns (uint128 liquidity, uint256 tokenUsed, uint256 buckUsed)
    {
        Constituent storage c = constituents[i];
        liquidity = _liquidityForAmounts(c, tokenAmount, buckAmount);
        if (liquidity == 0) return (0, 0, 0);
        _callbackPool = c.pool;
        (uint256 a0, uint256 a1) = IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, liquidity, abi.encode(c.token));
        _callbackPool = address(0);
        (buckUsed, tokenUsed) = c.buckIsToken0 ? (a0, a1) : (a1, a0);
    }

    function positionBurn(uint256 i, uint128 liquidity)
        external override onlySelf returns (uint256 tokenOut, uint256 buckOut)
    {
        Constituent storage c = constituents[i];
        if (liquidity == 0) return (0, 0);
        (uint256 p0, uint256 p1) =
            IUniswapV3Pool(c.pool).burn(c.tickLower, c.tickUpper, liquidity);
        (uint128 a0, uint128 a1) = IUniswapV3Pool(c.pool).collect(
            address(this), c.tickLower, c.tickUpper, uint128(p0), uint128(p1));
        (buckOut, tokenOut) = c.buckIsToken0 ? (uint256(a0), uint256(a1))
                                             : (uint256(a1), uint256(a0));
    }

    function positionSync(uint256 i)
        external override onlySelf returns (uint256 tokenOut, uint256 buckOut)
    {
        Constituent storage c = constituents[i];
        if (_positionLiquidity(c) > 0) {
            IUniswapV3Pool(c.pool).burn(c.tickLower, c.tickUpper, 0);   // accrue fees owed
        }
        (uint128 a0, uint128 a1) = IUniswapV3Pool(c.pool).collect(
            address(this), c.tickLower, c.tickUpper, type(uint128).max, type(uint128).max);
        (buckOut, tokenOut) = c.buckIsToken0 ? (uint256(a0), uint256(a1))
                                             : (uint256(a1), uint256(a0));
    }

    /// @notice The BUCK side of `liquidity` of the full-range position at
    ///         `sqrtP` (clamped to the range).
    function _buckSideAt(Constituent storage c, uint128 liquidity, uint160 sqrtP)
        internal view returns (uint256)
    {
        uint160 lo = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickLower);
        uint160 hi = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickUpper);
        if (c.buckIsToken0) {
            if (sqrtP >= hi) return 0;
            return UniswapV3OracleLib.getAmount0ForLiquidity(sqrtP > lo ? sqrtP : lo, hi, liquidity);
        }
        if (sqrtP <= lo) return 0;
        return UniswapV3OracleLib.getAmount1ForLiquidity(lo, sqrtP < hi ? sqrtP : hi, liquidity);
    }

    // --- V3 callbacks (authenticated by pool, not onlySelf) --------------- //

    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata data)
        external override
    {
        if (!(msg.sender == _callbackPool)) revert BadCallback();
        address token = abi.decode(data, (address));
        Constituent storage c = constituents[indexOf[token] - 1];
        if (c.buckIsToken0) {
            if (amount0Owed > 0) IERC20(address(buck)).transfer(msg.sender, amount0Owed);
            if (amount1Owed > 0) IERC20(c.token).transfer(msg.sender, amount1Owed);
        } else {
            if (amount0Owed > 0) IERC20(c.token).transfer(msg.sender, amount0Owed);
            if (amount1Owed > 0) IERC20(address(buck)).transfer(msg.sender, amount1Owed);
        }
    }

    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata)
        external override
    {
        if (!(msg.sender == _swapCallbackPool)) revert BadSwapCallback();
        // Authority comes from the active authenticated swap, not user data.
        // This also supports TOKEN/USDC and BUCK/USDC route legs.
        IUniswapV3Pool pool = IUniswapV3Pool(msg.sender);
        if (amount0Delta > 0) IERC20(pool.token0()).transfer(msg.sender, uint256(amount0Delta));
        if (amount1Delta > 0) IERC20(pool.token1()).transfer(msg.sender, uint256(amount1Delta));
    }

    function arbSwap(address pool, address input, address output, uint256 amount)
        external override onlySelf returns (uint256 received)
    {
        IUniswapV3Pool p = IUniswapV3Pool(pool);
        bool zeroForOne = p.token0() == input;
        if (amount == 0 || amount > uint256(type(int256).max)
            || (zeroForOne ? p.token1() != output : p.token1() != input || p.token0() != output)) revert BadToken();
        uint256 beforeOut = IERC20(output).balanceOf(address(this));
        uint256 beforeIn = IERC20(input).balanceOf(address(this));
        address previous = _swapCallbackPool;
        _swapCallbackPool = pool;
        p.swap(address(this), zeroForOne, int256(amount),
               zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1, "");
        _swapCallbackPool = previous;
        if (beforeIn - IERC20(input).balanceOf(address(this)) != amount) revert SwapDeltaSign();
        received = IERC20(output).balanceOf(address(this)) - beforeOut;
        if (received == 0) revert NoValue();
    }

    // --- Swap helpers ----------------------------------------------------- //

    /// @inheritdoc IBuckBasketVenue
    ///
    /// @dev The per-leg size bound lives in the shell, but note WHY one is
    ///      needed at all beyond good behaviour: `poolBuckValues` runs
    ///      `_enforceSlippageGuard` on every redemption, so a monetary swap
    ///      big enough to push spot off TWAP would revert every depositor
    ///      exit until the window caught up.  The basket must not be able to
    ///      brick its own redemption path.
    function monetaryLeg(uint256 i, bool sellBuck, uint256 amountIn)
        public virtual override onlySelf returns (uint256 spent, uint256 received)
    {
        if (amountIn == 0) return (0, 0);
        Constituent storage c = constituents[i];
        return sellBuck ? _swapBuckForTokenExactIn(c, amountIn)
                        : _swapTokenForBuckExactIn(c, amountIn);
    }

    function _swapTokenForBuckExactIn(Constituent storage c, uint256 tokenIn)
        internal returns (uint256 spent, uint256 received)
    {
        if (!(tokenIn > 0)) revert TokenIn0();
        _swapCallbackPool = c.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(c.pool).swap(
            address(this),
            !c.buckIsToken0,                                 // zeroForOne for TOKEN->BUCK
            int256(tokenIn),                                 // positive ⇒ exact-input
            c.buckIsToken0 ? MAX_SQRT_RATIO - 1 : MIN_SQRT_RATIO + 1,
            abi.encode(c.token)
        );
        _swapCallbackPool = address(0);
        int256 buckDelta = c.buckIsToken0 ? d0 : d1;
        int256 tokDelta  = c.buckIsToken0 ? d1 : d0;
        if (!(buckDelta <= 0 && tokDelta >= 0)) revert SwapDeltaSign();
        spent    = uint256(tokDelta);
        received = uint256(-buckDelta);
    }

    function _swapBuckForTokenExactIn(Constituent storage c, uint256 buckIn)
        internal returns (uint256 spent, uint256 received)
    {
        if (!(buckIn > 0)) revert BuckIn0();
        _swapCallbackPool = c.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(c.pool).swap(
            address(this),
            c.buckIsToken0,                                  // zeroForOne for BUCK->TOKEN
            int256(buckIn),                                  // positive ⇒ exact-input
            c.buckIsToken0 ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1,
            abi.encode(c.token)
        );
        _swapCallbackPool = address(0);
        int256 buckDelta = c.buckIsToken0 ? d0 : d1;
        int256 tokDelta  = c.buckIsToken0 ? d1 : d0;
        if (!(buckDelta >= 0 && tokDelta <= 0)) revert SwapDeltaSign();
        spent    = uint256(buckDelta);
        received = uint256(-tokDelta);
    }

    // --- Pool math / reads ------------------------------------------------ //

    /// @notice V3 liquidity for (tokenAmount, buckAmount) at the current price --
    ///         min of the two sides (leftover stays in the basket).
    function _liquidityForAmounts(Constituent storage c, uint256 tokenAmount, uint256 buckAmount)
        internal view returns (uint128)
    {
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(c.pool).slot0();
        uint160 sqrtLow  = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickLower);
        uint160 sqrtHigh = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickUpper);
        (uint256 amount0, uint256 amount1) = c.buckIsToken0
            ? (buckAmount, tokenAmount)
            : (tokenAmount, buckAmount);
        return UniswapV3OracleLib.getLiquidityForAmounts(sqrtP, sqrtLow, sqrtHigh, amount0, amount1);
    }

    function _findOrCreatePool(address token, uint24 feeTier) internal returns (address pool) {
        pool = v3Factory.getPool(address(buck), token, feeTier);
        if (pool == address(0)) {
            pool = v3Factory.createPool(address(buck), token, feeTier);
        }
    }

    function _positionLiquidity(Constituent storage c) internal view returns (uint128 liquidity) {
        (liquidity,,,,) = IUniswapV3Pool(c.pool).positions(
            keccak256(abi.encodePacked(address(this), c.tickLower, c.tickUpper))
        );
    }

    function _readPoolPrice(Constituent storage c, uint32 secondsAgo)
        internal view virtual returns (uint256 priceInBuck)
    {
        int24 tick;
        if (secondsAgo == 0) {
            (, tick,,,,,) = IUniswapV3Pool(c.pool).slot0();
        } else {
            try this.consultTickExternal(c.pool, secondsAgo) returns (int24 t) {
                tick = t;
            } catch {
                (, tick,,,,,) = IUniswapV3Pool(c.pool).slot0();
            }
        }
        priceInBuck = UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(10 ** c.decimals), c.token, address(buck));
    }

    /// @dev External so `_readPoolPrice` can `try/catch` the consult (cold pools
    ///      without TWAP history revert).  Routed back into the facet by the
    ///      shell fallback when `this` resolves to the basket under delegatecall.
    function consultTickExternal(address pool, uint32 secondsAgo) external view returns (int24) {
        return UniswapV3OracleLib.consult(pool, secondsAgo);
    }

    // --- Snapped full-range bounds + initial price (moved from BasketMath) - //

    function _fullRangeTicks(int24 spacing)
        internal pure returns (int24 lower, int24 upper)
    {
        lower = (MIN_TICK / spacing) * spacing;
        upper = (MAX_TICK / spacing) * spacing;
    }

    /// @notice sqrtPriceX96 that makes a pool quote 1 whole TOKEN for
    ///         `priceInBuck` whole BUCK, honouring address ordering.
    function _sqrtPriceFromBuckRate(bool buckIsToken0, uint256 priceInBuck, uint8 tokenDecimals)
        internal pure returns (uint160)
    {
        uint256 buckRaw  = priceInBuck;           // 18-dec
        uint256 tokenRaw = 10 ** tokenDecimals;   // raw
        uint256 amount0  = buckIsToken0 ? buckRaw  : tokenRaw;
        uint256 amount1  = buckIsToken0 ? tokenRaw : buckRaw;
        uint256 ratioX192 = UniswapV3OracleLib.mulDiv(amount1, 1 << 192, amount0);
        uint256 sqrtRoot  = Math.sqrt(ratioX192);
        if (!(sqrtRoot <= type(uint160).max)) revert BadPrice();
        return uint160(sqrtRoot);
    }
}
