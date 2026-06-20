// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IUniswapV3Pool}    from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {IERC20}            from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {UniswapV3OracleLib} from "../lib/UniswapV3OracleLib.sol";
import {IBuckKController}    from "../IBuckKController.sol";
import {BasketMath}         from "./BasketMath.sol";
import {BuckBasketReceipt}  from "./BuckBasketReceipt.sol";
import {IBasketRebalancer}  from "./IBasketRebalancer.sol";
import {BuckBasketStorage, IUniswapV3Factory, IUniswapV3MintCallback,
        IUniswapV3SwapCallback, IBuckMintBurn} from "./BuckBasketStorage.sol";

/// @title BuckBasketProRata -- pro-rata exit + treasury-split direct-mint orchestrator.
///
/// @notice Custodies one full-range Buck-owned Uniswap V3 LP position per
///         TOKEN/BUCK constituent pool and mints BUCK against deposited TOKEN.
///         Redemption is a **sell-high value-claim exit**: a receipt's claim
///         `V = θ·NAV` (θ = redeemBuck / totalOutstandingBuck) is allocated
///         across pools by a single closed-form over the per-pool depositor BUCK
///         reserves (full-range ⇒ pool value = 2·buckReserve), drawing from the
///         most *overweight* pools first and degenerating to pure pro-rata at
///         equilibrium.  Value-conservation preserves the coverage ratio, so the
///         tail stays solvent regardless of which pools are drawn.  An optional
///         `payoutToken` instead draws the whole claim from one pool (no
///         cross-pool routing).  The two economic reverts are (1) the burn
///         unsatisfiable within the caller's `maxConversionLossBp` budget
///         (underwater being its extreme) and (2) a single-TOKEN payout whose
///         pool can't source the claim (`token too thin`).
///
/// # Two kinds of liquidity
///
///   * **Depositor liquidity** -- backs outstanding receipts; the pro-rata
///     claim base.  `depositorL = positionL - treasuryLiquidity`.
///   * **Treasury liquidity / pending BUCK** -- NAV above `totalOutstandingBuck`
///     (AMM fees, retained BUCK profit, unclaimed external-arb BUCK).  Funds
///     BUCK-system R&D/ops; re-LP'd by the rebalancer; never part of a
///     depositor's pro-rata claim.
///
/// # Profit split (realized at redeem; `treasuryBp`, default 50/50)
///
///   * Depositor keeps 100% of the withdrawn TOKEN side + `(1-treasuryBp)` of
///     any BUCK profit (`Bw - R`).
///   * Treasury keeps `treasuryBp` of BUCK profit (accrued to `treasuryBuckPending`).
///   * The principal burn is *senior* to the split: under deflation it is
///     covered first from withdrawn BUCK, then from selling the depositor's
///     own TOKEN; the treasury never takes TOKEN.
///
/// # Manipulation guard
///
///   The allocation reads spot BUCK reserves (value = 2·buckReserve).  To stop a
///   sandwich from moving spot to distort the value read / claim, every pool
///   whose reserves enter the computation must sit within `defaultMaxDeviationBp`
///   of its TWAP (`_poolBuckValues` → `_enforceSlippageGuard`); a manipulated
///   pool reverts the redeem.  Cold pools without TWAP history skip the guard
///   (bootstrap window).
///
/// # Scaffold notes
///
///   * Shortfall conversion + treasury re-LP route through the internal
///     TOKEN/BUCK pools (`_coverShortfall`, `_reinvestTreasury`).  FX multi-hop
///     routing via `rebalancer` + Uniswap `ISwapRouter` is a follow-up; the
///     `IBasketRebalancer` wiring is already in place.
///   * Treasury re-LP (`sweepTreasury`) recycles accrued profit into the most
///     underweight pool as treasury-owned liquidity ("buy low").
///   * BUCK-side deposits and the full migration handoff are not yet built.
contract BuckBasketProRata is BuckBasketStorage, IUniswapV3MintCallback, IUniswapV3SwapCallback {


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
    ) {
        require(_buck != address(0) && _controller != address(0)
                && _v3Factory != address(0) && _governance != address(0), "zero addr");
        buck                   = IBuckMintBurn(_buck);
        controller             = IBuckKController(_controller);
        v3Factory              = IUniswapV3Factory(_v3Factory);
        governance             = _governance;
        defaultFeeTier         = _defaultFeeTier;
        twapWindow             = _twapWindow;
        observationCardinality = _observationCardinality;
        defaultMaxDeviationBp  = _defaultMaxDeviationBp;
        minSeedLiquidity       = _minSeedLiquidity;
        treasuryBp             = 5000;

        receipt = new BuckBasketReceipt(address(this));
    }

    // --- Governance ------------------------------------------------------- //

    function setGovernance(address _governance) external onlyGov {
        if (!(_governance != address(0))) revert Gov0();
        governance = _governance;
    }

    function setRebalancer(address _rebalancer) external onlyGov {
        rebalancer = IBasketRebalancer(_rebalancer);
        emit RebalancerSet(_rebalancer);
    }

    function setTreasuryBp(uint16 _treasuryBp) external onlyGov {
        if (!(_treasuryBp <= MAX_TREASURY_BP)) revert TreasuryBpTooHigh();
        treasuryBp = _treasuryBp;
        emit TreasuryBpSet(_treasuryBp);
    }

    /// @notice Draw accumulated treasury BUCK profit to fund operations.
    function treasuryWithdraw(address to, uint256 amount) external onlyGov {
        if (!(to != address(0))) revert To0();
        if (!(amount <= treasuryBuckPending)) revert ExceedsPending();
        treasuryBuckPending -= amount;
        IERC20(address(buck)).transfer(to, amount);
        emit TreasuryWithdrawn(to, amount);
    }

    /// @notice Recycle accrued treasury BUCK profit into the most underweight
    ///         pool as treasury-owned liquidity -- the "buy low" leg, decoupled
    ///         from redemption.  Permissionless (any keeper); no-op below the
    ///         re-LP floor.
    function sweepTreasury() external {
        if (treasuryBuckPending >= MIN_REINVEST_BUCK) {
            _reinvestTreasury(treasuryBuckPending);
        }
    }

    /// @notice Treasury-owned liquidity in constituent `i` (excluded from the
    ///         depositor pro-rata claim base).
    function treasuryLiquidityOf(uint256 i) external view returns (uint128) {
        return constituents[i].treasuryLiquidity;
    }

    /// @notice Register a basket constituent.  Renormalizes existing declared
    ///         weights to keep Σ targetWeightBp == 10000, preserving each
    ///         existing constituent's price (basketAmount scaled by the same
    ///         ratio, not re-priced at current spot).  Name + signature match
    ///         the legacy `BuckBasket.addBasketToken` for sim drop-in compat.
    function addBasketToken(
        address token,
        uint8   decimals,
        uint256 initialPriceInBuck,
        uint256 weightBp,
        uint24  feeTier
    ) external onlyGov returns (address pool) {
        if (!(token != address(0) && token != address(buck))) revert BadToken();
        if (!(weightBp <= 10000)) revert BadWeight();
        if (!(indexOf[token] == 0)) revert AlreadyPresent();
        if (!(initialPriceInBuck > 0)) revert BadPrice();

        uint256 N = constituents.length + 1;
        uint256 newW = weightBp > 0 ? weightBp : 10000 / N;
        if (!(newW <= 10000 && newW > 0)) revert BadTargetWeight();

        uint256 oldW = 10000 - newW;
        uint256 oldSumW = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            c.targetWeightBp = UniswapV3OracleLib.mulDiv(c.targetWeightBp, oldW, 10000);
            if (!(c.targetWeightBp > 0 && c.targetWeightBp < 10000)) revert InvalidRescale();
            oldSumW += c.targetWeightBp;
            c.basketAmount = UniswapV3OracleLib.mulDiv(c.basketAmount, oldW, 10000);
        }
        if (constituents.length > 0) {
            if (!(oldSumW < 10000)) revert ScaledWeightsIncorrect();
            newW = 10000 - oldSumW;
        }

        uint256 weightUnit  = UniswapV3OracleLib.mulDiv(newW, 1e18, 10000);
        uint256 basketAmount = UniswapV3OracleLib.mulDiv(weightUnit, 1e18, initialPriceInBuck);

        pool = _findOrCreatePool(token, feeTier);
        bool buckIsToken0 = address(buck) < token;
        uint160 sqrtPriceX96 = BasketMath.sqrtPriceFromBuckRate(
            buckIsToken0, initialPriceInBuck, decimals);
        try IUniswapV3Pool(pool).initialize(sqrtPriceX96) {} catch {}
        IUniswapV3Pool(pool).increaseObservationCardinalityNext(observationCardinality);

        int24 spacing = v3Factory.feeAmountTickSpacing(feeTier);
        (int24 tickLower, int24 tickUpper) = BasketMath.fullRangeTicks(spacing);

        constituents.push(Constituent({
            token: token,
            decimals: decimals,
            feeTier: feeTier,
            pool: pool,
            tickLower: tickLower,
            tickUpper: tickUpper,
            buckIsToken0: buckIsToken0,
            targetWeightBp: newW,
            basketAmount: basketAmount,
            initialPriceInBuck: initialPriceInBuck,
            treasuryLiquidity: 0
        }));
        indexOf[token] = constituents.length;

        controller.reprime();
        emit BasketTokenAdded(token, newW, initialPriceInBuck, pool);
    }

    function constituentsLength() external view returns (uint256) {
        return constituents.length;
    }

    // --- Process variable for the controller ------------------------------ //

    function basketValueInBuck() external view returns (int256) {
        if (constituents.length == 0) return int256(1e18);
        int256 total = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 priceInBuck = _readPoolPrice(c, twapWindow);
            total += int256(UniswapV3OracleLib.mulDiv(c.basketAmount, priceInBuck, 1e18));
        }
        return total;
    }

    // --- Direct mint ------------------------------------------------------ //

    /// @notice Deposit a registered basket TOKEN: mint BUCK at the pool's spot
    ///         price and LP the (TOKEN, BUCK) pair full-range.  Returns an
    ///         ERC-721 receipt.
    /// @dev    BUCK-side deposits (underweight routing) are a follow-up.
    function depositToken(address token, uint256 tokenAmount, uint256 maxDeviationBp)
        external returns (uint256 receiptId)
    {
        if (!(tokenAmount > 0)) revert Amount0();
        if (!(token != address(buck))) revert BUCKDepositTODO();
        uint256 idx = indexOf[token];
        if (!(idx > 0)) revert NotInBasket();
        Constituent storage c = constituents[idx - 1];

        _enforceSlippageGuard(c, _readPoolPrice(c, 0), maxDeviationBp);
        IERC20(token).transferFrom(msg.sender, address(this), tokenAmount);

        // Bind liquidity to the exact TOKEN deposited and compute the floor
        // partner BUCK for that L at the current sqrtP.  This is the receipt's
        // principal; a full redemption recovers it modulo a couple wei of V3
        // burn rounding (absorbed by MAX_DUST_WEI on redeem).
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(c.pool).slot0();
        uint160 sqrtLow  = BasketMath.getSqrtRatioAtTick(c.tickLower);
        uint160 sqrtHigh = BasketMath.getSqrtRatioAtTick(c.tickUpper);
        uint128 liquidity;
        uint256 buckToMint;
        if (c.buckIsToken0) {
            liquidity  = BasketMath.getLiquidityForAmount1(sqrtLow, sqrtP, tokenAmount);
            buckToMint = BasketMath.getAmount0ForLiquidity(sqrtP, sqrtHigh, liquidity);
        } else {
            liquidity  = BasketMath.getLiquidityForAmount0(sqrtP, sqrtHigh, tokenAmount);
            buckToMint = BasketMath.getAmount1ForLiquidity(sqrtLow, sqrtP, liquidity);
        }
        if (!(liquidity > 0)) revert L0();
        if (!(buckToMint > 0)) revert Buck0();
        if (_isFirstPositionInPool(c)) {
            if (!(liquidity >= minSeedLiquidity)) revert SeedTooSmall();
        }

        // Mint principal + 1 wei: V3 mint rounds the owed BUCK up past the floor
        // estimate by at most 1 wei, so this guarantees the callback is covered.
        // Any unconsumed wei stays idle in the basket (a negligible, bounded
        // buffer) -- far simpler than over-minting and refunding the remainder.
        buck.mintFromBasket(address(this), buckToMint + 1);
        _callbackPool = c.pool;
        IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, liquidity, abi.encode(token));
        _callbackPool = address(0);

        receiptId = receipt.mint(msg.sender);
        deposits[receiptId] = Deposit({
            buckPrincipal: buckToMint,
            tokenPrincipal: tokenAmount,
            token: token,
            depositTime: uint64(block.timestamp)
        });
        totalOutstandingBuck += buckToMint;

        controller.compute();
        emit Deposited(msg.sender, receiptId, token, tokenAmount, buckToMint, liquidity);
    }

    // --- Redemption (sell-high value-claim exit) -------------------------- //

    /// @notice Balanced redeem with the default conversion-loss budget (1%).
    function redeem(uint256 receiptId, uint256 redeemBp) external {
        _redeem(receiptId, redeemBp, DEFAULT_CONVERSION_LOSS_BP, address(0));
    }

    /// @notice Balanced redeem with an explicit conversion-loss budget.
    function redeem(uint256 receiptId, uint256 redeemBp, uint256 maxConversionLossBp)
        external
    {
        _redeem(receiptId, redeemBp, maxConversionLossBp, address(0));
    }

    /// @notice Single-TOKEN redeem: source the whole claim from `payoutToken`'s
    ///         pool only (no cross-pool routing).  Reverts if that pool can't
    ///         supply the claim (`token too thin`) or, under deflation, if the
    ///         within-pool TOKEN->BUCK conversion needed to cover the burn
    ///         exceeds `maxConversionLossBp` (`conversion loss`).
    function redeem(uint256 receiptId, uint256 redeemBp,
                    address payoutToken, uint256 maxConversionLossBp)
        external
    {
        if (!(payoutToken != address(0))) revert PayoutToken0();
        _redeem(receiptId, redeemBp, maxConversionLossBp, payoutToken);
    }

    /// @notice Shared redeem body.  `payoutToken == 0` ⇒ balanced sell-high
    ///         allocation (§5.1); otherwise the whole claim is drawn from that
    ///         token's pool.  `maxConversionLossBp` caps the value lost to forced
    ///         TOKEN->BUCK conversion under deflation; the call reverts rather
    ///         than realize a larger loss.
    function _redeem(uint256 receiptId, uint256 redeemBp,
                     uint256 maxConversionLossBp, address payoutToken)
        internal
    {
        if (!(receipt.ownerOf(receiptId) == msg.sender)) revert NotOwner();
        Deposit memory d = deposits[receiptId];
        if (!(d.buckPrincipal > 0)) revert EmptyDeposit();
        uint256 redeemShare = redeemBp == 0 ? 10000 : redeemBp;
        if (!(redeemShare <= 10000)) revert Bp10000();
        if (!(totalOutstandingBuck > 0)) revert NoOutstanding();

        uint256 R = redeemShare == 10000
            ? d.buckPrincipal
            : d.buckPrincipal * redeemShare / 10000;
        if (!(R > 0)) revert RedeemZero();

        // Phase 1: allocate (balanced sell-high, or all from one pool), withdraw.
        (uint256[] memory burnL, uint256 V) = payoutToken == address(0)
            ? _allocateSellHigh(R)
            : _allocateSingleToken(R, payoutToken);
        uint256 N = constituents.length;
        uint256[] memory perPoolTok = new uint256[](N);
        uint256 Bw = 0;
        bool anyWithdrawn = false;
        for (uint256 i = 0; i < N; i++) {
            if (burnL[i] == 0) continue;
            (uint256 tok, uint256 b) = _decreaseAndCollect(constituents[i], uint128(burnL[i]));
            perPoolTok[i] = tok;
            Bw += b;
            anyWithdrawn = true;
        }
        if (!(anyWithdrawn)) revert NoLPWithdrawn();

        // Phase 2: settle the burn.  Sell-high collects from BUCK-rich pools, so
        // `Bw >= R` is the common case (no conversion).  Under deflation, cover
        // the shortfall within the loss budget.
        uint256 depositorBuck = 0;
        uint256 treasuryBuck  = 0;
        uint256 burned;
        if (Bw >= R) {
            burned = R;
            (treasuryBuck, depositorBuck) = BasketMath.splitProfit(Bw - R, treasuryBp);
        } else {
            (uint256 gained, uint256 lossValue) = _coverShortfall(perPoolTok, R - Bw);
            uint256 have = Bw + gained;
            burned = have >= R ? R : have;
            // Revert path 1: burn unsatisfiable within the loss budget.
            // maxConversionLossBp == 0 means "unlimited" (skip the loss cap) so
            // `redeem(id, bp, 0)` matches the legacy basket's no-guard call.
            if (!(R - burned <= MAX_DUST_WEI)) revert Underwater();
            if (!(maxConversionLossBp == 0
                  || lossValue * 10000 <= maxConversionLossBp * V)) revert ConversionLoss();
            treasuryBuck = have - burned;   // over-swap excess; no depositor profit
        }

        // Phase 3: burn principal, pay out.
        if (burned > 0) buck.burnFromBasket(burned);
        if (treasuryBuck > 0) {
            treasuryBuckPending += treasuryBuck;
            emit TreasuryAccrued(treasuryBuck, treasuryBuckPending);
        }
        if (depositorBuck > 0) {
            IERC20(address(buck)).transfer(msg.sender, depositorBuck);
        }
        for (uint256 i = 0; i < N; i++) {
            if (perPoolTok[i] > 0) {
                IERC20(constituents[i].token).transfer(msg.sender, perPoolTok[i]);
            }
        }

        // Phase 4: finalize bookkeeping (outstanding drops by R; any dust gap
        // R-burned is a bounded supply leak, not double-counted).
        if (redeemShare == 10000) {
            delete deposits[receiptId];
            receipt.burn(receiptId);
        } else {
            deposits[receiptId].buckPrincipal -= R;
            deposits[receiptId].tokenPrincipal -= d.tokenPrincipal * redeemShare / 10000;
        }
        totalOutstandingBuck -= R;

        controller.compute();
        emit Redeemed(
            msg.sender, receiptId, burned, depositorBuck, treasuryBuck,
            redeemShare == 10000 ? 0 : 10000 - redeemShare);
    }

    /// @notice Closed-form sell-high allocation in BUCK value.  Full-range ⇒
    ///         pool value = 2·buckReserve, so this is a function of the per-pool
    ///         *depositor* BUCK reserves alone: draw the value claim
    ///         `V = θ·NAV` from the most overweight pools first, degenerating to
    ///         pro-rata at equilibrium.
    /// @return burnL  liquidity to burn per pool (∑ value = V, each ≤ depositorL).
    /// @return V      the value claim in BUCK (the loss-budget base).
    function _allocateSellHigh(uint256 R)
        internal view returns (uint256[] memory burnL, uint256 V)
    {
        (uint256[] memory bv, uint128[] memory depL, uint256 B) = _poolBuckValues();
        uint256 N = constituents.length;
        burnL = new uint256[](N);

        uint256 O = totalOutstandingBuck;
        V = UniswapV3OracleLib.mulDiv(R, 2 * B, O);             // θ·NAV
        uint256 claimBv = UniswapV3OracleLib.mulDiv(R, B, O);   // θ·B (BUCK half of V)

        // Ideal sell-high draw per pool: aᵢ = bvᵢ − wᵢ·B·(O−R)/O.  Positive ⇒
        // overweight; clamp negatives (underweight pools draw 0).
        uint256[] memory pos = new uint256[](N);
        uint256 sumPos = 0;
        for (uint256 i = 0; i < N; i++) {
            if (bv[i] == 0) continue;
            uint256 tgt = UniswapV3OracleLib.mulDiv(
                uint256(constituents[i].targetWeightBp) * B, O - R, 10000 * O);
            if (bv[i] > tgt) { pos[i] = bv[i] - tgt; sumPos += pos[i]; }
        }
        if (sumPos == 0) return (burnL, V);    // unreachable: ∑aᵢ = claimBv > 0

        for (uint256 i = 0; i < N; i++) {
            if (pos[i] == 0) continue;
            uint256 allocBv = UniswapV3OracleLib.mulDiv(pos[i], claimBv, sumPos);
            uint256 bl = UniswapV3OracleLib.mulDiv(uint256(depL[i]), allocBv, bv[i]);
            burnL[i] = bl > depL[i] ? depL[i] : bl;   // cap for rounding safety
        }
    }

    /// @notice Single-TOKEN allocation: draw the entire value claim `V = θ·NAV`
    ///         from `token`'s pool alone.  Reverts `token too thin` if that pool
    ///         can't source the claim (f > 1).  The burn is then covered from
    ///         that one pool's BUCK side (+ within-pool conversion if deflation),
    ///         so no other pool is ever touched.
    function _allocateSingleToken(uint256 R, address token)
        internal view returns (uint256[] memory burnL, uint256 V)
    {
        uint256 ix = indexOf[token];
        if (!(ix > 0)) revert NotInBasket();
        ix -= 1;

        (uint256[] memory bv, uint128[] memory depL, uint256 B) = _poolBuckValues();
        if (!(bv[ix] > 0)) revert EmptyPool();

        V = UniswapV3OracleLib.mulDiv(R, 2 * B, totalOutstandingBuck);   // θ·NAV
        uint256 dvX = 2 * bv[ix];                                        // pool value
        if (!(V <= dvX)) revert TokenTooThin();                            // f ≤ 1

        burnL = new uint256[](constituents.length);
        uint256 bl = UniswapV3OracleLib.mulDiv(uint256(depL[ix]), V, dvX);
        burnL[ix] = bl > depL[ix] ? depL[ix] : bl;
    }

    /// @notice Per-pool depositor BUCK reserve (the value sufficient statistic:
    ///         full-range ⇒ pool value = 2·buckReserve) plus the total `B`.
    ///         Each touched pool's spot must sit within `defaultMaxDeviationBp`
    ///         of its TWAP — the manipulation guard on the value read (a sandwich
    ///         that moves spot to distort the allocation reverts here).  Cold
    ///         pools without TWAP history skip the guard (bootstrap window).
    function _poolBuckValues()
        internal view returns (uint256[] memory bv, uint128[] memory depL, uint256 B)
    {
        uint256 N = constituents.length;
        bv   = new uint256[](N);
        depL = new uint128[](N);
        for (uint256 i = 0; i < N; i++) {
            Constituent storage c = constituents[i];
            uint128 totalL = _positionLiquidity(c);
            if (totalL <= c.treasuryLiquidity) continue;
            _enforceSlippageGuard(c, _readPoolPrice(c, 0), defaultMaxDeviationBp);
            depL[i] = totalL - c.treasuryLiquidity;
            uint256 poolBuck = IERC20(address(buck)).balanceOf(c.pool);
            bv[i] = uint256(depL[i]) * poolBuck / totalL;
            B += bv[i];
        }
        if (!(B > 0)) revert NoValue();
    }

    // --- Shortfall cover (scaffold: internal TOKEN/BUCK pools) ------------ //

    /// @notice Raise `shortfall` BUCK by greedily swapping the withdrawn TOKEN
    ///         (most-TOKEN pool first) into BUCK on the internal pools.
    ///         Mutates `perPoolTok` to reflect TOKEN spent.
    /// @return gained     BUCK obtained from the conversions.
    /// @return lossValue  BUCK value lost to slippage + fee (spent TOKEN valued
    ///                    at its pre-swap spot price, minus BUCK received).
    /// @dev    FX multi-hop routing via `rebalancer` is the follow-up; for now
    ///         this is internal-pool only.
    function _coverShortfall(uint256[] memory perPoolTok, uint256 shortfall)
        internal returns (uint256 gained, uint256 lossValue)
    {
        uint256 N = constituents.length;
        uint256 remaining = shortfall;
        // A near-exact per-pool estimate means one pass usually clears a pool;
        // the cap allows a couple of correction passes and multi-pool spread.
        uint256 maxPasses = N * 2 + 4;
        for (uint256 pass = 0; pass < maxPasses && remaining > 0; pass++) {
            // Pick the pool with the most withdrawn TOKEN that still has
            // in-range liquidity to swap against.  A pool we just fully drained
            // (single-depositor full redeem) has liquidity()==0 and is skipped;
            // its TOKEN simply flows to the depositor unconverted.
            uint256 bestIdx = type(uint256).max;
            uint256 bestTok = 0;
            for (uint256 i = 0; i < N; i++) {
                if (perPoolTok[i] > bestTok
                    && IUniswapV3Pool(constituents[i].pool).liquidity() > 0) {
                    bestIdx = i; bestTok = perPoolTok[i];
                }
            }
            if (bestIdx == type(uint256).max) break;

            Constituent storage c = constituents[bestIdx];
            uint256 priceBefore = _readPoolPrice(c, 0);   // spot, for loss accounting
            uint256 tokenIn = _tokenInForBuckOut(c, remaining);
            if (tokenIn == 0 || tokenIn > perPoolTok[bestIdx]) {
                tokenIn = perPoolTok[bestIdx];   // sell all available here
            }
            (uint256 spent, uint256 received) = _swapTokenForBuckExactIn(c, tokenIn);
            perPoolTok[bestIdx] -= spent;
            gained += received;
            uint256 spentValue = UniswapV3OracleLib.mulDiv(
                spent, priceBefore, 10 ** c.decimals);
            if (spentValue > received) lossValue += spentValue - received;
            remaining = received >= remaining ? 0 : remaining - received;
            if (received == 0) break;   // no progress; avoid spinning
        }
    }

    /// @notice TOKEN-in to extract `buckOut` BUCK from `c.pool`, via the
    ///         constant-product exact-output formula on the pool's *actual*
    ///         reserves (full-range V3 ⇒ pool balances are the virtual
    ///         reserves), inflated by the pool fee.  Near-exact, so a single
    ///         exact-input swap clears the shortfall even on a thinned pool.
    ///         Returns `type(uint256).max` when the pool can't cover `buckOut`
    ///         (caller falls back to selling all available TOKEN).
    function _tokenInForBuckOut(Constituent storage c, uint256 buckOut)
        internal view returns (uint256 tokenIn)
    {
        uint256 tokRes  = IERC20(c.token).balanceOf(c.pool);
        uint256 buckRes = IERC20(address(buck)).balanceOf(c.pool);
        if (buckRes <= buckOut || tokRes == 0) return type(uint256).max;
        // ideal (fee-free) tokenIn = tokRes * buckOut / (buckRes - buckOut)
        uint256 ideal = UniswapV3OracleLib.mulDiv(tokRes, buckOut, buckRes - buckOut);
        // inflate by 1/(1-fee); +1 wei round-up so the swap delivers ≥ buckOut.
        tokenIn = UniswapV3OracleLib.mulDiv(ideal, 1e6, 1e6 - c.feeTier) + 1;
    }

    /// @notice Exact-input TOKEN→BUCK swap on `c.pool`.
    function _swapTokenForBuckExactIn(Constituent storage c, uint256 tokenIn)
        internal returns (uint256 spent, uint256 received)
    {
        if (!(tokenIn > 0)) revert TokenIn0();
        _swapCallbackPool = c.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(c.pool).swap(
            address(this),
            !c.buckIsToken0,                                  // zeroForOne for TOKEN→BUCK
            int256(tokenIn),                                  // positive ⇒ exact-input
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

    // --- Treasury re-LP (recycle-to-buy-low) ------------------------------ //

    /// @notice Convert `buckAmount` treasury BUCK into a treasury-owned LP
    ///         position in the most underweight pool: swap ~half for the pool's
    ///         TOKEN, LP both sides, and tag the minted L as treasury.  Only
    ///         the BUCK actually consumed leaves `treasuryBuckPending`; any
    ///         remainder stays pending for the next sweep.
    /// @dev    The TOKEN-side swap uses the internal BUCK/TOKEN pool.  When an
    ///         FX route is registered this is where the rebalancer plugs in a
    ///         deeper external path (see `BASKET-REDESIGN.md` §7).
    function _reinvestTreasury(uint256 buckAmount) internal {
        uint256 i = _mostUnderweightPool();
        Constituent storage c = constituents[i];

        (uint256 buckSpent, uint256 tok) = _swapBuckForTokenExactIn(c, buckAmount / 2);
        uint256 buckForLp = buckAmount - buckSpent;        // remainder pairs with TOKEN

        uint128 liquidity = _liquidityForAmounts(c, tok, buckForLp);
        if (!(liquidity > 0)) revert ReinvestL0();

        _callbackPool = c.pool;
        (uint256 a0, uint256 a1) = IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, liquidity, abi.encode(c.token));
        _callbackPool = address(0);

        c.treasuryLiquidity += liquidity;
        uint256 consumed = buckSpent + (c.buckIsToken0 ? a0 : a1);   // swap + LP BUCK side
        treasuryBuckPending -= consumed;
        emit TreasuryReinvested(i, consumed, liquidity);
    }

    /// @notice Exact-input BUCK→TOKEN swap on `c.pool`.
    function _swapBuckForTokenExactIn(Constituent storage c, uint256 buckIn)
        internal returns (uint256 spent, uint256 received)
    {
        if (!(buckIn > 0)) revert BuckIn0();
        _swapCallbackPool = c.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(c.pool).swap(
            address(this),
            c.buckIsToken0,                                  // zeroForOne for BUCK→TOKEN
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

    /// @notice Index of the pool most underweight by value/target ratio (empty
    ///         pools, ratio 0, sort first).  Target = basketAmount * spot price.
    function _mostUnderweightPool() internal view returns (uint256 idx) {
        int256 best = type(int256).max;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 v = _poolLpValue(c);
            uint256 p = _readPoolPrice(c, 0);
            uint256 tgt = UniswapV3OracleLib.mulDiv(c.basketAmount, p, 1e18);
            int256 ratio = tgt > 0 ? int256(v * 1e18 / tgt) : type(int256).max;
            if (ratio < best) { best = ratio; idx = i; }
        }
    }

    /// @notice BUCK value of a pool's reserves (full-range ⇒ pool balances are
    ///         the virtual reserves).  0 if unseeded.
    function _poolLpValue(Constituent storage c) internal view returns (uint256 valueBuck) {
        uint256 tokBal = IERC20(c.token).balanceOf(c.pool);
        if (tokBal == 0) return 0;
        uint256 spotPrice = _readPoolPrice(c, 0);
        uint256 buckBal = IERC20(address(buck)).balanceOf(c.pool);
        valueBuck = UniswapV3OracleLib.mulDiv(tokBal, spotPrice, 10 ** c.decimals) + buckBal;
    }

    /// @notice V3 liquidity for (tokenAmount, buckAmount) at the pool's current
    ///         price -- min of the two sides (leftover stays in the basket).
    function _liquidityForAmounts(Constituent storage c, uint256 tokenAmount, uint256 buckAmount)
        internal view returns (uint128)
    {
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(c.pool).slot0();
        uint160 sqrtLow  = BasketMath.getSqrtRatioAtTick(c.tickLower);
        uint160 sqrtHigh = BasketMath.getSqrtRatioAtTick(c.tickUpper);
        (uint256 amount0, uint256 amount1) = c.buckIsToken0
            ? (buckAmount, tokenAmount)
            : (tokenAmount, buckAmount);
        return BasketMath.getLiquidityForAmounts(sqrtP, sqrtLow, sqrtHigh, amount0, amount1);
    }

    // --- Migration / unwind ----------------------------------------------- //

    /// @notice (Scaffold stub) Hand the receipt authority to a successor basket.
    ///         The full LP + outstanding handoff lands with the migration pass.
    function adoptReceiptTo(address successor) external onlyGov {
        receipt.adopt(successor);
    }

    // --- V3 callbacks ----------------------------------------------------- //

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

    // --- Internal helpers ------------------------------------------------- //

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
        priceInBuck = BasketMath.getQuoteAtTick(
            tick, uint128(10 ** c.decimals), c.token, address(buck));
    }

    function consultTickExternal(address pool, uint32 secondsAgo) external view returns (int24) {
        return BasketMath.consult(pool, secondsAgo);
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
        int24 tick = BasketMath.consult(pool, secondsAgo);
        Constituent storage c = constituents[indexOf[_poolToToken(pool)] - 1];
        return BasketMath.getQuoteAtTick(
            tick, uint128(10 ** c.decimals), c.token, address(buck));
    }

    function _poolToToken(address pool) internal view returns (address) {
        for (uint256 i = 0; i < constituents.length; i++) {
            if (constituents[i].pool == pool) return constituents[i].token;
        }
        revert("unknown pool");
    }

    function _decreaseAndCollect(Constituent storage c, uint128 liquidity)
        internal returns (uint256 tokenOut, uint256 buckOut)
    {
        IUniswapV3Pool(c.pool).burn(c.tickLower, c.tickUpper, liquidity);
        (uint128 a0, uint128 a1) = IUniswapV3Pool(c.pool).collect(
            address(this), c.tickLower, c.tickUpper, type(uint128).max, type(uint128).max);
        if (c.buckIsToken0) {
            buckOut  = a0;
            tokenOut = a1;
        } else {
            tokenOut = a0;
            buckOut  = a1;
        }
    }
}
