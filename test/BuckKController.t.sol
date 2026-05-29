// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/BuckKController.sol";
import "./harness/BuckKHarness.sol";
import "./mocks/MockAggregatorV3.sol";

/// @notice Unit tests for BuckKController using mock oracles.
contract BuckKControllerUnitTest is Test {
    BuckKHarness public ctrl;
    MockAggregatorV3 public goldFeed;
    MockAggregatorV3 public silverFeed;
    MockAggregatorV3 public oilFeed;
    MockAggregatorV3 public gasFeed;
    MockAggregatorV3 public copperFeed;

    address governance = makeAddr("governance");

    // Basket weights (sum = 1e18)
    //   Gold 30%, Silver 5%, Oil 30%, Gas 20%, Copper 15%
    uint256 constant W_GOLD   = 0.30e18;
    uint256 constant W_SILVER = 0.05e18;
    uint256 constant W_OIL    = 0.30e18;
    uint256 constant W_GAS    = 0.20e18;
    uint256 constant W_COPPER = 0.15e18;

    // Reference prices (8 decimals, matching Chainlink convention)
    // These are normalized per basket-unit so the weighted sum ~= 1.00e18 (= $1 BUCK target)
    //
    // Actual commodity prices:
    //   Gold ~$2,862/oz, Silver ~$31.82/oz, WTI ~$72/bbl, NatGas ~$3.50/MMBtu, Copper ~$4.20/lb
    //
    // We pick per-unit quantities so each component contributes its weight in dollars:
    //   Gold:   $0.30 worth = 0.30/2862  = 0.00010482 oz  -> price feed * qty = $0.30
    //   Silver: $0.05 worth = 0.05/31.82 = 0.00157103 oz  -> price feed * qty = $0.05
    //   Oil:    $0.30 worth = 0.30/72    = 0.00416667 bbl -> price feed * qty = $0.30
    //   Gas:    $0.20 worth = 0.20/3.50  = 0.05714286 MMBtu
    //   Copper: $0.15 worth = 0.15/4.20  = 0.03571429 lb
    //
    // But for the PID, we just need: weighted_sum(normalized_price) = basket_cost.
    // The weights ARE the "per-unit quantity" in dollar terms, and each feed returns
    // the price of one full unit.  So we need to scale the weights by 1/unit_price
    // to get basket_cost ~= 1e18.
    //
    // Simpler approach: set mock feeds to return 1e18 (= $1.00 in 18-dec),
    // then weights directly = dollar shares.  The basket cost = sum(weights) = $1.00.
    //
    // For realism, we'll use actual prices with scaling weights computed here.

    function setUp() public {
        // Deploy mock feeds with realistic prices (8 decimals)
        goldFeed   = new MockAggregatorV3("XAU / USD", 8, 286286487500);  // $2,862.86
        silverFeed = new MockAggregatorV3("XAG / USD", 8, 3181750000);    // $31.82
        oilFeed    = new MockAggregatorV3("WTI / USD", 8, 7200000000);    // $72.00
        gasFeed    = new MockAggregatorV3("NG / USD",  8, 350000000);     // $3.50
        copperFeed = new MockAggregatorV3("CU / USD",  8, 420000000);     // $4.20

        // Basket weights: we want basket_cost = $1.00 (= 1e18 in 18-dec).
        // normalized_price = price * 10^(18-8) = price * 1e10
        // basket_cost = sum(normalized_price_i * weight_i / 1e18)
        //
        // For gold: normalized = 286286487500 * 1e10 = 2.86286e21
        // We need:  2.86286e21 * w_gold / 1e18 = target_dollar_contribution
        //
        // If we want gold to contribute $0.30 to a $1.00 basket:
        //   w_gold = 0.30e18 * 1e18 / 2.86286e21 = 0.30e36 / 2.86286e21 = 1.0479e14
        //
        // Let's compute all weights so the basket totals $1.00:

        uint256 wGold   = _basketWeight(286286487500, 8, 0.30e18);
        uint256 wSilver = _basketWeight(3181750000,   8, 0.05e18);
        uint256 wOil    = _basketWeight(7200000000,   8, 0.30e18);
        uint256 wGas    = _basketWeight(350000000,    8, 0.20e18);
        uint256 wCopper = _basketWeight(420000000,    8, 0.15e18);

        // Deploy controller via harness
        // Gains are tuned for dT=3600 (1 hour).  With shorter dT, derivative
        // term explodes (D = (error-P)*1e18/dt), so Kd must be small relative
        // to dT.  For testing with dT=3600:
        //   Kp=0.1 -> 10% of error maps to buckK change
        //   Ki=0.01 -> slow integral accumulation
        //   Kd=0.00001 -> derivative damping (tiny because dt is in seconds)
        ctrl = new BuckKHarness(
            0.1e18,       // Kp
            0.01e18,      // Ki
            0.00001e18,   // Kd: must be tiny since D = (err-P)*1e18/dt_seconds
            3600,         // dT: 1 hour (matches gain tuning)
            0.50e18,      // buckKMin
            1.50e18,      // buckKMax
            1.0e18,       // initial buckK = 1.0
            address(0),   // no Uniswap pool (harness overrides _getBuckPrice)
            0,            // twapInterval unused
            governance
        );

        // Add basket components
        vm.startPrank(governance);
        ctrl.addBasketComponent(address(goldFeed),   wGold,   8);
        ctrl.addBasketComponent(address(silverFeed), wSilver, 8);
        ctrl.addBasketComponent(address(oilFeed),    wOil,    8);
        ctrl.addBasketComponent(address(gasFeed),    wGas,    8);
        ctrl.addBasketComponent(address(copperFeed), wCopper, 8);
        vm.stopPrank();

        // Set BUCK price to match basket ($1.00) -- no error, no PID correction
        ctrl.setBuckPrice(1.0e18);

        // Prime the controller: the first compute() captures references and
        // returns the cached buckK without running PID math.  All subsequent
        // tests start from a primed state.
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        assertTrue(ctrl.primed());
    }

    /// @dev Compute basket weight for a component given its price and target dollar share.
    function _basketWeight(int256 price, uint8 dec, uint256 targetDollars) internal pure returns (uint256) {
        // normalized = price * 10^(18 - dec)
        uint256 normalized = uint256(price) * (10 ** (18 - dec));
        // weight = targetDollars * 1e18 / normalized
        return targetDollars * 1e18 / normalized;
    }

    function test_basketCost_at_reference_prices() public view {
        int256 cost = ctrl.getBasketCost();
        // Should be very close to 1.0e18 ($1.00)
        assertApproxEqRel(uint256(cost), 1.0e18, 0.001e18);  // within 0.1%
    }

    function test_compute_at_parity() public {
        // BUCK price = basket cost -> error = 0 -> buckK stays at 1.0
        vm.warp(block.timestamp + 3601);  // advance past dT
        uint256 k = ctrl.compute();
        assertApproxEqRel(k, 1.0e18, 0.001e18);
    }

    // -------------------------------------------------------------------- //
    //  Canonical sign convention: error = BUCK - basket.                     //
    //                                                                        //
    //    BUCK BELOW basket (inflation):  error < 0  =>  buckK DECREASES.    //
    //    BUCK ABOVE basket (deflation): error > 0  =>  buckK INCREASES.    //
    //                                                                        //
    //  All shipped Kp / Ki / Kd are POSITIVE.  These two tests are the      //
    //  load-bearing assertion of the project's monetary policy direction.   //
    // -------------------------------------------------------------------- //

    function test_sign_convention_inflation_contracts_credit() public {
        // BUCK drops 5% below the basket -> credit must tighten.
        ctrl.setBuckPrice(0.95e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertLt(k, 1.0e18, "inflation must contract credit (buckK down)");
        // Error sign and P sign should both be negative.
        assertLt(ctrl.P(), 0, "P sign wrong on undervalued BUCK");
    }

    function test_sign_convention_deflation_expands_credit() public {
        // BUCK rises 5% above the basket -> credit must loosen.
        ctrl.setBuckPrice(1.05e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertGt(k, 1.0e18, "deflation must expand credit (buckK up)");
        assertGt(ctrl.P(), 0, "P sign wrong on overvalued BUCK");
    }

    function test_compute_buck_undervalued() public {
        // BUCK trades at $0.95, basket costs $1.00.
        // Sign convention: error = BUCK - basket = -0.05 (negative).
        // With Kp > 0, buckK should CONTRACT (tighter credit -> burn pressure
        // -> BUCK supply contracts -> price rises back to parity).
        ctrl.setBuckPrice(0.95e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertLt(k, 1.0e18);  // buckK should contract (inflation defense)
        emit log_named_uint("buckK (BUCK undervalued 5%)", k);
    }

    function test_compute_buck_overvalued() public {
        // BUCK trades at $1.05, basket costs $1.00.
        // Sign convention: error = BUCK - basket = +0.05 (positive).
        // With Kp > 0, buckK should EXPAND (more credit -> new mints + sales
        // -> BUCK supply grows -> price falls back to parity).
        ctrl.setBuckPrice(1.05e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertGt(k, 1.0e18);  // buckK should expand (deflation defense)
        emit log_named_uint("buckK (BUCK overvalued 5%)", k);
    }

    function test_compute_caches_within_dT() public {
        // First compute
        vm.warp(block.timestamp + 3601);
        ctrl.setBuckPrice(0.90e18);
        uint256 k1 = ctrl.compute();

        // Change price but don't advance time past dT -- should return cached value
        ctrl.setBuckPrice(1.10e18);
        uint256 k2 = ctrl.compute();
        assertEq(k1, k2);  // cached, not recomputed
    }

    function test_anti_windup_ceiling() public {
        // Massive BUCK overvaluation (deflation) -> error very positive ->
        // buckK should clamp at buckKMax (1.50).
        ctrl.setBuckPrice(2.0e18);  // BUCK trades at 2x basket
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertEq(k, 1.50e18);  // clamped at buckKMax
    }

    function test_anti_windup_floor() public {
        // Massive BUCK undervaluation (inflation) -> error very negative ->
        // buckK should clamp at buckKMin (0.50).
        ctrl.setBuckPrice(0.10e18);  // BUCK trades at 10% of basket
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertEq(k, 0.50e18);  // clamped at buckKMin
    }

    function test_commodity_price_shock_oil() public {
        // Oil doubles: $72 -> $144.  Basket cost increases.
        oilFeed.setPrice(14400000000);  // $144.00
        int256 newCost = ctrl.getBasketCost();
        // Oil was 30% of basket; doubling it adds ~$0.30 -> basket ~$1.30
        assertApproxEqRel(uint256(newCost), 1.30e18, 0.01e18);  // within 1%

        // With BUCK still at $1.00 and basket now $1.30, BUCK is undervalued
        // versus the basket: error = BUCK - basket = -0.30 -> buckK CONTRACTS
        // (BUCK has lost ~23% purchasing power vs the basket; the controller
        // tightens credit to throttle supply growth until BUCK price catches up).
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertLt(k, 1.0e18);
        emit log_named_uint("buckK (oil doubled)", k);
    }

    function test_multi_step_pid_convergence() public {
        // Simulate sustained BUCK undervaluation over multiple PID cycles.
        // With error = BUCK - basket = -0.03 sustained, both P and I terms
        // are negative, so buckK trends downward overall (credit contraction
        // in response to inflation).
        ctrl.setBuckPrice(0.97e18);  // 3% undervaluation

        // NOTE: track time via a locally-incremented uint256 instead of
        // re-reading block.timestamp each iteration.  solc 0.8.28's
        // optimizer folds identical `block.timestamp + N` reads across
        // intervening vm.warp calls (CSE doesn't know vm.warp mutates
        // the TIMESTAMP opcode), so a loop of vm.warp(block.timestamp +
        // 3601) silently no-ops on iterations 2..N -- elapsed stays at
        // 3601 and only the first PID cycle runs.  Tracking `t` outside
        // the optimizer's view sidesteps the fold.
        uint256 t = block.timestamp;
        for (uint i = 0; i < 10; i++) {
            t += 3601;
            vm.warp(t);
            ctrl.compute();
        }
        uint256 finalK = ctrl.buckK();
        // After 10 cycles of sustained inflation, buckK should be measurably below 1.0.
        assertLt(finalK, 0.999e18);
        // Integral should be negative (accumulated negative error).
        assertLt(ctrl.I(), 0);
        emit log_named_uint("buckK after 10 cycles (3% underval)", finalK);
        emit log_named_int("integral after 10 cycles", ctrl.I());
    }

    // -------------------------------------------------------------------- //
    //  dt clamp (long-gap protection)                                        //
    // -------------------------------------------------------------------- //

    function test_dTMax_default_is_unbounded() public view {
        assertEq(ctrl.dTMax(), type(uint256).max);
    }

    function test_setDTMax_governance_required() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        ctrl.setDTMax(7200);
    }

    function test_setDTMax_rejects_below_dT() public {
        vm.prank(governance);
        vm.expectRevert("dTMax<dT");
        ctrl.setDTMax(1800);  // dT is 3600
    }

    function test_dTMax_clamps_long_gap_integral() public {
        // Disable derivative for these tests -- otherwise a first-cycle
        // step in error spikes D*Kd huge enough to rail the output, which
        // freezes the integral via anti-windup and masks the clamp effect.
        vm.prank(governance);
        ctrl.setGains(0.1e18, 0.01e18, 0);

        // 1% under-valuation: small enough that PID stays in-band and the
        // integral is the term we actually observe.
        ctrl.setBuckPrice(0.99e18);

        // One normal cycle to capture the per-dT integral increment.
        // BUCK at $0.99 -> error = -0.01 -> I should accumulate negatively.
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
        int256 iAfterOneCycle = ctrl.I();
        assertLt(iAfterOneCycle, 0, "first cycle did not accumulate I");

        // Configure dTMax = dT (= 3600) and warp a full day.  The controller
        // should treat the gap as one dT-step worth of error.
        vm.prank(governance);
        ctrl.setDTMax(3600);

        int256 iBefore = ctrl.I();
        vm.warp(block.timestamp + 24 hours);
        ctrl.compute();
        int256 deltaClamped = ctrl.I() - iBefore;

        emit log_named_int("I delta over one normal cycle",  iAfterOneCycle);
        emit log_named_int("I delta over a clamped 24h gap", deltaClamped);

        // With clamp: deltaClamped ~= iAfterOneCycle.
        // Without clamp it would be ~24x larger.  Allow 50% slack.
        int256 deltaAbs    = deltaClamped < 0 ? -deltaClamped : deltaClamped;
        int256 oneCycleAbs = iAfterOneCycle < 0 ? -iAfterOneCycle : iAfterOneCycle;
        assertLe(deltaAbs, oneCycleAbs * 3 / 2, "dTMax clamp ineffective");

        // lastUpdate advances to real block.timestamp (no backlog).
        assertEq(ctrl.lastUpdate(), block.timestamp);
    }

    function test_dTMax_does_not_drive_buckK_to_rail_after_long_gap() public {
        vm.prank(governance);
        ctrl.setGains(0.1e18, 0.01e18, 0);
        vm.prank(governance);
        ctrl.setDTMax(3600);

        // 24-hour silence with sustained 5% under-valuation.  Under the
        // BUCK-minus-basket convention, error = -0.05 so buckK should
        // descend (not ascend).  Without the dTMax clamp the 24h integral
        // would slam buckK against the lower rail; with the clamp it should
        // dip just below 1.0 but stay well clear of buckKMin.
        ctrl.setBuckPrice(0.95e18);
        vm.warp(block.timestamp + 24 hours);
        uint256 k = ctrl.compute();

        assertGt(k, 0.90e18, "buckK overshot below band after clamped long gap");
        assertLt(k, 1.0e18,  "buckK did not respond at all");
    }

    // -------------------------------------------------------------------- //
    //  Priming + dS-compensated derivative                                   //
    // -------------------------------------------------------------------- //

    /// @dev Priming now happens in the constructor.  `primed` is true
    ///      immediately after deploy, `I` is pre-loaded for steady-state
    ///      continuity (error = 0 assumed at deploy), and price references
    ///      are set to UNIT (parity).  No separate prime branch runs at
    ///      compute() time.
    function test_constructor_primes_controller() public {
        // Initial buckK chosen above UNIT so the I-prime math is observable.
        BuckKHarness fresh = new BuckKHarness(
            0.1e18, 0.01e18, 0,
            3600,
            0.50e18, 1.50e18,
            1.20e18,                 // buckK starts above neutral
            address(0xdead), 600,
            governance
        );

        assertTrue(fresh.primed(),         "should be primed at construction");
        assertEq(fresh.lastBasketCost(), int256(1e18), "basket reference not at parity");
        assertEq(fresh.lastBuckPrice(),  int256(1e18), "buck reference not at parity");

        // I = ((buckK - UNIT) * UNIT) / Ki
        //   = ((1.20e18 - 1e18) * 1e18) / 0.01e18
        //   = (0.20e36) / 0.01e18
        //   = 20e18
        assertApproxEqRel(fresh.I(), 20e18, 0.0001e18,
                          "I not pre-loaded for steady-state continuity");
        // P and D start at zero (no proportional or derivative history).
        assertEq(fresh.P(), 0, "P should be 0 at construction");
        assertEq(fresh.D(), 0, "D should be 0 at construction");
    }

    /// @dev Steady-state continuity: when oracles read at parity (BUCK == basket)
    ///      the very first compute() must reproduce the initial buckK output.
    ///      This is the property that justifies constructor-time I priming.
    function test_first_compute_at_parity_reproduces_buckK() public {
        BuckKHarness fresh = new BuckKHarness(
            0.1e18, 0.01e18, 0,
            3600,
            0.50e18, 1.50e18,
            1.20e18,                  // non-neutral starting buckK
            address(0xdead), 600,
            governance
        );
        _wireBasket(fresh);
        fresh.setBuckPrice(1.0e18);   // BUCK at parity (basket = $1.00)

        vm.warp(block.timestamp + 3601);
        uint256 k = fresh.compute();
        assertApproxEqRel(k, 1.20e18, 0.0001e18,
                          "first compute at parity did not reproduce initial buckK");
    }

    /// @dev Priming doesn't make the first-after-step cycle spike-free
    ///      (derivative correctly responds to a real change in process).
    ///      What it buys: after the new equilibrium is reached, P_prev =
    ///      new error, so the *next* cycle sees a tiny (P - P_prev) and
    ///      D collapses.  Pre-priming, every fresh deploy with non-zero
    ///      Kd produced a perpetual derivative contribution because P_prev
    ///      was permanently stuck at 0.
    function test_derivative_dies_after_step_settles() public {
        BuckKHarness fresh = _makeHarness(0.1e18, 0.01e18, 0.00001e18, 3600);
        _wireBasket(fresh);
        fresh.setBuckPrice(1.0e18);

        // Track time via locally-incremented `t` -- see the note on
        // test_multi_step_pid_convergence above.  Three consecutive
        // vm.warp(block.timestamp + 3601) calls would CSE-fold to a
        // single advance under the solc 0.8.28 optimizer.
        uint256 t = block.timestamp;
        t += 3601;
        vm.warp(t);
        fresh.compute();   // prime

        // Step: BUCK drops 1%.
        fresh.setBuckPrice(0.99e18);
        t += 3601;
        vm.warp(t);
        fresh.compute();
        int256 d1 = fresh.D();

        // Hold steady -- next cycle's (error - P) ~= 0, so D dies.
        t += 3601;
        vm.warp(t);
        fresh.compute();
        int256 d2 = fresh.D();

        int256 absD1 = d1 < 0 ? -d1 : d1;
        int256 absD2 = d2 < 0 ? -d2 : d2;
        emit log_named_int("D on step",            d1);
        emit log_named_int("D one cycle past step", d2);

        assertGt(absD1, 0,                 "D did not respond to step");
        assertLt(absD2 * 10, absD1,        "D did not die after equilibrium");
    }

    /// @dev A pure setpoint shift (basket moved, BUCK didn't) should not
    ///      drive derivative action -- dS compensation strips the basket
    ///      delta out of (error - P) before dividing by dt.
    function test_dS_suppresses_derivative_on_basket_only_shift() public {
        // Reset to gains where Kd > 0 so we'd actually see a derivative
        // contribution if dS compensation were absent.
        vm.prank(governance);
        ctrl.setGains(0.1e18, 0.01e18, 0.0001e18);

        // Force a second prime under the new gains.  setUp's prime captured
        // P with Kd=0.00001 baked in; we want a fresh reference point.
        vm.warp(block.timestamp + 3601);
        ctrl.compute();

        int256 dBeforeShift = ctrl.D();

        // Shift the basket UP (gold spike) -- BUCK price unchanged.
        // basketCost grows by ~30% * (gold doubling) = ~30% basket move.
        goldFeed.setPrice(286286487500 * 2);

        vm.warp(block.timestamp + 3601);
        ctrl.compute();

        int256 dAfterShift = ctrl.D();
        int256 absD        = dAfterShift < 0 ? -dAfterShift : dAfterShift;

        emit log_named_int("D before basket shift", dBeforeShift);
        emit log_named_int("D after  basket shift", dAfterShift);

        // Without dS comp the derivative would be ~ (basketShift / dt) * UNIT
        // = 0.30e18 * 1e18 / 3600 ~= 8.3e31.  With dS, basket shift is
        // subtracted out of the (error - P) numerator, so D should be
        // basically noise (well under 1e18 in magnitude).
        assertLt(uint256(absD), 1e16, "dS compensation failed: spurious D on basket shift");

        // Sanity: the integral and proportional terms still react -- this
        // test isolates dS's effect on D, not the rest of the controller.
        assertNotEq(ctrl.P(), 0, "P should reflect the new error");
    }

    function _makeHarness(int256 _Kp, int256 _Ki, int256 _Kd, uint256 _dT) internal returns (BuckKHarness h) {
        h = new BuckKHarness(
            _Kp, _Ki, _Kd, _dT,
            0.50e18, 1.50e18, 1.0e18,
            address(0), 0, governance
        );
    }

    function _wireBasket(BuckKHarness h) internal {
        uint256 wGold   = _basketWeight(286286487500, 8, 0.30e18);
        uint256 wSilver = _basketWeight(3181750000,   8, 0.05e18);
        uint256 wOil    = _basketWeight(7200000000,   8, 0.30e18);
        uint256 wGas    = _basketWeight(350000000,    8, 0.20e18);
        uint256 wCopper = _basketWeight(420000000,    8, 0.15e18);
        vm.startPrank(governance);
        h.addBasketComponent(address(goldFeed),   wGold,   8);
        h.addBasketComponent(address(silverFeed), wSilver, 8);
        h.addBasketComponent(address(oilFeed),    wOil,    8);
        h.addBasketComponent(address(gasFeed),    wGas,    8);
        h.addBasketComponent(address(copperFeed), wCopper, 8);
        vm.stopPrank();
    }

    function test_governance_setGains() public {
        vm.prank(governance);
        ctrl.setGains(0.2e18, 0.02e18, 0.1e18);
        assertEq(ctrl.Kp(), 0.2e18);
        assertEq(ctrl.Ki(), 0.02e18);
        assertEq(ctrl.Kd(), 0.1e18);
    }

    function test_governance_setGains_reverts_non_gov() public {
        vm.prank(makeAddr("attacker"));
        vm.expectRevert("Not governance");
        ctrl.setGains(0.2e18, 0.02e18, 0.1e18);
    }
}

/// @notice Fork test: reads live Chainlink XAU/USD and XAG/USD from mainnet.
///         Mocks oil, gas, copper.  Verifies BUCK_K PID against real oracle data.
contract BuckKControllerForkTest is Test {
    BuckKHarness public ctrl;
    MockAggregatorV3 public oilFeed;
    MockAggregatorV3 public gasFeed;
    MockAggregatorV3 public copperFeed;

    // Live Chainlink feeds on Ethereum mainnet
    address constant GOLD_FEED   = 0x214eD9Da11D2fbe465a6fc601a91E62EbEc1a0D6;
    address constant SILVER_FEED = 0x379589227b15F1a12195D3f2d90bBc9F31f95235;

    address governance = makeAddr("governance");

    function setUp() public {
        // Fork-only: the live Chainlink gold/silver feeds exist only on a
        // mainnet fork.  Under a plain `forge test` (no --fork-url) the feed
        // address has no code, so skip rather than fail.  Runs for real under
        // `make test-fork-mainnet`.
        if (GOLD_FEED.code.length == 0) {
            vm.skip(true);
            return;
        }

        // Mock feeds for commodities without live Chainlink data
        oilFeed    = new MockAggregatorV3("WTI / USD", 8, 7200000000);   // $72.00
        gasFeed    = new MockAggregatorV3("NG / USD",  8, 350000000);    // $3.50
        copperFeed = new MockAggregatorV3("CU / USD",  8, 420000000);    // $4.20

        // Read actual gold and silver prices from forked mainnet
        (, int256 goldPrice,,,) = AggregatorV3Interface(GOLD_FEED).latestRoundData();
        (, int256 silverPrice,,,) = AggregatorV3Interface(SILVER_FEED).latestRoundData();

        emit log_named_int("Live gold price (8 dec)", goldPrice);
        emit log_named_int("Live silver price (8 dec)", silverPrice);

        // Compute basket weights targeting $1.00 total
        uint256 wGold   = _basketWeight(goldPrice,   8, 0.30e18);
        uint256 wSilver = _basketWeight(silverPrice, 8, 0.05e18);
        uint256 wOil    = _basketWeight(7200000000,  8, 0.30e18);
        uint256 wGas    = _basketWeight(350000000,   8, 0.20e18);
        uint256 wCopper = _basketWeight(420000000,   8, 0.15e18);

        ctrl = new BuckKHarness(
            0.1e18,       // Kp
            0.01e18,      // Ki
            0.00001e18,   // Kd
            3600,         // dT: 1 hour
            0.50e18,      // buckKMin
            1.50e18,      // buckKMax
            1.0e18,       // initial buckK
            address(0),   // no pool
            0,
            governance
        );

        vm.startPrank(governance);
        ctrl.addBasketComponent(GOLD_FEED,           wGold,   8);
        ctrl.addBasketComponent(SILVER_FEED,         wSilver, 8);
        ctrl.addBasketComponent(address(oilFeed),    wOil,    8);
        ctrl.addBasketComponent(address(gasFeed),    wGas,    8);
        ctrl.addBasketComponent(address(copperFeed), wCopper, 8);
        vm.stopPrank();

        ctrl.setBuckPrice(1.0e18);

        // Prime the controller (see unit-test setUp for the why).
        vm.warp(block.timestamp + 3601);
        ctrl.compute();
    }

    function _basketWeight(int256 price, uint8 dec, uint256 targetDollars) internal pure returns (uint256) {
        uint256 normalized = uint256(price) * (10 ** (18 - dec));
        return targetDollars * 1e18 / normalized;
    }

    function test_fork_basketCost_uses_live_gold_silver() public {
        int256 cost = ctrl.getBasketCost();
        emit log_named_int("Fork basket cost (18 dec)", cost);
        // Basket should be close to $1.00 since weights were calibrated to reference prices
        assertApproxEqRel(uint256(cost), 1.0e18, 0.02e18);  // within 2%
    }

    function test_fork_compute_at_parity() public {
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertApproxEqRel(k, 1.0e18, 0.01e18);
        emit log_named_uint("Fork buckK at parity", k);
    }

    function test_fork_gold_shock() public {
        // Simulate gold rising 10% (live feed still returns original; but we can
        // test by changing BUCK price to be relatively cheaper)
        ctrl.setBuckPrice(0.97e18);  // BUCK undervalued 3% vs basket
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertGt(k, 1.0e18);
        emit log_named_uint("Fork buckK (BUCK undervalued 3%)", k);
    }

    function test_fork_pid_multiple_cycles() public {
        // 5% sustained undervaluation over 5 PID cycles
        ctrl.setBuckPrice(0.95e18);

        for (uint i = 0; i < 5; i++) {
            vm.warp(block.timestamp + 3601);
            ctrl.compute();
        }

        uint256 k = ctrl.buckK();
        assertGt(k, 1.0e18);
        emit log_named_uint("Fork buckK after 5 cycles (5% underval)", k);

        // Integral state should be non-zero (accumulated error)
        int256 integral = ctrl.I();
        assertGt(integral, 0);
        emit log_named_int("Fork integral after 5 cycles", integral);
    }
}
