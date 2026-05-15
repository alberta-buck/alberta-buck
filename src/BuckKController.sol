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
///
///         SIGN CONVENTION
///         ---------------
///         error = buckPrice - basketCost  (process minus setpoint).
///
///           BUCK BELOW basket (inflation):   error < 0 -> buckK DECREASES,
///                                            contracting credit, inducing
///                                            voluntary burns, pulling BUCK
///                                            price back up.
///
///           BUCK ABOVE basket (deflation):   error > 0 -> buckK INCREASES,
///                                            expanding credit, encouraging
///                                            new mints + sales, pulling BUCK
///                                            price back down.
///
///         All shipped gains should be POSITIVE (Kp, Ki, Kd >= 0) under this
///         convention.  Negating any gain inverts the controller and is only
///         appropriate if the operator intends the alternative credit channel
///         (expand-on-inflation, contract-on-deflation) -- see the early-2026
///         arb-scenario doc for the rationale of that alternative.
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

    /// @notice Always true after construction.  Retained for ABI compatibility
    ///         with earlier two-phase priming designs.
    ///
    ///         History: previous revisions deferred priming to the first
    ///         compute() call so that real oracle prices could be captured
    ///         as P_prev / lastBasketCost references.  That approach
    ///         required the controller to live in a "not yet primed" state
    ///         and complicated callers.  The shipped design primes I and
    ///         the price references in the constructor (assuming error = 0
    ///         and parity references at deploy), so every compute() runs
    ///         a full PID cycle.
    bool public primed;

    /// @notice Last observed basket cost (18-dec USD).  Used to compute the
    ///         setpoint shift `dS = basketCost - lastBasketCost` so that
    ///         changes in the target (e.g. gold spike, governance re-weight)
    ///         do not appear as derivative-of-process action.  Mirrors the
    ///         ezpwd::pid `dS` correction.
    int256 public lastBasketCost;

    /// @notice Last observed BUCK reference price (18-dec USD).  Cached by
    ///         compute() alongside lastBasketCost so external callers (e.g.
    ///         Buck.mint) can read the most recent oracle pair without
    ///         re-doing the V3 consult.  Drives `fundingFactor()`.
    int256 public lastBuckPrice;

    // --- Output ---
    uint256 public buckK;     // Current BUCK_K (18-decimal, 1e18 = 1.0)
    uint256 public dT;        // Minimum seconds between PID state updates

    /// @notice Maximum effective dt (seconds) used for integration / derivative.
    ///         When the actual gap (block.timestamp - lastUpdate) exceeds
    ///         dTMax, the PID treats the cycle as if only dTMax seconds had
    ///         passed.  Real `lastUpdate` still advances to block.timestamp,
    ///         so we don't accumulate a backlog across subsequent cycles.
    ///
    ///         Rationale: in early days, mints may be infrequent.  A 24h gap
    ///         followed by a single compute() with sustained error would
    ///         otherwise slug the integral with 24h of accumulated error in
    ///         one step, slamming buckK to a rail.  Clamping treats long
    ///         silences as "we don't know what happened" rather than "this
    ///         error persisted the entire time."
    ///
    ///         Default: type(uint256).max (no clamp -- backwards-compat).
    ///         Governance can set a finite value via `setDTMax`.
    uint256 public dTMax;

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
    ///
    ///      All BuckKControllers start with an initial assumption that 1 BUCK equals 1 full
    ///      commodity basket value at initialization.
    ///
    ///      The _getBuckPrice API returns the current BUCK price in the exchange currency: the
    ///      process value, and _getBasketCost returns the current BUCK commodity basket price in
    ///      the exchange currency: the setpoint value.  Both of these float freely in this
    ///      implementation.  For example, if there is a 10% USD$ inflation, we expect both the
    ///      setpoint (the basket in USD$) and the process value (the BUCK in USD$) to increase by
    ///      10%.
    ///
    ///      This is a non-ideal embodiment.
    ///
    ///      The BUCK is defined in terms of a number of commodities priced in their RWA tokens
    ///      (eg. XAUT, PAXG, cbBTC, WBTC, ...)  *directly*, measured in terms of TOKEN/BUCK pools:
    ///      XAUT/BUCK, cbBTC/BUCK, etc.  These start at zero, and a special mint process allows a
    ///      token eg. PAXG to be presented to the Buck contract, and it will mint the BUCK half of
    ///      new units in the PAXG/BUCK pool.  Later, the pool liquidity token can be returned to
    ///      Buck contract, which will redeem the current proportion of PAXG/BUCK units from the
    ///      pool, burn the BUCKs and return the PAXG to the owner.
    ///
    ///      This eliminates an intermediate token such as USDC and USDT, and also the variability
    ///      of its peg, eg. USD$.  The XAUT/BUCK pools are very low fee, to make the
    ///      XAUT->BUCK->cbBTC path a low-cost route to exchange RWA tokens.
    ///
    ///      In this embodiment, _getBuckPrice always returns 1.0, while _getBacketCost returns the
    ///      value of the defined commodity basket in BUCKs.  
    ///
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
        dTMax = type(uint256).max; // disabled by default; governance opts in

        // ----------------------------------------------------------------
        // Constructor-time PID priming (assume error = 0 at deploy)
        // ----------------------------------------------------------------
        //
        // Pre-load `I` so that the first compute() with zero residual error
        // reproduces the initial buckK value exactly.  Algebra (with P = 0,
        // D = 0):
        //     buckK = UNIT + (I * Ki) / UNIT
        //   => I = ((buckK - UNIT) * UNIT) / Ki
        //
        // When Ki == 0 (e.g. P-only tuning) the I term contributes nothing
        // regardless, so we leave I = 0.
        //
        // Set `lastBasketCost` and `lastBuckPrice` to UNIT (parity, $1.00)
        // so the FIRST compute() interprets any deviation as a one-shot
        // setpoint shift (correctly subtracted by dS in the derivative
        // calculation) rather than as a process step.  This lets us avoid
        // calling `_getBuckPrice()` / `_getBasketCost()` in the constructor
        // -- those calls would be unsafe in derived test harnesses (their
        // overrides reference storage initialised after the parent
        // constructor returns).
        //
        // `primed = true` marks the controller as ready; compute() no
        // longer has a separate prime branch.
        if (_Ki != 0) {
            I = ((int256(_buckK) - UNIT) * UNIT) / _Ki;
        }
        lastBasketCost = UNIT;
        lastBuckPrice  = UNIT;
        primed         = true;
    }

    /// @notice Run (or cache) one PID cycle and return the current BUCK_K.
    ///
    /// @dev Permissionless and side-effect-only-when-due.  If less than `dT`
    ///      seconds have elapsed since the last cycle, this is a cheap
    ///      cached read (one SLOAD + return) -- callers can invoke it
    ///      freely.  When `dT` has elapsed, performs a full PID cycle:
    ///      oracle reads, P/I/D update, anti-windup clamp, store.
    ///
    ///      Pokability: this function is intentionally external and
    ///      non-restricted so any caller (mint, burn, transfer wrapper,
    ///      keeper, individual user) can advance the PID.  In normal
    ///      operation Buck.mint() / Buck.burn() drive it; in long quiet
    ///      stretches a keeper may wish to call it to keep the integral
    ///      alive (or rely on dTMax to bound the eventual catch-up step).
    ///
    ///      Future: if a richer PID grows expensive enough that mint-time
    ///      amortization becomes painful, the cycle work can be split into
    ///      a state-machine over multiple calls -- each transfer advancing
    ///      one micro-step of (read pool i, accumulate, finalize).  The
    ///      tradeoff is that pool reads stretch over real time and so the
    ///      basket sample becomes incoherent unless every input is itself
    ///      TWAP'd over a window covering the state-machine duration.
    ///      Today's full-cycle compute is cheap enough that the simpler
    ///      "all-or-nothing per dT" pattern wins; revisit if oracle reads
    ///      grow or the basket expands materially.
    function compute() external returns (uint256) {
        uint256 elapsed = block.timestamp - lastUpdate;
        if (elapsed < dT) {
            return buckK;
        }

        int256 basketCost = _getBasketCost();   // setpoint (target value)
        int256 buckPrice  = _getBuckPrice();    // process variable (observed)
        // Sign convention: error = process - setpoint = BUCK - basket.
        //
        //   BUCK BELOW basket (monetary inflation)  =>  error < 0
        //   BUCK ABOVE basket (monetary deflation) =>  error > 0
        //
        // With Kp > 0, positive error pushes buckK UP and negative error
        // pushes it DOWN.  This matches the canonical credit-stabilizer
        // policy: deflation expands credit (encourages new mints/sales,
        // returning BUCK toward parity from above) while inflation contracts
        // credit (forces voluntary burns, returning BUCK toward parity from
        // below).  See alberta-buck-ethereum.org "BUCK_K Sign Convention".
        int256 error      = buckPrice - basketCost;

        // Priming was performed at construction time: `I` is pre-loaded for
        // steady-state continuity, `lastBasketCost` / `lastBuckPrice` are
        // initialised to UNIT (assumed parity at deploy), and `primed` is
        // already true.  No runtime prime branch is needed -- every
        // compute() runs a full PID cycle.

        // Clamp the effective integration step but advance lastUpdate to
        // real block.timestamp so the next cycle measures forward correctly.
        uint256 effective = elapsed > dTMax ? dTMax : elapsed;
        int256 dt = int256(effective);

        // Setpoint shift since last cycle.  With error = BUCK - basket, an
        // upward move in basket DECREASES error by an equal amount, so the
        // setpoint contribution to (error - P_prev) is -(basketCost - lastBasketCost).
        // Subtracting that from (error - P_prev) leaves only the process change
        // (d(BUCK)/dt) for the derivative term.
        int256 dS = lastBasketCost - basketCost;

        int256 newP = error;
        int256 newI = I + error * dt / UNIT;
        int256 newD = dt > 0
            ? (error - P - dS) * UNIT / dt
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

        P              = newP;
        D              = newD;
        buckK          = newBuckK;
        lastUpdate     = block.timestamp;
        lastBasketCost = basketCost;     // update reference for next dS
        lastBuckPrice  = buckPrice;      // cache for fundingFactor() reads

        emit BuckKUpdated(newBuckK, error, P, I, D);
        return newBuckK;
    }

    /// @notice Current BUCK_K without updating state (view-only).
    function currentBuckK() external view returns (uint256) {
        return buckK;
    }

    /// @notice Counter-cyclical insurance funding factor (18-dec, 1e18 = 1.0).
    ///
    /// @dev   factor = max(0, 1e18 + 10 * (basket - BUCK) * 1e18 / basket)
    ///
    ///        Throttles new mints when BUCK trades below its commodity
    ///        basket (BUCK undervalued / monetary inflation): the minter
    ///        must pre-hold `poolPrincipal * factor / 1e18` BUCK before
    ///        their mint is accepted.  Saturates at 0 once BUCK trades
    ///        >= 110% of basket -- in deflationary regimes the gate fully
    ///        opens and new minting is unconstrained by reserves.
    ///
    ///        Reference points (basket fixed at $1.00):
    ///          BUCK = $1.00 (parity)     => factor = 1.00
    ///          BUCK = $0.99 (1 % infl.)  => factor = 1.10
    ///          BUCK = $0.97 (3 % infl.)  => factor = 1.30
    ///          BUCK = $1.10 (10% defl.)  => factor = 0
    ///
    ///        Returns 1e18 when un-primed (basket reference is zero) so
    ///        Buck.sol's gate behaves as a unit-coverage requirement during
    ///        the very early bootstrap; Buck.sol independently bypasses the
    ///        check when totalSupply == 0 to allow the genesis mint.
    function fundingFactor() external view returns (uint256) {
        int256 b = lastBasketCost;
        if (b <= 0) return uint256(UNIT);
        int256 p = lastBuckPrice;
        int256 raw = UNIT + int256(10) * (b - p) * UNIT / b;
        if (raw <= 0) return 0;
        return uint256(raw);
    }

    // --- Oracle Helpers ---

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

    /// @notice Cap the effective integration window for any single PID cycle.
    /// @dev Pass type(uint256).max to disable (no clamp).  See `dTMax` doc.
    function setDTMax(uint256 _dTMax) external {
        require(msg.sender == governance, "Not governance");
        require(_dTMax >= dT, "dTMax<dT");
        dTMax = _dTMax;
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
