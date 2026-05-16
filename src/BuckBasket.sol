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
///         Depositors hand the basket a real-world-asset token (PAXG,
///         cbBTC, ...) and receive a BuckBasketReceipt NFT representing
///         their claim on the corresponding pool slice.  BuckBasket
///         mints the BUCK side of their liquidity contribution against
///         the pool's current spot price (manipulation-guarded by a
///         spot-vs-TWAP deviation check), adds the (TOKEN, BUCK) pair to
///         its full-range position, and records the depositor's L share.
///
///         On redeem, BuckBasket withdraws the depositor's L share from
///         the pool, burns the principal BUCK, returns the principal
///         TOKEN plus half the accrued TOKEN+BUCK profit to the
///         depositor, and immediately redeposits the retained half into
///         its own treasury position in the same pool.
abstract contract BuckBasketAbstractGuards is IUniswapV3MintCallback {
    function uniswapV3MintCallback(uint256, uint256, bytes calldata) external virtual override;
}

contract BuckBasket is IUniswapV3MintCallback {

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
    }

    Constituent[] public constituents;
    mapping(address => uint256) public indexOf;   // token -> 1+index (0 = not present)

    struct Deposit {
        address token;
        uint256 principalToken;      // native decimals
        uint256 principalBuck;       // 18-dec
        uint128 liquidityShare;      // V3 L units; the depositor's slice of pool L
        uint64  depositTime;
    }
    mapping(uint256 => Deposit) public deposits;

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
        address indexed token,
        uint256 tokenAmount,
        uint256 buckAmount,
        uint128 liquidity
    );
    event Redeemed(
        address indexed depositor,
        uint256 indexed receiptId,
        address indexed token,
        uint256 principalToken,
        uint256 halfProfitToken,
        uint256 burnedBuck,
        uint256 halfProfitBuck,
        uint128 liquidityShare
    );

    // --- Constants -------------------------------------------------------- //

    int24 internal constant MIN_TICK = -887272;
    int24 internal constant MAX_TICK =  887272;

    // --- Mint callback re-entry guard ------------------------------------- //
    address internal _callbackPool;

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
    /// @dev    Dilutes every existing constituent's basketAmount by
    ///         (1 - weightBp/10000), then sets the new constituent's
    ///         basketAmount = (weightBp/10000) / initialPriceInBuck.
    ///         After the call the basket value at *current* pool prices
    ///         is exactly 1.0 BUCK (modulo TWAP rounding).  If existing
    ///         pools have drifted from their initial prices the diluted
    ///         amounts will not equal their original declared-weight
    ///         proportions — the basket weights float with the market.
    function addBasketToken(
        address token,
        uint8   decimals,
        uint256 initialPriceInBuck,
        uint256 weightBp,
        uint24  feeTier
    ) external returns (address pool) {
        require(msg.sender == governance, "Not governance");
        require(token != address(0) && token != address(buck), "bad token");
        require(weightBp > 0 && weightBp <= 10000, "bad weight");
        require(indexOf[token] == 0, "already present");
        require(initialPriceInBuck > 0, "bad price");

        // Rescale existing constituents so they collectively contribute
        // (10000 - weightBp) / 10000 of the new basket's 1.0 BUCK value
        // at *current* pool prices.  The new constituent then fills the
        // remaining weightBp/10000 share.
        //
        //   scale = ((10000 - weightBp) / 10000) / existingTotal
        //
        // Where existingTotal = sum(basketAmount_i * currentPrice_i).  When
        // the basket is empty, existingTotal == 0 and the loop is skipped;
        // the new constituent's weight may be any value up to 100%.
        if (constituents.length > 0) {
            uint256 existingTotal = uint256(_currentBasketValueAtCurrentPrices());
            require(existingTotal > 0, "existing basket = 0");
            // scale = (1 - weightBp/10000) * UNIT / existingTotal  (18-dec)
            uint256 numerator = (10000 - weightBp) * 1e18 / 10000;
            uint256 scaleX18  = (numerator * 1e18) / existingTotal;
            for (uint256 i = 0; i < constituents.length; i++) {
                constituents[i].basketAmount =
                    UniswapV3OracleLib.mulDiv(constituents[i].basketAmount, scaleX18, 1e18);
            }
        }

        // New constituent's basketAmount = (weight / price) in 18-dec.
        uint256 weightUnit = (weightBp * 1e18) / 10000;
        uint256 basketAmount = (weightUnit * 1e18) / initialPriceInBuck;

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
            buckIsToken0: address(buck) < token
        }));
        indexOf[token] = constituents.length;

        // Re-prime the controller so the dilution discontinuity doesn't
        // manifest as a single-cycle P/I spike.
        controller.reprime();

        emit BasketTokenAdded(token, weightBp, initialPriceInBuck, pool);
    }

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

    // --- Direct mint ----------------------------------------------------- //

    /// @notice Deposit `tokenAmount` of `token`, receive a claim NFT and
    ///         a Buck-custodied slice of the TOKEN/BUCK pool liquidity.
    /// @param  maxDeviationBp If non-zero AND a TWAP is available, the
    ///         deposit reverts when |spot - TWAP| > maxDeviationBp / 10000
    ///         of TWAP.  Pass 0 to skip the guard (e.g. cold-pool seed).
    function depositToken(
        address token,
        uint256 tokenAmount,
        uint256 maxDeviationBp
    ) external returns (uint256 receiptId) {
        uint256 idx = indexOf[token];
        require(idx > 0, "not in basket");
        require(tokenAmount > 0, "amount=0");
        Constituent storage c = constituents[idx - 1];

        // Spot price read; slippage guard against TWAP if requested.
        uint256 spotPrice = _readPoolPrice(c, 0);
        _enforceSlippageGuard(c, spotPrice, maxDeviationBp);

        // BUCK to mint = tokenAmount * spotPrice / 10**decimals (normalising
        // tokenAmount from native decimals to 18-dec, multiplying by
        // 18-dec spotPrice, then collapsing back to 18-dec BUCK).
        uint256 buckAmount = UniswapV3OracleLib.mulDiv(
            tokenAmount, spotPrice, 10 ** c.decimals
        );
        require(buckAmount > 0, "buck=0");

        // Pull TOKEN from depositor; mint corresponding BUCK to ourselves.
        IERC20(token).transferFrom(msg.sender, address(this), tokenAmount);
        buck.mintFromBasket(address(this), buckAmount);

        // Compute target liquidity from (token, buck) at current sqrtPrice.
        uint128 liquidity = _liquidityForAmounts(c, tokenAmount, buckAmount);
        require(liquidity > 0, "L=0");
        require(constituents.length > 0, ""); // gas/state guard; unreachable
        if (_isFirstPositionInPool(c)) {
            require(liquidity >= minSeedLiquidity, "seed too small");
        }

        // Mint the liquidity into our full-range position.
        _callbackPool = c.pool;
        IUniswapV3Pool(c.pool).mint(
            address(this), c.tickLower, c.tickUpper, liquidity,
            abi.encode(token)
        );
        _callbackPool = address(0);

        // Issue receipt with deposit metadata.
        receiptId = receipt.mint(msg.sender);
        deposits[receiptId] = Deposit({
            token: token,
            principalToken: tokenAmount,
            principalBuck: buckAmount,
            liquidityShare: liquidity,
            depositTime: uint64(block.timestamp)
        });

        // Keep PID warm.  Return value ignored; direct-mint isn't gated.
        controller.compute();

        emit Deposited(msg.sender, receiptId, token, tokenAmount, buckAmount, liquidity);
    }

    struct RedeemResult {
        uint256 toUserT;
        uint256 halfProfitT;
        uint256 burnedB;
        uint256 halfProfitB;
    }

    /// @notice Redeem a deposit receipt.  Caller must own the receipt NFT.
    function redeem(uint256 receiptId, uint256 maxDeviationBp) external {
        require(receipt.ownerOf(receiptId) == msg.sender, "not owner");
        Deposit memory d = deposits[receiptId];
        require(d.liquidityShare > 0, "empty deposit");
        Constituent storage c = constituents[indexOf[d.token] - 1];

        // Slippage guard against current spot vs TWAP.
        _enforceSlippageGuard(c, _readPoolPrice(c, 0), maxDeviationBp);

        // Withdraw the full liquidity share.
        (uint256 tokenOut, uint256 buckOut) = _decreaseAndCollect(c, d.liquidityShare);

        // Compute split + execute payouts + burn.
        RedeemResult memory r = _settleRedemption(c, d, tokenOut, buckOut, msg.sender);

        // Redeposit retained residuals into treasury position.
        if (r.halfProfitT > 0 && r.halfProfitB > 0) {
            _mintTreasury(c, d.token, r.halfProfitT, r.halfProfitB);
        }

        delete deposits[receiptId];
        receipt.burn(receiptId);
        controller.compute();

        emit Redeemed(
            msg.sender, receiptId, d.token,
            r.toUserT, r.halfProfitT, r.burnedB, r.halfProfitB,
            d.liquidityShare
        );
    }

    function _settleRedemption(
        Constituent storage c,
        Deposit memory d,
        uint256 tokenOut,
        uint256 buckOut,
        address to
    ) internal returns (RedeemResult memory r) {
        // Profit on each side (clamp negatives to 0 -- depositor eats losses).
        uint256 profitT = tokenOut > d.principalToken ? tokenOut - d.principalToken : 0;
        uint256 profitB = buckOut  > d.principalBuck  ? buckOut  - d.principalBuck  : 0;
        r.halfProfitT = profitT / 2;
        r.halfProfitB = profitB / 2;

        // Burn principal BUCK (or all of buckOut if pool was BUCK-depleted).
        r.burnedB = buckOut < d.principalBuck ? buckOut : d.principalBuck;
        if (r.burnedB > 0) buck.burnFromBasket(r.burnedB);

        // Pay depositor: principal_T (or all of tokenOut if T-depleted) plus
        // their half of the profit, on each side.
        r.toUserT = d.principalToken <= tokenOut ? d.principalToken : tokenOut;
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
