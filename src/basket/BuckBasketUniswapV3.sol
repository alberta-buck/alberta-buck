// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IUniswapV3Pool}     from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {IERC20}             from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Math}               from "@openzeppelin/contracts/utils/math/Math.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {IBuckBasketVenue}   from "./IBuckBasketVenue.sol";
import {BuckBasketStorage, IUniswapV3MintCallback,
        IUniswapV3SwapCallback} from "./BuckBasketStorage.sol";

/// @title BuckBasketUniswapV3 -- the Uniswap V3 venue facet for a BuckBasket.
///
/// @notice The AMM-specific half of the basket.  Deployed once and reached by
///         the shell (`BuckBasketProRata`) via `delegatecall`, so every method
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

    function setupPool(address token, uint8 decimals, uint256 initialPriceInBuck, uint24 feeTier)
        external override onlySelf
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

    function basketValueInBuck() external view override returns (int256) {
        if (constituents.length == 0) return int256(1e18);
        int256 total = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 priceInBuck = _readPoolPrice(c, twapWindow);
            total += int256(UniswapV3OracleLib.mulDiv(c.basketAmount, priceInBuck, 1e18));
        }
        return total;
    }

    function poolBuckValues()
        external view override
        returns (uint256[] memory bv, uint128[] memory depL, uint256 B, uint256[] memory prices)
    {
        uint256 N = constituents.length;
        bv     = new uint256[](N);
        depL   = new uint128[](N);
        prices = new uint256[](N);
        for (uint256 i = 0; i < N; i++) {
            Constituent storage c = constituents[i];
            uint128 totalL = _positionLiquidity(c);
            if (totalL <= c.treasuryLiquidity) continue;
            uint256 spot = _readPoolPrice(c, 0);
            _enforceSlippageGuard(c, spot, defaultMaxDeviationBp);
            prices[i] = spot;
            depL[i] = totalL - c.treasuryLiquidity;
            uint256 poolBuck = IERC20(address(buck)).balanceOf(c.pool);
            bv[i] = uint256(depL[i]) * poolBuck / totalL;
            B += bv[i];
        }
        if (!(B > 0)) revert NoValue();
    }

    // --- Liquidity in/out ------------------------------------------------- //

    // --- The work wheel's credits (doc/BASKET-WHEEL.org 8.4) --------------- //
    //
    // Policy, hosted here rather than in the shell for the shells' size budget
    // (the Fence inherits ProRata with ~1 KB to spare): reached through the
    // shell's fallback like every venue entry point, msg.sender preserved by
    // the delegatecall.  A Diamond would give them a facet of their own.

    function setWheel(address w) external {
        if (!(msg.sender == governance)) revert NotGovernance();
        wheel = w;
        emit WheelSet(w);
    }

    /// @notice The wheel credits TOKEN it captured to the DEPOSITORS: re-LP'd
    ///         into pool `i` as depositor liquidity with minted partner BUCK --
    ///         the stress fee's mechanics, booked in `stressBonusPrincipal` --
    ///         so every outstanding receipt's claim grows pro rata and the
    ///         basket still burns exactly what it minted.
    function creditDepositors(uint256 i, uint256 tokenAmount)
        external returns (uint128 liquidity, uint256 partnerBuck)
    {
        if (!(msg.sender == wheel && msg.sender != address(0))) revert NotWheel();
        if (!(totalOutstandingBuck > stressBonusPrincipal)) revert NoDepositors();
        IERC20(constituents[i].token).transferFrom(msg.sender, address(this), tokenAmount);
        (liquidity, partnerBuck) =
            IBuckBasketVenue(address(this)).provideForToken(i, tokenAmount, 0);
        stressBonusPrincipal += partnerBuck;
        totalOutstandingBuck += partnerBuck;
        emit WheelCredit(i, tokenAmount, partnerBuck);
    }

    /// @notice The wheel credits BUCK it captured to the treasury (re-LP'd by
    ///         sweepTreasury, like every other treasury accrual).
    function creditTreasury(uint256 buckAmount) external {
        if (!(msg.sender == wheel && msg.sender != address(0))) revert NotWheel();
        IERC20(address(buck)).transferFrom(msg.sender, address(this), buckAmount);
        treasuryBuckPending += buckAmount;
        emit TreasuryAccrued(buckAmount, treasuryBuckPending);
    }

    function provideForToken(uint256 i, uint256 tokenAmount, uint256 maxDeviationBp)
        external override onlySelf returns (uint128 liquidity, uint256 buckMinted)
    {
        Constituent storage c = constituents[i];
        _enforceSlippageGuard(c, _readPoolPrice(c, 0), maxDeviationBp);

        // Bind liquidity to the exact TOKEN held and compute the floor partner
        // BUCK for that L at the current sqrtP.  This is the receipt's principal;
        // a full redemption recovers it modulo a couple wei of V3 burn rounding.
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(c.pool).slot0();
        uint160 sqrtLow  = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickLower);
        uint160 sqrtHigh = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickUpper);
        if (c.buckIsToken0) {
            liquidity  = UniswapV3OracleLib.getLiquidityForAmount1(sqrtLow, sqrtP, tokenAmount);
            buckMinted = UniswapV3OracleLib.getAmount0ForLiquidity(sqrtP, sqrtHigh, liquidity);
        } else {
            liquidity  = UniswapV3OracleLib.getLiquidityForAmount0(sqrtP, sqrtHigh, tokenAmount);
            buckMinted = UniswapV3OracleLib.getAmount1ForLiquidity(sqrtLow, sqrtP, liquidity);
        }
        if (!(liquidity > 0)) revert L0();
        if (!(buckMinted > 0)) revert Buck0();
        if (_isFirstPositionInPool(c)) {
            if (!(liquidity >= minSeedLiquidity)) revert SeedTooSmall();
        }

        // Mint principal + 1 wei: V3 mint rounds owed BUCK up past the floor
        // estimate by at most 1 wei, so this guarantees the callback is covered.
        // Any unconsumed wei stays idle in the basket (bounded buffer).
        buck.mintFromBasket(address(this), buckMinted + 1);
        _callbackPool = c.pool;
        IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, liquidity, abi.encode(c.token));
        _callbackPool = address(0);
    }

    function withdrawLiquidity(uint256 i, uint128 liquidity)
        external override onlySelf returns (uint256 tokenOut, uint256 buckOut)
    {
        Constituent storage c = constituents[i];
        IUniswapV3Pool(c.pool).burn(c.tickLower, c.tickUpper, liquidity);
        (uint128 a0, uint128 a1) = IUniswapV3Pool(c.pool).collect(
            address(this), c.tickLower, c.tickUpper, type(uint128).max, type(uint128).max);
        if (c.buckIsToken0) { buckOut = a0; tokenOut = a1; }
        else                { tokenOut = a0; buckOut = a1; }
    }

    // --- Conversion verbs ------------------------------------------------- //

    /// @notice Deploy BUCK into the most-underweight (or hinted) pool: swap ~half
    ///         for the pool's TOKEN, LP both sides, return what was done.  The
    ///         shell tags ownership (treasury slice / receipt) and adjusts the
    ///         BUCK ledger; only the BUCK actually consumed is reported.
    function investFromBucks(uint256 buckAmount, uint256 poolHint)
        external override onlySelf
        returns (uint256 poolIdx, uint128 liquidity, uint256 buckConsumed)
    {
        poolIdx = poolHint == type(uint256).max ? _mostUnderweightPool() : poolHint;
        Constituent storage c = constituents[poolIdx];

        (uint256 buckSpent, uint256 tok) = _swapBuckForTokenExactIn(c, buckAmount / 2);
        uint256 buckForLp = buckAmount - buckSpent;            // remainder pairs with TOKEN

        liquidity = _liquidityForAmounts(c, tok, buckForLp);
        if (!(liquidity > 0)) revert ReinvestL0();

        _callbackPool = c.pool;
        (uint256 a0, uint256 a1) = IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, liquidity, abi.encode(c.token));
        _callbackPool = address(0);

        buckConsumed = buckSpent + (c.buckIsToken0 ? a0 : a1);   // swap + LP BUCK side
    }

    /// @notice Raise `targetBuck` BUCK by greedily swapping the held TOKEN
    ///         (most-TOKEN pool first) into BUCK on the internal pools.  Returns
    ///         the gained BUCK, the value lost to slippage+fee, and the remaining
    ///         inventory (the depositor's reduced TOKEN payout).
    /// @dev    FX multi-hop routing via the registry is a follow-up; internal
    ///         TOKEN/BUCK pools only for now.
    function convertIntoBucks(uint256[] calldata tokenInventory, uint256 targetBuck)
        external override onlySelf
        returns (uint256 gained, uint256 lossValue, uint256[] memory inv)
    {
        inv = tokenInventory;                  // calldata -> memory copy (mutated below)
        uint256 N = constituents.length;
        uint256 remaining = targetBuck;
        // A near-exact per-pool estimate clears a pool in one pass; the cap allows
        // a couple of correction passes and multi-pool spread.
        uint256 maxPasses = N * 2 + 4;
        for (uint256 pass = 0; pass < maxPasses && remaining > 0; pass++) {
            // Pool with the most held TOKEN that still has in-range liquidity to
            // swap against.  A pool just fully drained (single-depositor full
            // redeem) has liquidity()==0 and is skipped; its TOKEN flows to the
            // depositor unconverted.
            uint256 bestIdx = type(uint256).max;
            uint256 bestTok = 0;
            for (uint256 i = 0; i < N; i++) {
                if (inv[i] > bestTok
                    && IUniswapV3Pool(constituents[i].pool).liquidity() > 0) {
                    bestIdx = i; bestTok = inv[i];
                }
            }
            if (bestIdx == type(uint256).max) break;

            Constituent storage c = constituents[bestIdx];
            uint256 priceBefore = _readPoolPrice(c, 0);   // spot, for loss accounting
            uint256 tokenIn = _tokenInForBuckOut(c, remaining);
            if (tokenIn == 0 || tokenIn > inv[bestIdx]) {
                tokenIn = inv[bestIdx];   // sell all available here
            }
            (uint256 spent, uint256 received) = _swapTokenForBuckExactIn(c, tokenIn);
            inv[bestIdx] -= spent;
            gained += received;
            uint256 spentValue = UniswapV3OracleLib.mulDiv(
                spent, priceBefore, 10 ** c.decimals);
            if (spentValue > received) lossValue += spentValue - received;
            remaining = received >= remaining ? 0 : remaining - received;
            if (received == 0) break;   // no progress; avoid spinning
        }
    }

    // --- Equity primitives (BuckBasketEquity) ------------------------------ //

    /// @inheritdoc IBuckBasketVenue
    function marks(uint256 i, uint128 liquidity)
        external view override returns (Marks memory m)
    {
        Constituent storage c = constituents[i];
        (uint160 sSpot, int24 tSpot,,,,,) = IUniswapV3Pool(c.pool).slot0();
        int24 tTwap = tSpot;
        try this.consultTickExternal(c.pool, twapWindow) returns (int24 t) { tTwap = t; }
        catch {}
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

    function poolLive(uint256 i) external view override returns (bool) {
        return IUniswapV3Pool(constituents[i].pool).liquidity() > 0;
    }

    function positionMint(uint256 i, uint256 tokenAmount, uint256 buckAmount)
        external override onlySelf
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

    // --- Fence primitives (BuckBasketFence) ------------------------------- //

    function fencePool(address token, uint8 decimals, uint256 initialPriceInBuck,
                       uint24 feeTier)
        external override onlySelf
        returns (address pool, int24 spacing, bool buckIsToken0)
    {
        pool = _findOrCreatePool(token, feeTier);
        buckIsToken0 = address(buck) < token;
        uint160 sqrtPriceX96 =
            _sqrtPriceFromBuckRate(buckIsToken0, initialPriceInBuck, decimals);
        try IUniswapV3Pool(pool).initialize(sqrtPriceX96) {} catch {}
        IUniswapV3Pool(pool).increaseObservationCardinalityNext(observationCardinality);
        spacing = v3Factory.feeAmountTickSpacing(feeTier);
    }

    function fenceMint(address token, address pool, int24 lo, int24 hi,
                       uint128 liquidity)
        external override onlySelf returns (uint256 a0, uint256 a1)
    {
        if (!(liquidity > 0)) revert L0();
        _callbackPool = pool;
        (a0, a1) = IUniswapV3Pool(pool).mint(
            address(this), lo, hi, liquidity, abi.encode(token));
        _callbackPool = address(0);
    }

    function fenceBurn(address pool, int24 lo, int24 hi, uint128 liquidity)
        external override onlySelf returns (uint256 a0, uint256 a1)
    {
        IUniswapV3Pool(pool).burn(lo, hi, liquidity);
        (uint128 c0, uint128 c1) = IUniswapV3Pool(pool).collect(
            address(this), lo, hi, type(uint128).max, type(uint128).max);
        return (uint256(c0), uint256(c1));
    }

    function fenceSwap(address token, address pool, bool sellBuck, uint256 amountIn)
        external override onlySelf returns (uint256 spent, uint256 received)
    {
        if (amountIn == 0) return (0, 0);
        bool buckIs0 = address(buck) < token;
        bool zeroForOne = sellBuck ? buckIs0 : !buckIs0;
        _swapCallbackPool = pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(pool).swap(
            address(this), zeroForOne, int256(amountIn),
            zeroForOne ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1,
            abi.encode(token));
        _swapCallbackPool = address(0);
        int256 buckDelta = buckIs0 ? d0 : d1;
        int256 tokDelta  = buckIs0 ? d1 : d0;
        if (sellBuck) {
            if (!(buckDelta >= 0 && tokDelta <= 0)) revert SwapDeltaSign();
            spent = uint256(buckDelta); received = uint256(-tokDelta);
        } else {
            if (!(buckDelta <= 0 && tokDelta >= 0)) revert SwapDeltaSign();
            spent = uint256(tokDelta); received = uint256(-buckDelta);
        }
    }

    function fenceQuote(address pool, int24 lo, int24 hi, uint128 liquidity,
                        bool buckIsToken0)
        external view override returns (uint256 buckAmt, uint256 tokAmt)
    {
        if (liquidity == 0) return (0, 0);
        (uint160 sp,,,,,,) = IUniswapV3Pool(pool).slot0();
        uint160 sa = UniswapV3OracleLib.getSqrtRatioAtTick(lo);
        uint160 sb = UniswapV3OracleLib.getSqrtRatioAtTick(hi);
        uint160 spc = sp < sa ? sa : (sp > sb ? sb : sp);
        uint256 a0 = UniswapV3OracleLib.getAmount0ForLiquidity(spc, sb, liquidity);
        uint256 a1 = UniswapV3OracleLib.getAmount1ForLiquidity(sa, spc, liquidity);
        return buckIsToken0 ? (a0, a1) : (a1, a0);
    }

    function fenceTwap(address pool, address token, uint8 decimals,
                       uint32 secondsAgo)
        external view override returns (uint256)
    {
        int24 tick;
        if (secondsAgo == 0) {
            (, tick,,,,,) = IUniswapV3Pool(pool).slot0();
        } else {
            try this.consultTickExternal(pool, secondsAgo) returns (int24 t) {
                tick = t;
            } catch {
                (, tick,,,,,) = IUniswapV3Pool(pool).slot0();
            }
        }
        return UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(10 ** decimals), token, address(buck));
    }

    function fenceLiquidityFor(address pool, int24 lo, int24 hi,
                               uint256 amount0, uint256 amount1)
        external view override returns (uint128)
    {
        (uint160 sp,,,,,,) = IUniswapV3Pool(pool).slot0();
        return UniswapV3OracleLib.getLiquidityForAmounts(
            sp, UniswapV3OracleLib.getSqrtRatioAtTick(lo),
            UniswapV3OracleLib.getSqrtRatioAtTick(hi), amount0, amount1);
    }

    function fenceState(address pool)
        external view override
        returns (uint160 sqrtPriceX96, int24 tick, int24 spacing)
    {
        (sqrtPriceX96, tick,,,,,) = IUniswapV3Pool(pool).slot0();
        spacing = IUniswapV3Pool(pool).tickSpacing();
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

    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data)
        external override
    {
        if (!(msg.sender == _swapCallbackPool)) revert BadSwapCallback();
        address token = abi.decode(data, (address));
        Constituent storage c = constituents[indexOf[token] - 1];
        if (c.buckIsToken0) {
            if (amount0Delta > 0) IERC20(address(buck)).transfer(msg.sender, uint256(amount0Delta));
            if (amount1Delta > 0) IERC20(c.token).transfer(msg.sender, uint256(amount1Delta));
        } else {
            if (amount0Delta > 0) IERC20(c.token).transfer(msg.sender, uint256(amount0Delta));
            if (amount1Delta > 0) IERC20(address(buck)).transfer(msg.sender, uint256(amount1Delta));
        }
    }

    // --- Swap helpers ----------------------------------------------------- //

    /// @notice TOKEN-in to extract `buckOut` BUCK from `c.pool`, via the CPMM
    ///         exact-output formula on the pool's actual reserves (full-range V3
    ///         ⇒ pool balances are the virtual reserves), inflated by the fee.
    ///         Returns `type(uint256).max` when the pool can't cover `buckOut`.
    function _tokenInForBuckOut(Constituent storage c, uint256 buckOut)
        internal view returns (uint256 tokenIn)
    {
        uint256 tokRes  = IERC20(c.token).balanceOf(c.pool);
        uint256 buckRes = IERC20(address(buck)).balanceOf(c.pool);
        if (buckRes <= buckOut || tokRes == 0) return type(uint256).max;
        uint256 ideal = UniswapV3OracleLib.mulDiv(tokRes, buckOut, buckRes - buckOut);
        tokenIn = UniswapV3OracleLib.mulDiv(ideal, 1e6, 1e6 - c.feeTier) + 1;
    }

    /// @inheritdoc IBuckBasketVenue
    ///
    /// @dev The per-leg size bound lives in the shell, but note WHY one is
    ///      needed at all beyond good behaviour: `poolBuckValues` runs
    ///      `_enforceSlippageGuard` on every redemption, so a monetary swap
    ///      big enough to push spot off TWAP would revert every depositor
    ///      exit until the window caught up.  The basket must not be able to
    ///      brick its own redemption path.
    function monetaryLeg(uint256 i, bool sellBuck, uint256 amountIn)
        external override onlySelf returns (uint256 spent, uint256 received)
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

    /// @notice Index of the pool most underweight against the basket's
    ///         *fixed-quantity* target, whose value share scales by
    ///         initialPrice/spot (hold less BUCK value of a token as it
    ///         appreciates).  Relative target share s = basketAmount *
    ///         initialPrice² / spot; rank pools by actual-value / s (lowest =
    ///         most underweight; empty pools, ratio 0, sort first).
    function _mostUnderweightPool() internal view returns (uint256 idx) {
        int256 best = type(int256).max;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 v = _poolLpValue(c);
            uint256 p = _readPoolPrice(c, 0);
            uint256 base = UniswapV3OracleLib.mulDiv(c.basketAmount, c.initialPriceInBuck, 1e18);
            uint256 s = p > 0 ? UniswapV3OracleLib.mulDiv(base, c.initialPriceInBuck, p) : 0;
            int256 ratio = s > 0 ? int256(UniswapV3OracleLib.mulDiv(v, 1e18, s)) : type(int256).max;
            if (ratio < best) { best = ratio; idx = i; }
        }
    }

    /// @notice BUCK value of a pool's reserves (full-range ⇒ balances are the
    ///         virtual reserves).  0 if unseeded.
    function _poolLpValue(Constituent storage c) internal view returns (uint256 valueBuck) {
        uint256 tokBal = IERC20(c.token).balanceOf(c.pool);
        if (tokBal == 0) return 0;
        uint256 spotPrice = _readPoolPrice(c, 0);
        uint256 buckBal = IERC20(address(buck)).balanceOf(c.pool);
        valueBuck = UniswapV3OracleLib.mulDiv(tokBal, spotPrice, 10 ** c.decimals) + buckBal;
    }

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

    function _isFirstPositionInPool(Constituent storage c) internal view returns (bool) {
        return _positionLiquidity(c) == 0;
    }

    function _readPoolPrice(Constituent storage c, uint32 secondsAgo)
        internal view returns (uint256 priceInBuck)
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

    function _enforceSlippageGuard(Constituent storage c, uint256 spotPrice, uint256 maxDeviationBp)
        internal view
    {
        if (maxDeviationBp == 0) return;
        try this.peekTwap(c.pool, twapWindow) returns (uint256 twapPrice) {
            if (twapPrice == 0) return;
            uint256 dev = spotPrice > twapPrice ? spotPrice - twapPrice : twapPrice - spotPrice;
            if (!(dev * 10000 <= twapPrice * maxDeviationBp)) revert Slippage();
        } catch {
            return;
        }
    }

    function peekTwap(address pool, uint32 secondsAgo) external view returns (uint256) {
        int24 tick = UniswapV3OracleLib.consult(pool, secondsAgo);
        Constituent storage c = constituents[indexOf[_poolToToken(pool)] - 1];
        return UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(10 ** c.decimals), c.token, address(buck));
    }

    function _poolToToken(address pool) internal view returns (address) {
        for (uint256 i = 0; i < constituents.length; i++) {
            if (constituents[i].pool == pool) return constituents[i].token;
        }
        revert NotInBasket();
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
