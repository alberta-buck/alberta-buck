// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "@chainlink/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol";
import "./lib/UniswapV3OracleLib.sol";

interface IERC20Decimals {
    function decimals() external view returns (uint8);
}

/// @title BuckKController -- On-Chain PID Value Stabilization
/// @notice Maintains purchasing-power parity between BUCK and its commodity basket
///         via a PID controller that publishes a dynamic credit-limit multiplier
///         (BUCK_K).  Basket pricing supports two parallel sources: Chainlink
///         AggregatorV3 feeds (for synthetic / live test baskets) and Uniswap V3
///         pool TWAPs (for real on-chain commodity-token pairs such as XAUT/USDT,
///         PAXG/USDC, cbBTC/USDC, WBTC/USDT).  Both arrays sum into the final
///         basket cost, weighted in 18-dec USD share.
///
///         The BUCK reference price is read from a Uniswap V3 BUCK/quote pool
///         (e.g. BUCK/USDT) via TWAP.  Tests override `_getBuckPrice` through
///         BuckKHarness.
contract BuckKController {

    // --- PID Gains (governance-set, 18-decimal fixed point) ---
    int256 public Kp;
    int256 public Ki;
    int256 public Kd;

    // --- PID State ---
    int256 public P;
    int256 public I;
    int256 public D;
    uint256 public lastUpdate;

    // --- Output ---
    uint256 public buckK;     // Current BUCK_K (18-decimal, 1e18 = 1.0)
    uint256 public dT;        // Minimum seconds between PID state updates

    // --- Output Limits (anti-windup) ---
    uint256 public buckKMin;
    uint256 public buckKMax;

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
    /// @dev Each pool quotes one unit of `baseToken` (e.g. XAUT) in `quoteToken`
    ///      (e.g. USDT).  We assume the quote token is a USD stablecoin pegged
    ///      to $1 (USDT, USDC).  weight is the pool's USD share of the basket
    ///      (18-dec), unitsPerWeight scales how much "one unit" represents in
    ///      18-dec USD when multiplied against `weight`.
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

    int256 constant UNIT = 1e18;
    address public governance;

    event BuckKUpdated(uint256 newBuckK, int256 error, int256 P, int256 I, int256 D);
    event GainsUpdated(int256 Kp, int256 Ki, int256 Kd);
    event BasketComponentAdded(address indexed feed, uint256 weight, uint8 decimals);
    event BasketPoolAdded(address indexed pool, address indexed base, address indexed quote, uint256 weight);
    event BuckPriceOracleSet(address indexed pool, address indexed buck, address indexed quote);

    constructor(
        int256 _Kp, int256 _Ki, int256 _Kd,
        uint256 _dT,
        uint256 _buckKMin, uint256 _buckKMax, uint256 _buckK,
        address _buckPricePool, uint32 _twapInterval,
        address _governance
    ) {
        Kp = _Kp; Ki = _Ki; Kd = _Kd;
        dT = _dT;
        buckKMin = _buckKMin;
        buckKMax = _buckKMax;
        buckPricePool = _buckPricePool;
        twapInterval = _twapInterval;
        governance = _governance;
        buckK = _buckK;
        lastUpdate = block.timestamp;
    }

    /// @notice Compute and return the current BUCK_K value.
    /// @dev If dT has elapsed, performs a full PID cycle (oracle reads + state update).
    ///      Otherwise returns cached buckK.  The minter pays gas for any PID update.
    function compute() external returns (uint256) {
        if (block.timestamp - lastUpdate < dT) {
            return buckK;
        }

        int256 basketCost = _getBasketCost();
        int256 buckPrice  = _getBuckPrice();

        int256 dt = int256(block.timestamp - lastUpdate);
        int256 error = basketCost - buckPrice;

        int256 newP = error;
        int256 newI = I + error * dt / UNIT;
        int256 newD = dt > 0
            ? (error - P) * UNIT / dt
            : int256(0);

        int256 rawOutput = UNIT
            + newP * Kp / UNIT
            + newI * Ki / UNIT
            + newD * Kd / UNIT;

        // Anti-windup clamping
        uint256 newBuckK;
        if (rawOutput < int256(buckKMin)) {
            newBuckK = buckKMin;
            if (newI > I) I = newI;
        } else if (rawOutput > int256(buckKMax)) {
            newBuckK = buckKMax;
            if (newI < I) I = newI;
        } else {
            newBuckK = uint256(rawOutput);
            I = newI;
        }

        P = newP;
        D = newD;
        buckK = newBuckK;
        lastUpdate = block.timestamp;

        emit BuckKUpdated(newBuckK, error, P, I, D);
        return newBuckK;
    }

    /// @notice Current BUCK_K without updating state (view-only).
    function currentBuckK() external view returns (uint256) {
        return buckK;
    }

    // --- Oracle Helpers ---

    function _getBasketCost() internal view returns (int256) {
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
            // Price of 1 base unit in quote (returned in quote-token decimals)
            uint256 oneBase = 10 ** bp.baseDecimals;
            int24   tick    = _poolTick(bp.pool, bp.twapInterval);
            uint256 quoteOut = UniswapV3OracleLib.getQuoteAtTick(
                tick, uint128(oneBase), bp.baseToken, bp.quoteToken
            );
            // Normalize quoteOut from quoteDecimals to 18-dec USD (assuming
            // quote == USD stable pegged to $1).
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
        // Normalize quote-decimals -> 18-dec USD.
        return int256(q * 10 ** (18 - buckQuoteDecimals));
    }

    // --- Governance ---

    function setGains(int256 _Kp, int256 _Ki, int256 _Kd) external {
        require(msg.sender == governance, "Not governance");
        Kp = _Kp; Ki = _Ki; Kd = _Kd;
        emit GainsUpdated(_Kp, _Ki, _Kd);
    }

    function setDT(uint256 _dT) external {
        require(msg.sender == governance, "Not governance");
        dT = _dT;
    }

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
    /// @param pool         The V3 pool address (BASE/QUOTE).
    /// @param baseToken    The commodity-pegged token (XAUT, PAXG, cbBTC, WBTC).
    /// @param quoteToken   The USD stablecoin paired in the pool (USDT, USDC).
    /// @param weight       18-dec USD share contributed by this pool.
    /// @param baseDecimals Decimals of `baseToken`.
    /// @param quoteDecimals Decimals of `quoteToken`.
    /// @param poolTwapInterval Seconds of TWAP to consult; 0 means use spot (slot0).
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
