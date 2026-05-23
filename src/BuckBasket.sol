// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IUniswapV3Pool}    from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";
import {IERC20}            from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC721}           from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {Math}              from "@openzeppelin/contracts/utils/math/Math.sol";

import {UniswapV3OracleLib} from "./lib/UniswapV3OracleLib.sol";
import {BuckBasketReceipt}  from "./BuckBasketReceipt.sol";

interface IUniswapV3Factory {
    function createPool(address tokenA, address tokenB, uint24 fee) external returns (address pool);
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address pool);
    function feeAmountTickSpacing(uint24 fee) external view returns (int24);
}

interface IUniswapV3MintCallback {
    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata data) external;
}

interface IBuckMintBurn {
    function mintFromBasket(address to, uint256 amount) external;
    function burnFromBasket(uint256 amount) external;
    function balanceOf(address) external view returns (uint256);
}

interface IBuckKControllerDirect {
    function compute() external returns (uint256);
    function reprime() external;
}

/// @title BuckBasket -- USD-free basket registry + direct-mint orchestrator.
///
/// @notice Holds the system's TOKEN/BUCK Uniswap V3 pools, custodies the
///         single Buck-owned full-range liquidity position in each pool,
///         and orchestrates the direct-mint / redeem flow.
///
/// # Two kinds of BUCK
///
/// 1.  **BuckBasket-minted BUCKs** — minted via `Buck.mintFromBasket()`
///     when TOKEN liquidity is deposited.  Tracked in
///     `totalOutstandingBuck`; burned on redemption.
///
/// 2.  **Externally-supplied BUCKs** — created by normal `Buck.mint()`
///     backed by insured assets (BuckCredit).  Flow into the pools
///     through external arbitrage; grow basket NAV beyond the basket-
///     minted total.
///
/// # Economic model
///
/// A depositor's BUCK principal entitles them to a proportional share
/// of total LP NAV:
///
///     valueClaim = redeemBuck * NAV / totalOutstandingBuck
///
/// NAV grows from AMM swap fees, external-arb BUCK influx (price-up
/// moves leave BUCK behind in pools; price-down moves draw BUCK out),
/// and reinvested treasury BUCK from prior redemptions.  A depositor's
/// profit is the spread between deposit-time and redemption-time
/// NAV/outstanding ratios.
///
/// # Redemption ("sell high, recycle to buy low")
///
/// The value claim is allocated across overweight pools in proportion
/// to each pool's positive value-weight error (a P-controller pulling
/// the basket toward target weights).  For each overweight pool the
/// basket touches:
///
///   * Withdraws an L slice equal to that pool's share of the value
///     claim.
///   * Transfers the entire TOKEN side of that withdrawal to the
///     depositor.
///   * Adds the entire BUCK side to a running treasury bucket.
///
/// After the loop, `redeemBuck` of BUCK is burned from the bucket
/// (closing principal); the rest is swapped into the most underweight
/// pool's TOKEN, re-minted as new BUCK against that TOKEN, and LP'd as
/// a treasury-owned position — the "buy low" leg in the same tx.
///
/// The depositor/treasury split is *approximately* 50/50: full-range
/// V3 LP is roughly 50/50 by value at any price, so the TOKEN side
/// (depositor) and BUCK side (treasury) are roughly equal.  The
/// principal burn comes out of the BUCK side, slightly favoring the
/// depositor.
///
/// # Invariant
///
/// After all deposits exit, `totalOutstandingBuck == 0` and remaining
/// LP value is entirely treasury-owned — compounded TOKEN appreciation,
/// accumulated AMM fees, and external-arb BUCK that didn't get claimed.
///
/// # Known gaps (to address during redesign; see inline `BUG #N:` tags)
///
/// * #5  Slippage guard runs on the depositor's original pool, not on
///       the pools the redemption actually withdraws from.
/// * #6  `_buckToLp` / `_swapTokenForBuckExactIn` swaps use the
///       permissive `MIN/MAX_SQRT_RATIO ± 1` price limit; should be
///       TWAP-bounded to resist sandwich attacks.
/// * #7  `_mostUnderweightPool` / `_mostOverweightPool` read spot
///       prices; should use TWAP to resist manipulation.
/// * #8  `_reinvestBuck` LPs into a single most-underweight pool;
///       should mirror the redemption's proportional allocation.
/// * #9  TOKEN deposits LP into the deposited token's own pool with no
///       underweight-routing — asymmetric with the BUCK-deposit path.
/// * #10 `Buck.mintFromBasket` bypasses BuckK's `fundingFactor` gate;
///       document or fold basket-minted BUCKs into the PID's accounting.
/// * #11 `_reinvestBuck` emits no event; downstream observers can't
///       distinguish treasury reinvestment from external arb trades.
/// * #13 Late-basket-life redemptions can revert with "pool depth too
///       thin" when the basket NAV is too low to cover the remaining
///       outstanding (genuine liquidity exhaustion, not dust); a
///       graceful tail-redemption mode is future work.
abstract contract BuckBasketAbstractGuards is IUniswapV3MintCallback {
    function uniswapV3MintCallback(uint256, uint256, bytes calldata) external virtual override;
}

interface IUniswapV3SwapCallback {
    function uniswapV3SwapCallback(int256, int256, bytes calldata) external;
}

contract BuckBasket is IUniswapV3MintCallback, IUniswapV3SwapCallback {

    // --- Constituents ----------------------------------------------------- //

    struct Constituent {
        address token;
        uint8   decimals;
        uint256 basketAmount;        // 18-dec; total of basketAmount*price = 1 BUCK at init
        uint256 initialPriceInBuck;  // 18-dec; quote token price in BUCK at addBasketToken
        uint24  feeTier;
        address pool;
        int24   tickLower;
        int24   tickUpper;
        bool    buckIsToken0;        // BUCK address ordering in the pool
        uint256 targetWeightBp;      // declared weight in basis points (0-10000)
    }

    Constituent[] public constituents;
    mapping(address => uint256) public indexOf;   // token -> 1+index (0 = not present)

    struct Deposit {
        uint256 buckPrincipal;     // 18-dec BUCK minted at deposit
        uint256 tokenPrincipal;    // native-dec token deposited (for ROI)
        address token;             // original deposit token
        uint64  depositTime;
    }
    mapping(uint256 => Deposit) public deposits;

    /// @notice Total outstanding BUCK principal across all active deposits.
    uint256 public totalOutstandingBuck;

    // --- Wiring ----------------------------------------------------------- //

    IBuckMintBurn          public immutable buck;
    BuckBasketReceipt      public immutable receipt;
    IBuckKControllerDirect public immutable controller;
    IUniswapV3Factory      public immutable v3Factory;
    address                public governance;

    uint24  public defaultFeeTier;
    uint32  public twapWindow;
    uint16  public observationCardinality;
    uint256 public defaultMaxDeviationBp;
    uint256 public minSeedLiquidity;

    /// @notice Redemption-value threshold (bp of the most-overweight
    ///         pool's LP value) below which `_allocateRedemption` uses
    ///         the gas-cheap single-pool fast path instead of the full
    ///         two-pass allocation across constituents.  100 bp = 1%.
    uint16 public constant SMALL_REDEEM_BP = 100;

    // --- Events ----------------------------------------------------------- //

    event BasketTokenAdded(
        address indexed token,
        uint256 weightBp,
        uint256 initialPriceInBuck,
        address pool
    );
    event Deposited(
        address indexed depositor,
        uint256 indexed receiptId,
        address indexed depositedToken,
        uint256 tokenAmount,          // native decimals
        uint256 buckAmount,            // 18-dec BUCK minted
        address lpToken,               // token actually LP'd into
        uint128 liquidity
    );
    /// @notice Aggregate per-redemption event.  See `RedeemedFromPool`
    ///         for the per-pool TOKEN payouts that compose this redemption.
    event Redeemed(
        address indexed depositor,
        uint256 indexed receiptId,
        uint256 burnedBuck,            // principal BUCK burned
        uint256 retainedBuck,          // treasury profit BUCK (reinvested)
        uint256 remainingBp            // 0 if fully redeemed; else NFT share
    );
    /// @notice Emitted once per pool a redemption touches.  Burn and
    ///         profit are aggregate (see `Redeemed`); per-pool events
    ///         carry TOKEN flows and L withdrawn for observability.
    ///         `tokenSwapped` is non-zero only when the basket had to
    ///         swap part of this pool's withdrawn TOKEN to BUCK during
    ///         aggregate shortfall coverage.
    event RedeemedFromPool(
        uint256 indexed receiptId,
        address indexed pool,
        address indexed token,
        uint256 tokenToUser,           // native-dec; transferred to depositor
        uint256 tokenSwapped,          // native-dec; TOKEN→BUCK for shortfall
        uint128 burnedLiquidity        // L removed from basket's position
    );

    // --- Constants -------------------------------------------------------- //

    int24 internal constant MIN_TICK = -887272;
    int24 internal constant MAX_TICK =  887272;
    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;

    /// @notice Slippage buffer (bp of shortfall) applied to the TOKEN
    ///         input estimated for `_coverShortfallAggregate`'s exact-
    ///         input swap.  Robust under V3 fee + price-impact noise.
    uint256 internal constant SHORTFALL_BUFFER_BP = 200;   // 2%

    /// @notice Floor for `_reinvestBuck`.  Profit below this threshold
    ///         stays in the basket's BUCK balance (still treasury-owned;
    ///         the next redemption sweeps it as additional totalBuckOut)
    ///         rather than going through `_buckToLp` where V3-precision
    ///         noise can cause callback failures at small amounts.
    uint256 internal constant MIN_REINVEST_BUCK = 1e15;    // 0.001 BUCK

    /// @notice Residual shortfall tolerated when `_coverShortfallAggregate`
    ///         can't fully cover the gap because the touched pools have
    ///         no usable TOKEN balance left (e.g., single-pool full
    ///         redemption drains the pool; V3 burn leaves 1-2 wei dust).
    ///         The depositor's principal is decremented by the shortfall;
    ///         `totalOutstandingBuck` is decremented by the actual burn
    ///         (= redeemBuck - shortfall_dust), so cumulative orphan
    ///         supply is bounded by MAX_ORPHAN_DUST_WEI per redemption.
    uint256 internal constant MAX_ORPHAN_DUST_WEI = 1e6;   // 0.000001 BUCK

    // --- Mint callback re-entry guard ------------------------------------- //
    address internal _callbackPool;       // mint callback guard
    address internal _swapCallbackPool;    // swap callback guard

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
        buck               = IBuckMintBurn(_buck);
        controller         = IBuckKControllerDirect(_controller);
        v3Factory          = IUniswapV3Factory(_v3Factory);
        governance         = _governance;
        defaultFeeTier     = _defaultFeeTier;
        twapWindow         = _twapWindow;
        observationCardinality = _observationCardinality;
        defaultMaxDeviationBp  = _defaultMaxDeviationBp;
        minSeedLiquidity       = _minSeedLiquidity;

        receipt = new BuckBasketReceipt(address(this));
    }

    // --- Configuration (governance) --------------------------------------- //

    /// @notice Add a basket constituent.
    /// @dev    `weightBp` is the declared target weight in basis points
    ///         (1 bp = 0.01%).  Pass 0 to default to an equal share:
    ///         `10000 / N` for the resulting N-constituent basket.
    ///
    ///         Existing constituents' declared weights are rescaled
    ///         proportionally so the total remains 10000 bp, preserving
    ///         their relative weight ratios.  Every existing constituent's
    ///         `basketAmount` is then recomputed at its *current* pool
    ///         spot price so its contribution equals its declared weight
    ///         share of 1.0 BUCK.  The new constituent is set at
    ///         `initialPriceInBuck`.
    ///
    ///         Because existing constituents are re-priced at the current
    ///         spot, price drift between additions is locked into
    ///         `basketAmount`.  A future `rebalanceWeights()` (governance)
    ///         will allow re-weighting or removing one or more constituents,
    ///         recomputing all `basketAmount` values from current spot
    ///         prices so every constituent matches its declared weight
    ///         share of 1.0 BUCK.  Passing a weight of 0 removes a token.
    function addBasketToken(
        address token,
        uint8   decimals,
        uint256 initialPriceInBuck,
        uint256 weightBp,
        uint24  feeTier
    ) external returns (address pool) {
        require(msg.sender == governance, "Not governance");
        require(token != address(0) && token != address(buck), "bad token");
        require(weightBp <= 10000, "bad weight");
        require(indexOf[token] == 0, "already present");
        require(initialPriceInBuck > 0, "bad price");

        // --- resolve target weight -------------------------------------- //
        // weightBp == 0  =>  equal share for all N constituents.
        uint256 N = constituents.length + 1;
        uint256 newW = weightBp > 0 ? weightBp : 10000 / N;
        // Total existing declared weight (before rescaling).
        uint256 existingSumW = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            existingSumW += constituents[i].targetWeightBp;
        }

        // --- renormalize existing constituents --------------------------- //
        // Each existing constituent's declared weight is scaled so the
        // total across all N constituents is 10000 bp.  Its basketAmount
        // is then recomputed at the *current* pool spot price so its
        // contribution to basketValueInBuck equals its declared share.
        if (constituents.length > 0) {
            uint256 remaining = 10000 - newW;
            require(remaining > 0 || newW == 10000, "no remaining weight");
            for (uint256 i = 0; i < constituents.length; i++) {
                Constituent storage c = constituents[i];
                uint256 scaledW = existingSumW > 0
                    ? c.targetWeightBp * remaining / existingSumW
                    : 0;
                c.targetWeightBp = scaledW;
                // basketAmount = scaledW / 10000 / spotPrice  (18-dec)
                uint256 spotPrice = _readPoolPrice(c, 0);
                require(spotPrice > 0, "pool price = 0");
                uint256 weightUnit =
                    UniswapV3OracleLib.mulDiv(scaledW, 1e18, 10000);
                c.basketAmount =
                    UniswapV3OracleLib.mulDiv(weightUnit, 1e18, spotPrice);
            }
        }

        // --- new constituent --------------------------------------------- //
        // basketAmount = newW / 10000 / initialPriceInBuck
        uint256 weightUnit =
            UniswapV3OracleLib.mulDiv(newW, 1e18, 10000);
        uint256 basketAmount =
            UniswapV3OracleLib.mulDiv(weightUnit, 1e18, initialPriceInBuck);

        // Create / locate the V3 pool.  Order tokens by address.
        pool = _findOrCreatePool(token, feeTier);

        // Initialise the pool at the declared initial price.  initialize()
        // reverts if already initialised, so we silently no-op if a pool
        // for this (token, fee) pair already had a price.
        uint160 sqrtPriceX96 = _sqrtPriceFromBuckRate(
            address(buck) < token,   // buckIsToken0
            initialPriceInBuck,
            decimals
        );
        try IUniswapV3Pool(pool).initialize(sqrtPriceX96) {} catch {}

        // Bump observation cardinality so TWAP reads are immediately
        // possible after the first swap.  This is best-effort; if the
        // pool already has equal-or-greater cardinality the call is a
        // no-op inside V3.
        IUniswapV3Pool(pool).increaseObservationCardinalityNext(observationCardinality);

        // Compute and store the tick bounds for the full-range position.
        int24 spacing = v3Factory.feeAmountTickSpacing(feeTier);
        int24 tickLower = (MIN_TICK / spacing) * spacing;
        int24 tickUpper = (MAX_TICK / spacing) * spacing;

        constituents.push(Constituent({
            token: token,
            decimals: decimals,
            basketAmount: basketAmount,
            initialPriceInBuck: initialPriceInBuck,
            feeTier: feeTier,
            pool: pool,
            tickLower: tickLower,
            tickUpper: tickUpper,
            buckIsToken0: address(buck) < token,
            targetWeightBp: newW
        }));
        indexOf[token] = constituents.length;

        // Re-prime the controller so the dilution discontinuity doesn't
        // manifest as a single-cycle P/I spike.
        controller.reprime();

        emit BasketTokenAdded(token, newW, initialPriceInBuck, pool);
    }

    /// @notice (Future) Re-weight or remove basket constituents.
    /// @dev    Accepts one or more (token, weightBp) pairs.  Weight 0
    ///         removes the token.  The remaining declared-weight budget
    ///         is distributed to unchanged constituents proportionally.
    ///         All `basketAmount` values are recomputed at current spot
    ///         prices so each constituent's contribution equals its
    ///         declared share of 1.0 BUCK.
    //
    // function rebalanceWeights(
    //     address[] calldata tokens,
    //     uint256[] calldata weightBps
    // ) external {
    //     require(msg.sender == governance, "Not governance");
    //     require(tokens.length == weightBps.length, "length mismatch");
    //     // ... renormalize all targetWeightBp, recompute basketAmount ...
    // }

    function setGovernance(address _governance) external {
        require(msg.sender == governance, "Not governance");
        require(_governance != address(0), "gov=0");
        governance = _governance;
    }

    function constituentsLength() external view returns (uint256) {
        return constituents.length;
    }

    // --- Process variable for the controller ----------------------------- //

    /// @notice Sum of basketAmount_i * pool_price_i, in 18-dec BUCK.  This
    ///         is the direct-embodiment "process variable" -- the value of
    ///         one basket bundle measured in BUCK.
    function basketValueInBuck() external view returns (int256) {
        if (constituents.length == 0) return int256(1e18);
        return _currentBasketValueAtCurrentPrices();
    }

    function _currentBasketValueAtCurrentPrices() internal view returns (int256) {
        int256 total = 0;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 priceInBuck = _readPoolPrice(c, twapWindow);
            total += int256(UniswapV3OracleLib.mulDiv(c.basketAmount, priceInBuck, 1e18));
        }
        return total;
    }

    // --- Pool valuation helpers ---------------------------------------- //

    /// @notice Total BUCK value of the BuckBasket's LP in one pool.
    ///         0 if the pool has no token reserves (empty / not yet seeded).
    function _poolLpValue(Constituent storage c) internal view
        returns (uint256 valueBuck)
    {
        uint256 tokBal = IERC20(c.token).balanceOf(c.pool);
        if (tokBal == 0) return 0;
        uint256 spotPrice = _readPoolPrice(c, 0);
        uint256 buckBal = IERC20(address(buck)).balanceOf(c.pool);
        valueBuck = UniswapV3OracleLib.mulDiv(tokBal, spotPrice, 10 ** c.decimals)
                    + buckBal;
    }

    /// @notice Total BUCK value across all TOKEN/BUCK pools.
    function _totalBasketLpValue() internal view returns (uint256 total) {
        for (uint256 i = 0; i < constituents.length; i++) {
            total += _poolLpValue(constituents[i]);
        }
    }

    /// @notice Index of the pool whose LP is most underweight relative to
    ///         its price-adjusted target value.  Empty pools (value=0)
    ///         are always first.
    /// @dev    Ratio: actual value / target value (lower = more
    ///         underweight).  Target value = basketAmount * priceInBuck,
    ///         matching `_poolWeightErrors` and `basket_model.py`.
    function _mostUnderweightPool() internal view returns (uint256 idx) {
        int256 best = type(int256).max;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 v = _poolLpValue(c);
            uint256 p = _readPoolPrice(c, 0);
            uint256 tgt = UniswapV3OracleLib.mulDiv(c.basketAmount, p, 1e18);
            int256 ratio = tgt > 0
                ? int256(v * 1e18 / tgt)
                : type(int256).max;
            // Empty pools have v=0, so ratio=0 (most underweight).
            if (ratio < best) {
                best = ratio;
                idx = i;
            }
        }
    }

    /// @notice Index of the pool whose LP is most overweight relative to
    ///         its price-adjusted target value.  Skips empty pools.
    function _mostOverweightPool() internal view returns (uint256 idx) {
        int256 best = -1;
        for (uint256 i = 0; i < constituents.length; i++) {
            Constituent storage c = constituents[i];
            uint256 v = _poolLpValue(c);
            if (v == 0) continue;
            uint256 p = _readPoolPrice(c, 0);
            uint256 tgt = UniswapV3OracleLib.mulDiv(c.basketAmount, p, 1e18);
            if (tgt == 0) continue;
            int256 ratio = int256(v * 1e18 / tgt);
            if (ratio > best) {
                best = ratio;
                idx = i;
            }
        }
        require(best >= 0, "no overweight pool");
    }

    // --- Direct mint ----------------------------------------------------- //

    /// @notice Deposit a basket token.  Mints BUCK at the token's current
    ///         pool spot price and adds (token, BUCK) liquidity to that
    ///         pool.  The depositor receives an NFT representing their
    ///         share of total basket value.
    ///
    ///         Cross-pool rebalancing ("buy low, sell high") is deferred
    ///         to a follow-up change.  The helpers _mostUnderweightPool(),
    ///         _mostOverweightPool(), _reinvestBuck(), and the swap callback
    ///         are already in place for that work.
    ///
    /// @param  maxDeviationBp 0 = skip TWAP guard (bootstrap / cold pool).
    ///         Non-zero rejects the deposit if |spot - TWAP| exceeds
    ///         maxDeviationBp / 10000 of TWAP, protecting large deposits
    ///         from being front-run at a stale pool price.
    function depositToken(
        address token,
        uint256 tokenAmount,
        uint256 maxDeviationBp
    ) external returns (uint256 receiptId) {
        require(tokenAmount > 0, "amount=0");

        // --- BUCK deposit: swap BUCK -> underweight token, then LP ------ //
        if (token == address(buck)) {
            // Pull user's already-minted, fully-backed BUCKs.
            IERC20(address(buck)).transferFrom(
                msg.sender, address(this), tokenAmount);

            (Constituent storage tgtC, uint256 tgtTok, uint256 buckToMint) =
                _buckToLp(tokenAmount);

            receiptId = receipt.mint(msg.sender);
            deposits[receiptId] = Deposit({
                buckPrincipal: buckToMint,
                tokenPrincipal: tgtTok,
                token: tgtC.token,
                depositTime: uint64(block.timestamp)
            });
            totalOutstandingBuck += buckToMint;
            controller.compute();
            emit Deposited(msg.sender, receiptId, address(buck),
                           tokenAmount, buckToMint, tgtC.token, 0);
            return receiptId;
        }

        // --- TOKEN deposit: standard single-pool LP ---------------------- //
        uint256 depositIdx = indexOf[token];
        require(depositIdx > 0, "not in basket");
        Constituent storage c = constituents[depositIdx - 1];

        uint256 spotPrice = _readPoolPrice(c, 0);
        _enforceSlippageGuard(c, spotPrice, maxDeviationBp);

        uint256 buckToMint = UniswapV3OracleLib.mulDiv(
            tokenAmount, spotPrice, 10 ** c.decimals
        );
        require(buckToMint > 0, "buck=0");

        IERC20(token).transferFrom(msg.sender, address(this), tokenAmount);
        buck.mintFromBasket(address(this), buckToMint);

        uint128 liquidity = _liquidityForAmounts(c, tokenAmount, buckToMint);
        require(liquidity > 0, "L=0");
        if (_isFirstPositionInPool(c)) {
            require(liquidity >= minSeedLiquidity, "seed too small");
        }

        _callbackPool = c.pool;
        IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, liquidity,
            abi.encode(token)
        );
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
        emit Deposited(msg.sender, receiptId, token, tokenAmount,
                       buckToMint, c.token, liquidity);
    }

    // --- Redemption allocation ------------------------------------------- //

    /// @notice Allocate `redeemValue` BUCK across constituent pools.
    ///         Each `alloc[i]` is the BUCK-value to extract from pool i;
    ///         Σ ≈ redeemValue (modulo integer-division dust).
    ///
    /// # Model
    ///
    /// Ideal post-redemption state: every pool sits at its target share
    /// of the smaller, post-redemption NAV.  That is,
    ///     alloc_i = v_i - t_i × (NAV - redeemValue)
    /// where `t_i = basketAmount_i × price_i / Σ(...)` is the pool's
    /// natural value share (same definition as
    /// `_currentBasketValueAtCurrentPrices` and `basket_model.py`).
    ///
    ///   * Heavily overweight pool: alloc_i > t_i × redeemValue
    ///     (pool contributes more than its proportional share — pulls
    ///     the basket toward target).
    ///   * At-target pool: alloc_i = t_i × redeemValue (proportional).
    ///   * Heavily underweight pool: alloc_i < 0 (would require ADDING
    ///     value — clamp to 0, redistribute the slack).
    ///
    /// Algorithm (one shot — full-range LP withdrawal doesn't move pool
    /// prices, so a static computation matches the loop's behaviour):
    ///
    ///   Pass 1 — Take from pools with positive ideal allocation
    ///   (overweight + slightly underweight), proportional to their
    ///   excess over the post-redemption target.
    ///
    ///   Pass 2 — If pass 1's positives didn't cover `redeemValue`
    ///   (heavily-underweight pools clamped to zero), take the
    ///   remainder proportional to current pool value across ALL pools.
    ///   This handles the equilibrium case (all at target ⇒ pure
    ///   proportional contribution) and the rare "anti-rebalancing"
    ///   case where even underweight pools must contribute.
    ///
    /// # Small-redemption fast path
    ///
    /// If `redeemValue` is below `SMALL_REDEEM_BP` of the most
    /// overweight pool's value, satisfy the entire redemption from
    /// that single pool.  Saves the dominant-case gas of running the
    /// full multi-pool allocation when the impact is small.
    function _allocateRedemption(uint256 redeemValue, uint256 navTotal)
        internal view returns (uint256[] memory alloc)
    {
        uint256 N = constituents.length;
        alloc = new uint256[](N);
        if (redeemValue == 0 || navTotal == 0) return alloc;

        // Gather per-pool value + target value.
        uint256 totalTarget = 0;
        uint256[] memory v = new uint256[](N);
        uint256[] memory targetVal = new uint256[](N);
        for (uint256 i = 0; i < N; i++) {
            Constituent storage c = constituents[i];
            v[i] = _poolLpValue(c);
            uint256 p = _readPoolPrice(c, 0);
            targetVal[i] = UniswapV3OracleLib.mulDiv(
                c.basketAmount, p, 1e18);
            totalTarget += targetVal[i];
        }
        if (totalTarget == 0) return alloc;

        // Compute each pool's "ideal" allocation (positive part) and
        // find the most overweight pool for the fast-path threshold.
        // postNav = NAV - redeemValue (clamped at 0).
        uint256 postNav = navTotal > redeemValue ? navTotal - redeemValue : 0;
        uint256[] memory positive = new uint256[](N);
        uint256 totalPositive = 0;
        uint256 mostOvIdx = 0;
        uint256 mostOvAbsExcess = 0;
        for (uint256 i = 0; i < N; i++) {
            uint256 tgtPost = UniswapV3OracleLib.mulDiv(
                targetVal[i], postNav, totalTarget);
            if (v[i] > tgtPost) {
                positive[i] = v[i] - tgtPost;
                totalPositive += positive[i];
            }
            // Track the most overweight pool by absolute excess over
            // its CURRENT target (not post-redemption target) for the
            // fast-path threshold check.
            uint256 tgtNow = UniswapV3OracleLib.mulDiv(
                targetVal[i], navTotal, totalTarget);
            if (v[i] > tgtNow && v[i] - tgtNow > mostOvAbsExcess) {
                mostOvAbsExcess = v[i] - tgtNow;
                mostOvIdx = i;
            }
        }

        // Fast path: small redemption against the most overweight pool.
        // Skips the multi-pool allocation entirely.
        if (mostOvAbsExcess > 0
            && redeemValue * 10000
                <= uint256(SMALL_REDEEM_BP) * v[mostOvIdx]) {
            alloc[mostOvIdx] = redeemValue;
            return alloc;
        }

        // Pass 1: allocate by positive ideal allocation.
        uint256 fromPositive = totalPositive >= redeemValue
            ? redeemValue : totalPositive;
        if (fromPositive > 0) {
            for (uint256 i = 0; i < N; i++) {
                if (positive[i] > 0) {
                    alloc[i] = UniswapV3OracleLib.mulDiv(
                        positive[i], fromPositive, totalPositive);
                }
            }
        }

        // Pass 2: if pass 1 didn't cover redeemValue (because some
        // pools were heavily underweight and clamped to zero), allocate
        // the remainder proportional to current pool value.  Handles
        // the equilibrium fallback (totalPositive == 0 ⇒ all from
        // pass 2) and the "must dip into underweight" case.
        if (redeemValue > totalPositive) {
            uint256 remainder = redeemValue - totalPositive;
            for (uint256 i = 0; i < N; i++) {
                alloc[i] += UniswapV3OracleLib.mulDiv(
                    v[i], remainder, navTotal);
            }
        }
    }

    // --- Redemption phase helpers ---------------------------------------- //

    /// @notice Validate the receipt and compute the BUCK principal to
    ///         redeem.  Extracted so `redeem()` stays small.
    function _validateAndSize(uint256 receiptId, uint256 redeemBp)
        internal view returns (
            Deposit memory d,
            uint256 redeemBuck,
            uint256 redeemShare)
    {
        require(receipt.ownerOf(receiptId) == msg.sender, "not owner");
        d = deposits[receiptId];
        require(d.buckPrincipal > 0, "empty deposit");
        redeemShare = redeemBp > 0 ? redeemBp : 10000;
        require(redeemShare <= 10000, "redeemBp > 10000");
        redeemBuck = redeemShare == 10000
            ? d.buckPrincipal
            : d.buckPrincipal * redeemShare / 10000;
        require(redeemBuck > 0, "redeem zero");
    }

    /// @notice Phase 1 of redeem(): burn LP from each pool with
    ///         non-zero allocation.  Holds the resulting (TOKEN, BUCK)
    ///         in the basket — no transfers yet.
    /// @return perPoolTok    Native-dec TOKEN withdrawn per pool.
    /// @return perPoolBurnL  L units burned per pool (for telemetry).
    /// @return totalBuckOut  Aggregate BUCK side from all withdrawals.
    function _burnAllPoolLP(uint256[] memory alloc)
        internal returns (
            uint256[] memory perPoolTok,
            uint128[] memory perPoolBurnL,
            uint256 totalBuckOut)
    {
        uint256 N = constituents.length;
        perPoolTok   = new uint256[](N);
        perPoolBurnL = new uint128[](N);
        for (uint256 i = 0; i < N; i++) {
            if (alloc[i] == 0) continue;
            Constituent storage pc = constituents[i];
            uint256 poolVal = _poolLpValue(pc);
            if (poolVal == 0) continue;
            uint256 frac = alloc[i] * 1e18 / poolVal;
            if (frac > 1e18) frac = 1e18;

            (uint128 totalL,,,,) = IUniswapV3Pool(pc.pool).positions(
                keccak256(abi.encodePacked(address(this),
                              pc.tickLower, pc.tickUpper))
            );
            if (totalL == 0) continue;
            uint128 burnL = uint128(uint256(totalL) * frac / 1e18);
            if (burnL == 0) continue;

            (uint256 tok, uint256 b) = _decreaseAndCollect(pc, burnL);
            perPoolTok[i]   = tok;
            perPoolBurnL[i] = burnL;
            totalBuckOut   += b;
        }
    }

    /// @notice Phase 4 of redeem(): transfer each pool's remaining
    ///         TOKEN balance to the depositor; emit `RedeemedFromPool`
    ///         per non-empty pool.
    /// @dev    `perPoolTokInitial[i]` is the TOKEN held before any
    ///         shortfall-cover swap; `perPoolTok[i]` is what survives.
    ///         The difference is `tokenSwapped` for the event.
    function _payDepositors(
        uint256 receiptId,
        uint256[] memory perPoolTok,
        uint256[] memory perPoolTokInitial,
        uint128[] memory perPoolBurnL
    ) internal {
        uint256 N = constituents.length;
        for (uint256 i = 0; i < N; i++) {
            if (perPoolBurnL[i] == 0) continue;
            Constituent storage pc = constituents[i];
            uint256 tokToUser = perPoolTok[i];
            uint256 tokSwapped = perPoolTokInitial[i] - perPoolTok[i];
            if (tokToUser > 0) {
                IERC20(pc.token).transfer(msg.sender, tokToUser);
            }
            emit RedeemedFromPool(
                receiptId, pc.pool, pc.token,
                tokToUser, tokSwapped, perPoolBurnL[i]);
        }
    }

    /// @notice Phase 6 of redeem(): finalize deposit state.  Full
    ///         redemption clears the receipt; partial scales down
    ///         BUCK + TOKEN principals proportionally.
    function _finalizeDeposit(
        uint256 receiptId,
        Deposit memory d,
        uint256 redeemBuck,
        uint256 redeemShare
    ) internal {
        if (redeemShare == 10000) {
            delete deposits[receiptId];
            receipt.burn(receiptId);
        } else {
            uint256 scaledT = d.tokenPrincipal * redeemShare / 10000;
            deposits[receiptId].buckPrincipal -= redeemBuck;
            deposits[receiptId].tokenPrincipal -= scaledT;
        }
    }

    /// @dev Helper for `redeem()`: true if any pool was actually
    ///      withdrawn from (any `burnL > 0`).
    function _anyBurned(uint128[] memory perPoolBurnL)
        internal pure returns (bool)
    {
        for (uint256 i = 0; i < perPoolBurnL.length; i++) {
            if (perPoolBurnL[i] > 0) return true;
        }
        return false;
    }

    // --- Redemption (P-controller: sell overweight pools proportionally) -- //

    /// @notice Redeem all or part of a basket receipt.  See the contract-
    ///         level natspec for the full economic model; briefly:
    ///
    ///             valueClaim = redeemBuck * NAV / totalOutstandingBuck
    ///
    ///         Withdrawals are allocated across overweight pools in
    ///         proportion to each pool's positive value-weight error.
    ///         The depositor receives the TOKEN side(s); the treasury
    ///         keeps the BUCK side minus the principal burn.  Excess
    ///         BUCK is swapped to the most underweight TOKEN and re-LP'd
    ///         as a treasury position.
    ///
    /// @param  redeemBp        0 = redeem entire deposit; otherwise the
    ///                         basis-point fraction of `d.buckPrincipal`
    ///                         to redeem.
    /// @param  maxDeviationBp  0 = skip TWAP guard.  Non-zero enforces
    ///                         |spot - TWAP| < maxDeviationBp/10000 of
    ///                         TWAP on the *depositor's original pool*
    ///                         (gap #5: should also guard the pools we
    ///                         actually withdraw from).
    function redeem(uint256 receiptId, uint256 redeemBp,
                    uint256 maxDeviationBp) external {
        // Phase 0: validate + size.
        (Deposit memory d, uint256 redeemBuck, uint256 redeemShare) =
            _validateAndSize(receiptId, redeemBp);

        // Phase 1: slippage guard (deposit pool — gap #5: TODO per-withdrawal).
        {
            Constituent storage depC = constituents[indexOf[d.token] - 1];
            _enforceSlippageGuard(
                depC, _readPoolPrice(depC, 0), maxDeviationBp);
        }

        // Phase 2: value claim + per-pool allocation.
        uint256 redeemValue;
        uint256[] memory alloc;
        {
            uint256 navTotal = _totalBasketLpValue();
            require(navTotal > 0, "no LP value");
            redeemValue = UniswapV3OracleLib.mulDiv(
                redeemBuck, navTotal, totalOutstandingBuck);
            alloc = _allocateRedemption(redeemValue, navTotal);
        }

        // Phase 3: burn LP from each allocated pool.  Holds TOKEN + BUCK
        // in basket — no depositor transfers yet, so any shortfall can
        // still reach back into the basket's TOKEN holdings.
        (uint256[] memory perPoolTok,
         uint128[] memory perPoolBurnL,
         uint256 totalBuckOut) = _burnAllPoolLP(alloc);
        require(_anyBurned(perPoolBurnL), "no LP withdrawn");

        // Snapshot pre-swap TOKEN balances for per-pool event telemetry.
        uint256[] memory perPoolTokInitial = new uint256[](perPoolTok.length);
        for (uint256 i = 0; i < perPoolTok.length; i++) {
            perPoolTokInitial[i] = perPoolTok[i];
        }

        // Phase 4: aggregate shortfall cover (exact-input TOKEN→BUCK
        // with slippage buffer).  Robust for any non-zero pool depth.
        // If `totalBuckOut` is still below `redeemBuck` after the
        // greedy swap pass, the residual is treated as orphan dust
        // (bounded by MAX_ORPHAN_DUST_WEI) — e.g., single-pool full
        // redemption where the pool is V3-burn-drained.
        if (totalBuckOut < redeemBuck) {
            totalBuckOut = _coverShortfallAggregate(
                perPoolTok, redeemBuck - totalBuckOut, totalBuckOut);
        }

        // Phase 5: burn what's actually available.  Strict invariant
        // when no orphan; bounded drift otherwise.
        uint256 actualBurn = totalBuckOut >= redeemBuck
            ? redeemBuck
            : totalBuckOut;
        uint256 orphan = redeemBuck - actualBurn;
        require(orphan <= MAX_ORPHAN_DUST_WEI, "pool depth too thin");
        if (actualBurn > 0) buck.burnFromBasket(actualBurn);
        uint256 profit = totalBuckOut - actualBurn;  // ≥ 0 by construction

        // Phase 6: pay depositor each pool's surviving TOKEN; emit
        // per-pool events.
        _payDepositors(
            receiptId, perPoolTok, perPoolTokInitial, perPoolBurnL);

        // Phase 7: reinvest profit only above the precision floor;
        // sub-floor BUCK stays in basket and is swept by the next
        // redemption (still treasury-owned, just not yet LP'd).
        if (profit >= MIN_REINVEST_BUCK) {
            _reinvestBuck(profit);
        }

        // Phase 8: finalize deposit + outstanding bookkeeping.
        // Outstanding decrements by actual burn so the invariant
        // `totalOutstandingBuck == 0 after all exits` holds modulo
        // bounded orphan dust per redemption.
        _finalizeDeposit(receiptId, d, redeemBuck, redeemShare);
        totalOutstandingBuck -= actualBurn;

        // Phase 9: keep PID warm + emit aggregate.
        controller.compute();
        emit Redeemed(
            msg.sender, receiptId,
            actualBurn, profit,
            redeemShare == 10000 ? 0 : 10000 - redeemShare
        );
    }

    // --- Shared: BUCK -> underweight-token LP ---------------------------- //

    /// @notice Swap BUCKs already held by the BuckBasket for the most
    ///         underweight pool's token, mint new BUCKs against that token,
    ///         and LP both.  Returns the LP constituent and amounts.
    ///         Caller decides whether to issue an NFT / update outstanding.
    function _buckToLp(uint256 buckAmount)
        internal
        returns (Constituent storage tgtC, uint256 tgtTok, uint256 buckToMint)
    {
        require(buckAmount > 0, "buckAmount=0");

        uint256 tgtIdx = _mostUnderweightPool();
        tgtC = constituents[tgtIdx];

        // Swap BUCK -> target token on the target pool.  Callback data
        // carries the constituent token address; the callback uses
        // `buckIsToken0` to decide which side to pay.
        //
        // sqrtPriceLimit selection (V3 SPL rule):
        //   zeroForOne=true  (price moves DOWN) ⇒ limit < current ⇒ MIN_SQRT_RATIO+1
        //   zeroForOne=false (price moves UP)   ⇒ limit > current ⇒ MAX_SQRT_RATIO-1
        // BUCK→TOKEN with buckIsToken0=true:  zeroForOne=true  ⇒ MIN+1.
        // BUCK→TOKEN with buckIsToken0=false: zeroForOne=false ⇒ MAX-1.
        // (Gap #6 will TWAP-bound this in place of the permissive extremes.)
        _swapCallbackPool = tgtC.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(tgtC.pool).swap(
            address(this), tgtC.buckIsToken0,
            int256(buckAmount),
            tgtC.buckIsToken0 ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1,
            abi.encode(tgtC.token)
        );
        _swapCallbackPool = address(0);
        // Sign convention: TOKEN-side delta is NEGATIVE after a
        // BUCK→TOKEN swap (pool sent us TOKEN).  Extract its absolute
        // value as the received amount.
        int256 tokenDelta = tgtC.buckIsToken0 ? d1 : d0;
        require(tokenDelta < 0, "swap BUCK->token failed");
        tgtTok = uint256(-tokenDelta);

        // Derive `buckToMint` and `liquidity` from a SINGLE sqrtPriceX96
        // read so the resulting V3 mint amounts match what we hold
        // exactly (the old code mixed tick-quoted spotPrice for the
        // BUCK amount with sqrtPriceX96 for L, causing callback under-
        // transfers up to 0.6% on 60-tick pools).
        //
        // Strategy: bind L on the TOKEN side (we know `tgtTok` exactly),
        // then compute the partner BUCK amount from that L at the
        // current sqrtPriceX96.  V3 mint's reverse computation reads
        // the same sqrtPriceX96 → amounts match to the wei.
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(tgtC.pool).slot0();
        uint160 sqrtLow  = UniswapV3OracleLib.getSqrtRatioAtTick(tgtC.tickLower);
        uint160 sqrtHigh = UniswapV3OracleLib.getSqrtRatioAtTick(tgtC.tickUpper);
        uint128 liquidity;
        if (tgtC.buckIsToken0) {
            // TOKEN=token1: L from amount1 over [sqrtLow, sqrtP].
            liquidity = UniswapV3OracleLib.getLiquidityForAmount1(
                sqrtLow, sqrtP, tgtTok);
            // BUCK=token0: matching amount0 over [sqrtP, sqrtHigh].
            buckToMint = UniswapV3OracleLib.getAmount0ForLiquidity(
                sqrtP, sqrtHigh, liquidity);
        } else {
            // TOKEN=token0: L from amount0 over [sqrtP, sqrtHigh].
            liquidity = UniswapV3OracleLib.getLiquidityForAmount0(
                sqrtP, sqrtHigh, tgtTok);
            // BUCK=token1: matching amount1 over [sqrtLow, sqrtP].
            buckToMint = UniswapV3OracleLib.getAmount1ForLiquidity(
                sqrtLow, sqrtP, liquidity);
        }
        require(liquidity > 0, "L=0");
        require(buckToMint > 0, "buck=0");
        if (_isFirstPositionInPool(tgtC)) {
            require(liquidity >= minSeedLiquidity, "seed too small");
        }

        buck.mintFromBasket(address(this), buckToMint);

        _callbackPool = tgtC.pool;
        IUniswapV3Pool(tgtC.pool).mint(
            address(this), tgtC.tickLower, tgtC.tickUpper, liquidity,
            abi.encode(tgtC.token)
        );
        _callbackPool = address(0);
    }

    /// @notice Reinvest treasury BUCK profit — same as _buckToLp, but
    ///         no NFT (silent NAV increase).
    function _reinvestBuck(uint256 buckAmount) internal {
        if (buckAmount == 0) return;
        _buckToLp(buckAmount);
    }

    /// @notice Exact-INPUT TOKEN→BUCK swap on `pc.pool`.  Returns the
    ///         (spent, received) deltas the swap actually produced.
    ///         Sign-convention-checked.  Caller decides how to use the
    ///         output (cover shortfall, book as profit, etc.).
    function _swapTokenForBuckExactIn(
        Constituent storage pc,
        uint256 tokenIn
    ) internal returns (uint256 spent, uint256 received) {
        require(tokenIn > 0, "tokenIn=0");
        _swapCallbackPool = pc.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(pc.pool).swap(
            address(this),
            !pc.buckIsToken0,                       // zeroForOne for TOKEN→BUCK
            int256(tokenIn),                         // positive ⇒ exact-input
            pc.buckIsToken0 ? MAX_SQRT_RATIO - 1 : MIN_SQRT_RATIO + 1,
            abi.encode(pc.token)
        );
        _swapCallbackPool = address(0);
        int256 buckDelta = pc.buckIsToken0 ? d0 : d1;
        int256 tokDelta  = pc.buckIsToken0 ? d1 : d0;
        require(buckDelta <= 0 && tokDelta >= 0, "swap delta sign");
        spent    = uint256(tokDelta);
        received = uint256(-buckDelta);
    }

    /// @notice Estimate TOKEN-in needed to extract `buckOut` BUCK from
    ///         `pc.pool`, padded by `bufferBp` for V3 fee + price-impact
    ///         slippage.  Returns 0 if the BUCK side is too thin.
    ///
    /// @dev    Uses sqrtPriceX96 directly (token1/token0 = sqrtP²/Q192)
    ///         to avoid the tick-quote vs sqrtP discrepancy.
    function _tokenInForBuckOut(
        Constituent storage pc,
        uint160 sqrtP,
        uint256 buckOut,
        uint256 bufferBp
    ) internal view returns (uint256 tokenIn) {
        // sqrtPriceX96 = sqrt(token1/token0) × 2^96
        // ⇒ token1/token0 = (sqrtPriceX96)² / 2^192
        uint256 priceX192 = uint256(sqrtP) * uint256(sqrtP);
        // For buckIsToken0=true: BUCK is token0, TOKEN is token1.
        //   token1/token0 = TOKEN/BUCK (in raw units) = priceX192/2^192.
        //   tokenIn_raw = buckOut_raw × priceX192 / 2^192.
        // For buckIsToken0=false: BUCK is token1, TOKEN is token0.
        //   token1/token0 = BUCK/TOKEN = priceX192/2^192.
        //   tokenIn_raw = buckOut_raw × 2^192 / priceX192.
        if (pc.buckIsToken0) {
            tokenIn = UniswapV3OracleLib.mulDiv(buckOut, priceX192, 1 << 192);
        } else {
            tokenIn = UniswapV3OracleLib.mulDiv(buckOut, 1 << 192, priceX192);
        }
        // Pad for fee + slippage so the exact-input swap delivers ≥ buckOut.
        tokenIn = tokenIn * (10000 + bufferBp) / 10000;
    }

    /// @notice Cover an aggregate BUCK shortfall by swapping TOKEN from
    ///         whichever pool(s) have the most TOKEN-balance remaining.
    ///         Uses exact-INPUT swaps with a slippage buffer (robust:
    ///         the swap always succeeds for any non-zero TOKEN held;
    ///         we just may need to iterate across pools).
    ///
    ///         Mutates `perPoolTok` in place to reflect post-swap state.
    /// @return newTotalBuckOut  totalBuckOut after the swap(s).
    function _coverShortfallAggregate(
        uint256[] memory perPoolTok,
        uint256 shortfall,
        uint256 totalBuckOut
    ) internal returns (uint256 newTotalBuckOut) {
        newTotalBuckOut = totalBuckOut;
        uint256 remaining = shortfall;
        uint256 N = constituents.length;

        // Greedy: each pass picks the pool with the most TOKEN balance,
        // swaps as much as needed (or as much as available), then
        // re-evaluates.  At worst O(N²) which is fine for typical N≤10.
        for (uint256 pass = 0; pass < N && remaining > 0; pass++) {
            uint256 bestIdx = type(uint256).max;
            uint256 bestTok = 0;
            for (uint256 i = 0; i < N; i++) {
                if (perPoolTok[i] > bestTok) {
                    bestIdx = i;
                    bestTok = perPoolTok[i];
                }
            }
            if (bestIdx == type(uint256).max) break;

            Constituent storage pc = constituents[bestIdx];
            (uint160 sqrtP,,,,,,) = IUniswapV3Pool(pc.pool).slot0();
            uint256 tokenIn = _tokenInForBuckOut(
                pc, sqrtP, remaining, SHORTFALL_BUFFER_BP);
            if (tokenIn == 0 || tokenIn > perPoolTok[bestIdx]) {
                tokenIn = perPoolTok[bestIdx];
            }
            (uint256 spent, uint256 received) =
                _swapTokenForBuckExactIn(pc, tokenIn);
            perPoolTok[bestIdx] -= spent;
            newTotalBuckOut += received;
            remaining = received >= remaining ? 0 : remaining - received;
        }
        // Don't revert here.  Caller checks the residual gap and
        // decides: tolerate tiny dust (MAX_ORPHAN_DUST_WEI) for things
        // like V3 burn rounding on drained pools, revert on larger gaps
        // that indicate genuine liquidity exhaustion.
    }

    // --- Settlement helpers ---------------------------------------------- //

    struct RedeemResult {
        uint256 toUserT;
        uint256 halfProfitT;
        uint256 burnedB;
        uint256 halfProfitB;
    }

    function _settleRedemption(
        Constituent storage c,
        Deposit memory d,
        uint256 tokenOut,
        uint256 buckOut,
        address to
    ) internal returns (RedeemResult memory r) {
        uint256 profitT = tokenOut > d.tokenPrincipal ? tokenOut - d.tokenPrincipal : 0;
        uint256 profitB = buckOut  > d.buckPrincipal  ? buckOut  - d.buckPrincipal  : 0;
        r.halfProfitT = profitT / 2;
        r.halfProfitB = profitB / 2;
        r.burnedB = buckOut < d.buckPrincipal ? buckOut : d.buckPrincipal;
        if (r.burnedB > 0) buck.burnFromBasket(r.burnedB);
        r.toUserT = d.tokenPrincipal <= tokenOut ? d.tokenPrincipal : tokenOut;
        uint256 totalT = r.toUserT + r.halfProfitT;
        if (totalT > 0) IERC20(d.token).transfer(to, totalT);
        if (r.halfProfitB > 0) IERC20(address(buck)).transfer(to, r.halfProfitB);
    }

    function _mintTreasury(
        Constituent storage c,
        address token,
        uint256 halfProfitT,
        uint256 halfProfitB
    ) internal {
        uint128 residualL = _liquidityForAmounts(c, halfProfitT, halfProfitB);
        if (residualL == 0) return;
        _callbackPool = c.pool;
        IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, residualL,
            abi.encode(token)
        );
        _callbackPool = address(0);
    }

    // --- V3 mint callback ------------------------------------------------- //

    function uniswapV3MintCallback(
        uint256 amount0Owed,
        uint256 amount1Owed,
        bytes calldata data
    ) external override {
        require(msg.sender == _callbackPool, "bad callback");
        address token = abi.decode(data, (address));
        Constituent storage c = constituents[indexOf[token] - 1];

        // BUCK is token0 iff its address compares less than the basket TOKEN.
        if (c.buckIsToken0) {
            if (amount0Owed > 0) IERC20(address(buck)).transfer(msg.sender, amount0Owed);
            if (amount1Owed > 0) IERC20(c.token).transfer(msg.sender, amount1Owed);
        } else {
            if (amount0Owed > 0) IERC20(c.token).transfer(msg.sender, amount0Owed);
            if (amount1Owed > 0) IERC20(address(buck)).transfer(msg.sender, amount1Owed);
        }
    }

    /// @notice Swap callback: pay positive deltas (tokens the pool needs to
    ///         receive from us) from the BuckBasket's own balance.
    /// @dev    `data` carries the constituent's basket TOKEN address only;
    ///         we look up the constituent and use its `buckIsToken0` to
    ///         decide which side is BUCK vs TOKEN.  Mirrors the mint
    ///         callback pattern — both orderings of (BUCK, TOKEN) work.
    function uniswapV3SwapCallback(
        int256 amount0Delta,
        int256 amount1Delta,
        bytes calldata data
    ) external override {
        require(msg.sender == _swapCallbackPool, "bad swap callback");
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

    function _isFirstPositionInPool(Constituent storage c) internal view returns (bool) {
        (uint128 currentL,,,,) = IUniswapV3Pool(c.pool).positions(
            keccak256(abi.encodePacked(address(this), c.tickLower, c.tickUpper))
        );
        return currentL == 0;
    }

    function _readPoolPrice(
        Constituent storage c,
        uint32 secondsAgo
    ) internal view returns (uint256 priceInBuck) {
        int24 tick;
        if (secondsAgo == 0) {
            (, tick,,,,,) = IUniswapV3Pool(c.pool).slot0();
        } else {
            // TWAP read: fall back to slot0 spot if the pool has no
            // observations old enough to satisfy `secondsAgo` (cold pool
            // immediately after addBasketToken).  basketValueInBuck()
            // reaches here from controller.compute() and from view callers,
            // so a revert would freeze the PID rather than degrade
            // gracefully.
            try this.consultTickExternal(c.pool, secondsAgo) returns (int24 t) {
                tick = t;
            } catch {
                (, tick,,,,,) = IUniswapV3Pool(c.pool).slot0();
            }
        }
        uint256 oneToken = 10 ** c.decimals;
        priceInBuck = UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(oneToken), c.token, address(buck)
        );
    }

    /// @dev External wrapper around `consult()` so internal try/catch can
    ///      handle the V3 pool's "OLD" revert from observations older than
    ///      the available history.
    function consultTickExternal(address pool, uint32 secondsAgo) external view returns (int24) {
        return UniswapV3OracleLib.consult(pool, secondsAgo);
    }

    function _enforceSlippageGuard(
        Constituent storage c,
        uint256 spotPrice,
        uint256 maxDeviationBp
    ) internal view {
        if (maxDeviationBp == 0) return;
        // Try TWAP; on empty pool / insufficient observations the V3
        // consult reverts.  Catch and skip the guard in that case.
        try this.peekTwap(c.pool, twapWindow) returns (uint256 twapPrice) {
            if (twapPrice == 0) return;
            uint256 spot = spotPrice;
            uint256 dev  = spot > twapPrice ? spot - twapPrice : twapPrice - spot;
            require(dev * 10000 <= twapPrice * maxDeviationBp, "slippage");
        } catch {
            return;
        }
    }

    /// @dev External wrapper so the slippage guard can catch consult reverts.
    function peekTwap(address pool, uint32 secondsAgo) external view returns (uint256) {
        int24 tick = UniswapV3OracleLib.consult(pool, secondsAgo);
        Constituent storage c = constituents[indexOf[_poolToToken(pool)] - 1];
        uint256 oneToken = 10 ** c.decimals;
        return UniswapV3OracleLib.getQuoteAtTick(
            tick, uint128(oneToken), c.token, address(buck)
        );
    }

    function _poolToToken(address pool) internal view returns (address) {
        for (uint256 i = 0; i < constituents.length; i++) {
            if (constituents[i].pool == pool) return constituents[i].token;
        }
        revert("unknown pool");
    }

    function _liquidityForAmounts(
        Constituent storage c,
        uint256 tokenAmount,
        uint256 buckAmount
    ) internal view returns (uint128) {
        (uint160 sqrtP,,,,,,) = IUniswapV3Pool(c.pool).slot0();
        uint160 sqrtLow  = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickLower);
        uint160 sqrtHigh = UniswapV3OracleLib.getSqrtRatioAtTick(c.tickUpper);
        (uint256 amount0, uint256 amount1) = c.buckIsToken0
            ? (buckAmount, tokenAmount)
            : (tokenAmount, buckAmount);
        return UniswapV3OracleLib.getLiquidityForAmounts(
            sqrtP, sqrtLow, sqrtHigh, amount0, amount1
        );
    }

    function _decreaseAndCollect(
        Constituent storage c,
        uint128 liquidity
    ) internal returns (uint256 tokenOut, uint256 buckOut) {
        IUniswapV3Pool(c.pool).burn(c.tickLower, c.tickUpper, liquidity);
        (uint128 a0, uint128 a1) = IUniswapV3Pool(c.pool).collect(
            address(this),
            c.tickLower,
            c.tickUpper,
            type(uint128).max,
            type(uint128).max
        );
        if (c.buckIsToken0) {
            buckOut  = a0;
            tokenOut = a1;
        } else {
            tokenOut = a0;
            buckOut  = a1;
        }
    }

    function _sqrtPriceFromBuckRate(
        bool   buckIsToken0,
        uint256 priceInBuck,        // 18-dec; BUCK per 1 whole TOKEN
        uint8   tokenDecimals
    ) internal pure returns (uint160) {
        // We want to set sqrtPriceX96 so that the pool quotes 1 whole TOKEN
        // for `priceInBuck` whole BUCK.  In raw units:
        //     amount_BUCK = priceInBuck                            (18-dec)
        //     amount_TOKEN = 10 ** tokenDecimals                   (raw)
        // sqrtPriceX96 = sqrt(amount1 / amount0) * 2^96
        // with amount0/amount1 ordered by address.
        uint256 buckRaw  = priceInBuck;                // 18-dec
        uint256 tokenRaw = 10 ** tokenDecimals;        // raw
        uint256 amount0  = buckIsToken0 ? buckRaw  : tokenRaw;
        uint256 amount1  = buckIsToken0 ? tokenRaw : buckRaw;
        // sqrt(amount1 / amount0) * Q96 = sqrt(amount1 * Q192 / amount0)
        uint256 ratioX192 = UniswapV3OracleLib.mulDiv(amount1, 1 << 192, amount0);
        uint256 sqrtRoot  = Math.sqrt(ratioX192);
        require(sqrtRoot <= type(uint160).max, "sqrtP:overflow");
        return uint160(sqrtRoot);
    }
}
