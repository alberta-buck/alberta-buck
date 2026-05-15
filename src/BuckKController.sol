// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "@chainlink/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol";
import "./lib/UniswapV3OracleLib.sol";
import "./BuckKControllerBase.sol";

interface IERC20Decimals {
    function decimals() external view returns (uint8);
}

/// @title BuckKController -- On-Chain PID Value Stabilization
///                          (USD-intermediated embodiment)
///
/// @notice Concrete subclass of BuckKControllerBase that reads the BUCK
///         reference price from a Uniswap V3 BUCK/USDx pool and assembles
///         the basket cost from Chainlink feeds + Uniswap V3 commodity
///         pools, all denominated in USD.  The PID error is BUCK-in-USD
///         minus basket-in-USD; positive Kp expands credit when BUCK
///         trades above basket (deflation) and contracts when BUCK trades
///         below basket (inflation).
///
///         For the USD-free alternative -- where BUCK measures itself
///         directly against TOKEN/BUCK pools with no stablecoin
///         intermediary -- see BuckKControllerDirect.sol.
contract BuckKController is BuckKControllerBase {

    // --- BUCK Price Oracle (Uniswap V3) ---
    address public buckPricePool;       // V3 pool: BUCK paired with a USD-stable quote
    address public buckToken;           // BUCK ERC20 (the base token in the pool)
    address public buckQuoteToken;      // Quote ERC20 (e.g. USDT or USDC)
    uint8   public buckQuoteDecimals;   // Decimals of the quote token (6 for USDT/USDC)
    uint32  public twapInterval;        // TWAP window in seconds for buck pool
    uint128 internal constant ONE_BUCK = 1e18; // 1 whole BUCK (assumes 18-dec BUCK)

    // --- Chainlink basket ---
    struct BasketComponent {
        AggregatorV3Interface feed;
        uint256 weight;         // 18-dec, sum across BOTH arrays = 1e18
        uint8   feedDecimals;
    }
    BasketComponent[] public basket;

    // --- Uniswap V3 basket pools ---
    struct BasketPool {
        address pool;
        address baseToken;
        address quoteToken;
        uint256 weight;             // 18-dec USD share of basket (sum across all = 1e18)
        uint8   baseDecimals;
        uint8   quoteDecimals;
        uint32  twapInterval;       // seconds; 0 means use slot0() spot price
    }
    BasketPool[] public basketPools;

    event BasketComponentAdded(address indexed feed, uint256 weight, uint8 decimals);
    event BasketPoolAdded(address indexed pool, address indexed base, address indexed quote, uint256 weight);
    event BuckPriceOracleSet(address indexed pool, address indexed buck, address indexed quote);

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _buckPricePool, uint32 _twapInterval,
        address _governance
    ) BuckKControllerBase(_Kp, _Ki, _Kd, _dT, _buckKMin, _buckKMax, _buckK, _governance) {
        buckPricePool = _buckPricePool;
        twapInterval  = _twapInterval;
    }

    // --- Subclass override: provide (buckValue, basketValue) for the base --- //

    function _readReferences() internal view override
        returns (int256 buckValue, int256 basketValue)
    {
        buckValue   = _getBuckPrice();
        basketValue = _getBasketCost();
    }

    // --- Oracle helpers --------------------------------------------------- //

    function _getBasketCost() internal view virtual returns (int256) {
        int256 total = 0;

        // Chainlink-feed components
        for (uint i = 0; i < basket.length; i++) {
            BasketComponent storage comp = basket[i];
            (, int256 price,,,) = comp.feed.latestRoundData();
            int256 normalized = price * int256(10 ** (18 - comp.feedDecimals));
            total += normalized * int256(comp.weight) / UNIT;
        }

        // Uniswap V3 pool components
        for (uint i = 0; i < basketPools.length; i++) {
            BasketPool storage bp = basketPools[i];
            uint256 oneBase = 10 ** bp.baseDecimals;
            int24   tick    = _poolTick(bp.pool, bp.twapInterval);
            uint256 quoteOut = UniswapV3OracleLib.getQuoteAtTick(
                tick, uint128(oneBase), bp.baseToken, bp.quoteToken
            );
            int256 normalized = int256(quoteOut * 10 ** (18 - bp.quoteDecimals));
            total += normalized * int256(bp.weight) / UNIT;
        }

        return total;
    }

    function _poolTick(address pool, uint32 secondsAgo) internal view returns (int24 tick) {
        if (secondsAgo == 0) {
            (, tick,,,,,) = IUniswapV3Pool(pool).slot0();
        } else {
            tick = UniswapV3OracleLib.consult(pool, secondsAgo);
        }
    }

    /// @notice BUCK reference price, expressed in 18-dec USD.
    /// @dev Reads BUCK/quote TWAP via the Uniswap V3 oracle.  Quote token must
    ///      be a USD stablecoin (treated as $1).  Tests override via harness.
    function _getBuckPrice() internal view virtual returns (int256) {
        require(buckPricePool != address(0), "BuckK: no BUCK price oracle");
        int24 tick    = _poolTick(buckPricePool, twapInterval);
        uint256 q     = UniswapV3OracleLib.getQuoteAtTick(
            tick, ONE_BUCK, buckToken, buckQuoteToken
        );
        return int256(q * 10 ** (18 - buckQuoteDecimals));
    }

    // --- Embodiment-specific governance ----------------------------------- //

    function addBasketComponent(address feed, uint256 weight, uint8 feedDecimals) external {
        require(msg.sender == governance, "Not governance");
        basket.push(BasketComponent({
            feed: AggregatorV3Interface(feed),
            weight: weight,
            feedDecimals: feedDecimals
        }));
        emit BasketComponentAdded(feed, weight, feedDecimals);
    }

    /// @notice Register a Uniswap V3 pool as a basket-cost source.
    function addBasketPool(
        address pool,
        address baseToken,
        address quoteToken,
        uint256 weight,
        uint8   baseDecimals,
        uint8   quoteDecimals,
        uint32  poolTwapInterval
    ) external {
        require(msg.sender == governance, "Not governance");
        require(pool != address(0), "pool=0");
        require(quoteDecimals <= 18 && baseDecimals <= 18, "decimals>18");
        basketPools.push(BasketPool({
            pool: pool,
            baseToken: baseToken,
            quoteToken: quoteToken,
            weight: weight,
            baseDecimals: baseDecimals,
            quoteDecimals: quoteDecimals,
            twapInterval: poolTwapInterval
        }));
        emit BasketPoolAdded(pool, baseToken, quoteToken, weight);
    }

    /// @notice Configure the BUCK reference-price Uniswap V3 pool.
    function setBuckPriceOracle(
        address pool,
        address _buckToken,
        address _quoteToken,
        uint8   quoteDecimals,
        uint32  _twapInterval
    ) external {
        require(msg.sender == governance, "Not governance");
        require(quoteDecimals <= 18, "decimals>18");
        buckPricePool     = pool;
        buckToken         = _buckToken;
        buckQuoteToken    = _quoteToken;
        buckQuoteDecimals = quoteDecimals;
        twapInterval      = _twapInterval;
        emit BuckPriceOracleSet(pool, _buckToken, _quoteToken);
    }

    function basketLength() external view returns (uint256) { return basket.length; }
    function basketPoolsLength() external view returns (uint256) { return basketPools.length; }
}
