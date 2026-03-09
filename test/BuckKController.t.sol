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

    function test_compute_buck_undervalued() public {
        // BUCK trades at $0.95, basket costs $1.00 -> error = +0.05 -> buckK increases
        ctrl.setBuckPrice(0.95e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertGt(k, 1.0e18);  // buckK should expand
        emit log_named_uint("buckK (BUCK undervalued 5%)", k);
    }

    function test_compute_buck_overvalued() public {
        // BUCK trades at $1.05, basket costs $1.00 -> error = -0.05 -> buckK decreases
        ctrl.setBuckPrice(1.05e18);
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertLt(k, 1.0e18);  // buckK should contract
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

    function test_anti_windup_floor() public {
        // Massive BUCK overvaluation -> buckK should clamp at floor
        ctrl.setBuckPrice(2.0e18);  // BUCK trades at 2x basket
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertEq(k, 0.50e18);  // clamped at buckKMin
    }

    function test_anti_windup_ceiling() public {
        // Massive BUCK undervaluation -> buckK should clamp at ceiling
        ctrl.setBuckPrice(0.10e18);  // BUCK trades at 10% of basket
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertEq(k, 1.50e18);  // clamped at buckKMax
    }

    function test_commodity_price_shock_oil() public {
        // Oil doubles: $72 -> $144.  Basket cost increases.
        oilFeed.setPrice(14400000000);  // $144.00
        int256 newCost = ctrl.getBasketCost();
        // Oil was 30% of basket; doubling it adds ~$0.30 -> basket ~$1.30
        assertApproxEqRel(uint256(newCost), 1.30e18, 0.01e18);  // within 1%

        // With BUCK still at $1.00, error = +$0.30 -> buckK should increase
        vm.warp(block.timestamp + 3601);
        uint256 k = ctrl.compute();
        assertGt(k, 1.0e18);
        emit log_named_uint("buckK (oil doubled)", k);
    }

    function test_multi_step_pid_convergence() public {
        // Simulate sustained BUCK undervaluation over multiple PID cycles.
        // The integral term accumulates, so buckK should trend upward overall,
        // though individual cycles may oscillate.
        ctrl.setBuckPrice(0.97e18);  // 3% undervaluation

        for (uint i = 0; i < 10; i++) {
            vm.warp(block.timestamp + 3601);
            ctrl.compute();
        }
        uint256 finalK = ctrl.buckK();
        // After 10 cycles of sustained error, buckK should be meaningfully above 1.0
        assertGt(finalK, 1.001e18);
        // Integral should be positive (accumulated undervaluation signal)
        assertGt(ctrl.I(), 0);
        emit log_named_uint("buckK after 10 cycles (3% underval)", finalK);
        emit log_named_int("integral after 10 cycles", ctrl.I());
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
