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
/// * #6  `_buckToLp` swap uses `MIN/MAX_SQRT_RATIO ± 1` (no real
///       `sqrtPriceLimit`); sandwichable.
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
/// * #12 No Forge tests for the new redemption paths; behavior is
///       only validated by the Python sim.
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
    /// @notice Emitted once per overweight pool a redemption touches.
    ///         For a given receiptId across these events:
    ///           Σ buckBurned   == redeemBuck (mint/burn invariant)
    ///           Σ buckProfit   == retainedBuck of the matching Redeemed
    ///         `tokenSwapped` is non-zero only when the pool's BUCK side
    ///         was insufficient to cover `buckBurned` and the basket
    ///         had to swap part of the withdrawn TOKEN back to BUCK on
    ///         the same pool.  (Conceptually that swap eats the
    ///         depositor's profit-TOKEN first, then principal-TOKEN;
    ///         the labeling is narrative — the math is the same.)
    event RedeemedFromPool(
        uint256 indexed receiptId,
        address indexed pool,
        address indexed token,
        uint256 tokenToUser,           // native-dec; transferred to depositor
        uint256 buckBurned,            // 18-dec; pool's share of redeemBuck
        uint256 buckProfit,            // 18-dec; surplus BUCK → treasury
        uint256 tokenSwapped,          // native-dec; TOKEN→BUCK to cover shortfall
        uint128 burnedLiquidity        // L removed from basket's position
    );

    // --- Constants -------------------------------------------------------- //

    int24 internal constant MIN_TICK = -887272;
    int24 internal constant MAX_TICK =  887272;
    uint160 internal constant MIN_SQRT_RATIO = 4295128739;
    uint160 internal constant MAX_SQRT_RATIO =
        1461446703485210103287273052203988822378723970342;

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
        // --- step 1: ownership + redemption sizing ------------------- //
        // Receipt owner only.  `redeemBp == 0` means full redemption;
        // otherwise basis points of `d.buckPrincipal` to redeem.
        require(receipt.ownerOf(receiptId) == msg.sender, "not owner");
        Deposit memory d = deposits[receiptId];
        require(d.buckPrincipal > 0, "empty deposit");

        uint256 redeemShare = redeemBp > 0 ? redeemBp : 10000;
        require(redeemShare <= 10000, "redeemBp > 10000");

        uint256 redeemBuck = redeemShare == 10000
            ? d.buckPrincipal
            : d.buckPrincipal * redeemShare / 10000;
        require(redeemBuck > 0, "redeem zero");

        // --- step 2: slippage guard ---------------------------------- //
        // BUG #5: this guards the depositor's *original* pool, not the
        // pool(s) we'll actually withdraw from in step 4.  Under the
        // multi-pool model the guard should be applied per-withdrawal
        // pool inside the loop.
        Constituent storage depC = constituents[indexOf[d.token] - 1];
        _enforceSlippageGuard(depC, _readPoolPrice(depC, 0), maxDeviationBp);

        // --- step 3: compute value claim + per-pool allocation -------- //
        // The depositor's BUCK principal claims a proportional share of
        // total LP NAV — including AMM fees, external-arb BUCK influx,
        // and any reinvested treasury BUCK.  NAV/outstanding > 1 ⇒ the
        // depositor's value claim exceeds their principal (profit).
        //
        // `_allocateRedemption` returns per-pool BUCK-value extractions
        // that drive every pool toward its target share of the post-
        // redemption NAV.  In equilibrium this is proportional-by-value
        // for everyone (no revert); when pools are skewed it pulls
        // hardest from the overweight ones.  Small redemptions (below
        // SMALL_REDEEM_BP of the most overweight pool) take a single-
        // pool fast path for gas.
        uint256 navTotal = _totalBasketLpValue();
        require(navTotal > 0, "no LP value");
        uint256 redeemValue = UniswapV3OracleLib.mulDiv(
            redeemBuck, navTotal, totalOutstandingBuck);
        uint256[] memory alloc = _allocateRedemption(redeemValue, navTotal);

        // --- step 4: per-pool withdrawal + shortfall cover ------------ //
        // For each pool with non-zero allocation: withdraw an L-slice of
        // value `alloc[i]`, transfer the TOKEN side to the depositor,
        // and route the BUCK side through the per-pool burn budget
        // (`poolPrincipalBuck = redeemBuck × alloc[i] / redeemValue`).
        //
        // If the pool's withdrawn BUCK side is short of its burn share,
        // the basket swaps part of the withdrawn TOKEN back to BUCK on
        // the same pool (`_coverBuckShortfall`) — preserving the
        // mint/burn invariant Σ poolPrincipalBuck == redeemBuck.
        // The depositor receives `tok - tokSpent`; treasury collects
        // any surplus `b - poolPrincipalBuck` for reinvestment.
        uint256 totalProfit = 0;
        uint256 totalIntendedBurn = 0;
        bool    anyWithdrawn = false;

        for (uint256 i = 0; i < constituents.length; i++) {
            if (alloc[i] == 0) continue;

            Constituent storage pc = constituents[i];
            uint256 poolVal = _poolLpValue(pc);
            if (poolVal == 0) continue;
            uint256 poolRedeemFrac = alloc[i] * 1e18 / poolVal;
            if (poolRedeemFrac > 1e18) poolRedeemFrac = 1e18;

            (uint128 totalL,,,,) = IUniswapV3Pool(pc.pool).positions(
                keccak256(abi.encodePacked(address(this),
                              pc.tickLower, pc.tickUpper))
            );
            if (totalL == 0) continue;
            uint128 burnL = uint128(
                uint256(totalL) * poolRedeemFrac / 1e18);
            if (burnL == 0) continue;

            (uint256 tok, uint256 b) = _decreaseAndCollect(pc, burnL);
            anyWithdrawn = true;

            // Per-pool burn budget = redeemBuck weighted by alloc share.
            uint256 poolPrincipalBuck = UniswapV3OracleLib.mulDiv(
                redeemBuck, alloc[i], redeemValue);
            uint256 tokSpent = 0;
            uint256 poolProfit = 0;

            if (b >= poolPrincipalBuck) {
                // Surplus: keep the excess BUCK as treasury profit.
                poolProfit = b - poolPrincipalBuck;
            } else {
                // Shortfall: swap some withdrawn TOKEN back to BUCK on
                // the same pool to bring the bucket up to the per-pool
                // burn budget.  Reverts if the pool can't cover.
                tokSpent = _coverBuckShortfall(
                    pc, tok, poolPrincipalBuck - b);
            }

            totalIntendedBurn += poolPrincipalBuck;
            totalProfit += poolProfit;

            uint256 tokToUser = tok - tokSpent;
            if (tokToUser > 0) {
                IERC20(pc.token).transfer(msg.sender, tokToUser);
            }
            emit RedeemedFromPool(
                receiptId, pc.pool, pc.token,
                tokToUser, poolPrincipalBuck, poolProfit, tokSpent, burnL);
        }
        require(anyWithdrawn, "no LP withdrawn");
        // Integer-division dust: `poolPrincipalBuck = redeemBuck *
        // poolFrac / 10000` truncates, so Σ poolPrincipalBuck can be a
        // few wei less than `redeemBuck`.  Absorb the dust by shrinking
        // treasury profit (the basket holds the same total BUCK either
        // way — we're just relabeling it).  Reverts if the rounding
        // exceeds available profit (rare; user retries with smaller
        // redeemBp).
        if (totalIntendedBurn < redeemBuck) {
            uint256 dust = redeemBuck - totalIntendedBurn;
            require(totalProfit >= dust, "burn rounding uncovered");
            totalProfit -= dust;
            totalIntendedBurn += dust;
        }
        require(totalIntendedBurn == redeemBuck, "burn mismatch");

        // --- step 5: burn principal BUCK ----------------------------- //
        // Mint/burn invariant: burn exactly `redeemBuck`, no more, no
        // less.  Step 4's shortfall handling guarantees the basket holds
        // at least this much BUCK at this point.
        buck.burnFromBasket(redeemBuck);

        // --- step 6: treasury reinvestment --------------------------- //
        // Surplus BUCK from overweight pools is the basket's realized
        // profit.  Swap it into the most underweight TOKEN, re-mint
        // BUCK against that TOKEN, and LP back in as a treasury-owned
        // position — the "buy low" leg in this same tx.
        // BUG #8: `_reinvestBuck` LPs into the single most-underweight
        // pool; should mirror step 4's proportional allocation across
        // all underweight pools.
        // BUG #11: `_reinvestBuck` emits no event; downstream observers
        // (PID, accounting) can't distinguish treasury reinvestment
        // from external arb trades.
        if (totalProfit > 0) {
            _reinvestBuck(totalProfit);
        }

        // --- step 7: update deposit state ---------------------------- //
        // Full redemption ⇒ delete deposit + burn receipt NFT.
        // Partial ⇒ scale down both BUCK and TOKEN principals.
        uint256 scaledTokenPrincipal = redeemShare == 10000
            ? d.tokenPrincipal
            : d.tokenPrincipal * redeemShare / 10000;
        if (redeemShare == 10000) {
            delete deposits[receiptId];
            receipt.burn(receiptId);
        } else {
            deposits[receiptId].buckPrincipal -= redeemBuck;
            deposits[receiptId].tokenPrincipal -= scaledTokenPrincipal;
        }
        totalOutstandingBuck -= redeemBuck;

        // --- step 8: keep PID warm + emit aggregate ------------------ //
        controller.compute();

        emit Redeemed(
            msg.sender, receiptId,
            redeemBuck, totalProfit,
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
        _swapCallbackPool = tgtC.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(tgtC.pool).swap(
            address(this), tgtC.buckIsToken0,
            int256(buckAmount),
            tgtC.buckIsToken0 ? MAX_SQRT_RATIO - 1 : MIN_SQRT_RATIO + 1,
            abi.encode(tgtC.token)
        );
        _swapCallbackPool = address(0);
        tgtTok = tgtC.buckIsToken0
            ? uint256(d1 > 0 ? d1 : int256(0))
            : uint256(d0 > 0 ? d0 : int256(0));
        require(tgtTok > 0, "swap BUCK->token failed");

        // Mint new BUCK against the received token.
        uint256 spotPrice = _readPoolPrice(tgtC, 0);
        buckToMint = UniswapV3OracleLib.mulDiv(
            tgtTok, spotPrice, 10 ** tgtC.decimals
        );
        require(buckToMint > 0, "buck=0");
        buck.mintFromBasket(address(this), buckToMint);

        // LP into the underweight pool.
        uint128 liquidity = _liquidityForAmounts(
            tgtC, tgtTok, buckToMint);
        require(liquidity > 0, "L=0");
        if (_isFirstPositionInPool(tgtC)) {
            require(liquidity >= minSeedLiquidity, "seed too small");
        }

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

    /// @notice Exact-output TOKEN→BUCK swap on `ovC.pool` to cover a
    ///         BUCK shortfall during redemption from an appreciated
    ///         (BUCK-light) pool.  Maintains the mint/burn invariant:
    ///         the basket always burns exactly `redeemBuck` of principal,
    ///         even when the pool's BUCK side alone can't cover the
    ///         pool's share of that burn.
    ///
    ///         The swap is funded from `tokAvailable` — the TOKEN the
    ///         basket just received from `_decreaseAndCollect` for this
    ///         pool.  Whatever TOKEN the swap consumes is taken from
    ///         that bucket; the rest flows to the depositor.
    ///
    ///         Reverts if the pool's remaining liquidity can't deliver
    ///         the full `shortfall` (would otherwise under-burn and
    ///         break the invariant).  User retries with a smaller
    ///         `redeemBp`.
    /// @return tokSpent  Native-dec TOKEN consumed by the swap.
    function _coverBuckShortfall(
        Constituent storage ovC,
        uint256 tokAvailable,
        uint256 shortfall
    ) internal returns (uint256 tokSpent) {
        require(shortfall > 0, "shortfall=0");
        require(tokAvailable > 0, "tokAvail=0");

        _swapCallbackPool = ovC.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(ovC.pool).swap(
            address(this),
            !ovC.buckIsToken0,                  // zeroForOne for TOKEN→BUCK
            -int256(shortfall),                  // negative ⇒ exact-output
            // Permissive limit today (gap #6); fix will TWAP-bound this.
            ovC.buckIsToken0 ? MIN_SQRT_RATIO + 1 : MAX_SQRT_RATIO - 1,
            abi.encode(ovC.token)
        );
        _swapCallbackPool = address(0);

        // Sign convention: positive delta = we paid; negative = we received.
        int256 buckDelta = ovC.buckIsToken0 ? d0 : d1;
        int256 tokDelta  = ovC.buckIsToken0 ? d1 : d0;
        require(buckDelta < 0 && tokDelta > 0, "unexpected swap deltas");
        uint256 actualBuck = uint256(-buckDelta);
        tokSpent = uint256(tokDelta);
        require(actualBuck >= shortfall, "swap shortfall incomplete");
        require(tokSpent <= tokAvailable, "swap exceeded tokAvail");
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
