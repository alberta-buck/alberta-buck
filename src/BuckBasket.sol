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
/// BuckBasket manages two distinct categories of BUCK:
///
/// 1.  **BuckBasket-minted BUCKs** — minted via `Buck.mintFromBasket()` when
///     TOKEN liquidity is deposited into BuckBasket's AMM pools.  Tracked in
///     `totalOutstandingBuck` and always burned on redemption.
///
/// 2.  **Externally-supplied BUCKs** — created by normal `Buck.mint()` backed
///     by insured assets (BuckCredit).  These flow into the pools through
///     external arbitrage and AMM trading, providing the BUCK side of
///     liquidity and generating fee income for BuckBasket LP positions.
///
/// # Redemption: pool selection & BUCK shortfall
///
/// `redeem()` selects the pool with the most BUCKs in its liquidity
/// position — by definition the most appreciated / overweight pool ("sell
/// high").  The depositor's principal BUCKs are burned with priority:
///
///  1. Withdrawn principal BUCKs from the overweight pool's LP position.
///  2. If short: 50% from treasury profit BUCKs, 50% by converting the
///     customer's profit TOKENs to BUCKs (shared risk).
///  3. If still short: convert customer's principal TOKENs to BUCKs.
///  4. Revert if the pool is too depleted to cover the principal.
///
/// Treasury redemptions do NOT re-invest profits — TOKENs and remaining
/// BUCKs are paid out to governance.
///
/// # Invariant
///
/// Every BuckBasket-minted BUCK recorded in `totalOutstandingBuck` is
/// eventually burned by `redeem()`.  After all deposits exit,
/// `totalOutstandingBuck == 0` and any remaining LP value is entirely
/// treasury-owned: retained TOKEN profit and its corresponding
/// BuckBasket-minted BUCKs — the compounded profits from all direct-mint
/// activity.
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
    event Redeemed(
        address indexed depositor,
        uint256 indexed receiptId,
        address indexed redeemedToken, // overweight token returned to user
        uint256 tokenToUser,           // native-dec token returned (principal + half profit)
        uint256 burnedBuck,            // BUCK principal burned
        uint256 retainedBuck,          // profit BUCK kept by treasury
        uint256 remainingBp            // 0 if fully redeemed; else new NFT share
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
    ///         its targetWeightBp.  Empty pools (value=0) are always first.
    function _mostUnderweightPool() internal view returns (uint256 idx) {
        int256 best = type(int256).max;
        for (uint256 i = 0; i < constituents.length; i++) {
            uint256 v = _poolLpValue(constituents[i]);
            uint256 w = constituents[i].targetWeightBp;
            // Ratio: value per basis-point (lower = more underweight).
            // Empty pools have v=0, so ratio=0 (most underweight).
            int256 ratio = w > 0 ? int256(v / w) : type(int256).max;
            if (ratio < best) {
                best = ratio;
                idx = i;
            }
        }
    }

    /// @notice Index of the pool whose LP is most overweight relative to
    ///         its targetWeightBp.  Skips empty pools (they can't be sold).
    function _mostOverweightPool() internal view returns (uint256 idx) {
        int256 best = -1;
        for (uint256 i = 0; i < constituents.length; i++) {
            uint256 v = _poolLpValue(constituents[i]);
            uint256 w = constituents[i].targetWeightBp;
            if (v == 0 || w == 0) continue;
            int256 ratio = int256(v / w);
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

    // --- Pool weight errors (P-controller) -------------------------------- //

    /// @notice Per-pool value-weight error: actualWeight - targetWeight.
    ///         Positive = overweight (sell), negative = underweight (buy).
    function _poolWeightErrors() internal view
        returns (int256[] memory errors, uint256 totalPositive)
    {
        uint256 N = constituents.length;
        errors = new int256[](N);

        // Compute actual and target values.
        uint256[] memory actualVal = new uint256[](N);
        uint256[] memory targetVal = new uint256[](N);
        uint256 avSum = 0;
        uint256 tvSum = 0;
        for (uint256 i = 0; i < N; i++) {
            uint256 v = _poolLpValue(constituents[i]);
            actualVal[i] = v;
            avSum += v;
            uint256 p = _readPoolPrice(constituents[i], 0);
            targetVal[i] = constituents[i].targetWeightBp * p;
            tvSum += targetVal[i];
        }

        if (avSum == 0 || tvSum == 0) return (errors, 0);

        // Normalise to basis points and compute error.
        for (uint256 i = 0; i < N; i++) {
            int256 aw = int256(actualVal[i] * 10000 / avSum);
            int256 tw = int256(targetVal[i] * 10000 / tvSum);
            errors[i] = aw - tw;
            if (errors[i] > 0) {
                totalPositive += uint256(errors[i]);
            }
        }
    }

    // --- Redemption (P-controller: sell overweight pools proportionally) -- //

    /// @notice Redeem all or part of a basket receipt.  Withdrawals are
    ///         spread across all *overweight* pools proportional to their
    ///         value-weight error (P-controller allocation — "sell high").
    ///         Principal BUCK is burned, ALL TOKENs are returned to the
    ///         client, and profit BUCK is reinvested into underweight pools.
    ///
    /// @param  redeemBp  0 = redeem entire deposit; otherwise basis points.
    /// @param  maxDeviationBp 0 = skip TWAP guard.
    function redeem(uint256 receiptId, uint256 redeemBp,
                    uint256 maxDeviationBp) external {
        require(receipt.ownerOf(receiptId) == msg.sender, "not owner");
        Deposit memory d = deposits[receiptId];
        require(d.buckPrincipal > 0, "empty deposit");

        uint256 redeemShare = redeemBp > 0 ? redeemBp : 10000;
        require(redeemShare <= 10000, "redeemBp > 10000");

        uint256 redeemBuck = redeemShare == 10000
            ? d.buckPrincipal
            : d.buckPrincipal * redeemShare / 10000;
        require(redeemBuck > 0, "redeem zero");

        // Slippage guard (use deposited token's pool).
        Constituent storage depC = constituents[indexOf[d.token] - 1];
        _enforceSlippageGuard(depC, _readPoolPrice(depC, 0), maxDeviationBp);

        // P-controller: allocate redemption across overweight pools.
        (int256[] memory errors, uint256 totalPos) = _poolWeightErrors();
        require(totalPos > 0, "no overweight pools");

        uint256 totalValue = _totalBasketLpValue();
        require(totalValue > 0, "no LP value");
        uint256 redeemValue = UniswapV3OracleLib.mulDiv(
            redeemBuck, totalValue, totalOutstandingBuck);

        uint256 totalTokOut = 0;
        uint256 totalBuckOut = 0;
        address  firstToken = address(0);

        for (uint256 i = 0; i < constituents.length; i++) {
            if (errors[i] <= 0) continue;  // skip non-overweight pools

            uint256 poolFrac = uint256(errors[i]) * 10000 / totalPos;
            uint256 poolRedeem = redeemValue * poolFrac / 10000;
            if (poolRedeem == 0) continue;

            Constituent storage ovC = constituents[i];
            uint256 ovPoolVal = _poolLpValue(ovC);
            if (ovPoolVal == 0) continue;
            uint256 poolRedeemFrac = poolRedeem * 1e18 / ovPoolVal;
            if (poolRedeemFrac > 1e18) poolRedeemFrac = 1e18;

            (uint128 totalL,,,,) = IUniswapV3Pool(ovC.pool).positions(
                keccak256(abi.encodePacked(address(this),
                              ovC.tickLower, ovC.tickUpper))
            );
            if (totalL == 0) continue;
            uint128 burnL = uint128(
                uint256(totalL) * poolRedeemFrac / 1e18);
            if (burnL == 0) continue;

            (uint256 tok, uint256 b) = _decreaseAndCollect(ovC, burnL);
            totalTokOut += tok;
            totalBuckOut += b;
            if (firstToken == address(0)) firstToken = ovC.token;
        }
        require(totalTokOut > 0 || totalBuckOut > 0, "no LP withdrawn");

        // Burn principal BUCK.
        uint256 burnedB = totalBuckOut < redeemBuck
            ? totalBuckOut : redeemBuck;
        if (burnedB > 0) buck.burnFromBasket(burnedB);

        // Client receives ALL tokens from all withdrawn pools.
        // (In the multi-pool case the client may receive multiple token
        //  types; we send all to the client.  A future improvement can
        //  swap to a single requested token.)
        if (totalTokOut > 0) {
            // For simplicity send all tokens from the first overweight pool.
            // Multi-token aggregation needs a swap step — deferred.
            IERC20(firstToken).transfer(msg.sender, totalTokOut);
        }

        // Treasury retains BUCK profit and reinvests.
        uint256 profitBuck = totalBuckOut > burnedB
            ? totalBuckOut - burnedB : 0;
        if (profitBuck > 0) {
            _reinvestBuck(profitBuck);
        }

        // Update the deposit.
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

        controller.compute();

        emit Redeemed(
            msg.sender, receiptId,
            firstToken != address(0) ? firstToken : depC.token,
            totalTokOut, burnedB, profitBuck,
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

        // Swap BUCK -> target token on the target pool.
        _swapCallbackPool = tgtC.pool;
        (int256 d0, int256 d1) = IUniswapV3Pool(tgtC.pool).swap(
            address(this), tgtC.buckIsToken0,
            int256(buckAmount),
            tgtC.buckIsToken0 ? MAX_SQRT_RATIO - 1 : MIN_SQRT_RATIO + 1,
            abi.encode(address(buck), tgtC.token)
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
    function uniswapV3SwapCallback(
        int256 amount0Delta,
        int256 amount1Delta,
        bytes calldata data
    ) external override {
        require(msg.sender == _swapCallbackPool, "bad swap callback");
        (address t0, address t1) = abi.decode(data, (address, address));
        if (amount0Delta > 0) IERC20(t0).transfer(msg.sender, uint256(amount0Delta));
        if (amount1Delta > 0) IERC20(t1).transfer(msg.sender, uint256(amount1Delta));
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
