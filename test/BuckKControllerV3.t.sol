// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckKController}    from "../src/BuckKController.sol";
import {UniswapV3OracleLib} from "../src/lib/UniswapV3OracleLib.sol";
import {MockERC20}          from "./mocks/MockERC20.sol";
import {UniswapV3Fixture}   from "./fixtures/UniswapV3Fixture.sol";
import {IUniswapV3Pool}     from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

/// @title BuckKControllerV3Test -- locally deployed Uniswap V3 pool basket
/// @notice Validates that BuckKController can read commodity-token prices from
///         real (locally-deployed) Uniswap V3 pools, computes a basket cost
///         consistent with their initialized prices, and runs an amortized
///         PID cycle keyed on `dT = 60` seconds.
///
///         Pool basket targets the production-shape config:
///           XAUT/USDT  25%  ($4000/oz gold)
///           PAXG/USDC  25%  ($4000/oz gold)
///           cbBTC/USDC 25%  ($100,000/BTC)
///           WBTC/USDT  25%  ($100,000/BTC)
///         Basket-cost target: $1.00 per BUCK at parity.
///
///         The BUCK reference price comes from a locally-deployed BUCK/USDT
///         pool initialized at exactly 1 BUCK = 1 USDT, mirroring the design
///         narrative ("first BUCK minting agents establish a 100k/100k
///         BUCK/USDT pool"). Liquidity-providing isn't needed for slot0()
///         spot reads; that's a follow-up once we exercise swap-induced
///         price drift to drive the PID.
contract BuckKControllerV3Test is Test, UniswapV3Fixture {
    BuckKController public ctrl;

    // Tokens (real-world decimal layouts)
    MockERC20 public usdt;
    MockERC20 public usdc;
    MockERC20 public xaut;
    MockERC20 public paxg;
    MockERC20 public cbbtc;
    MockERC20 public wbtc;
    MockERC20 public buck;

    // Pools
    address public xautUsdt;
    address public paxgUsdc;
    address public cbbtcUsdc;
    address public wbtcUsdt;
    address public buckUsdt;

    address governance = makeAddr("governance");

    // Reference prices ($/unit) used to initialize pools
    uint256 constant GOLD_USD = 4000;     // $4000 / oz
    uint256 constant BTC_USD  = 100000;   // $100,000 / BTC

    // Basket weight: equal 25% across all four commodity pools, sums to 1e18
    uint256 constant W_EACH = 0.25e18;

    function setUp() public {
        setUpV3();

        // Mainnet-shape decimals
        usdt  = new MockERC20("Tether USD",            "USDT",  6);
        usdc  = new MockERC20("USD Coin",              "USDC",  6);
        xaut  = new MockERC20("Tether Gold",           "XAUT",  6);   // mainnet XAUT is 6-dec
        paxg  = new MockERC20("PAX Gold",              "PAXG", 18);
        cbbtc = new MockERC20("Coinbase Wrapped BTC",  "cbBTC", 8);
        wbtc  = new MockERC20("Wrapped BTC",           "WBTC",  8);
        buck  = new MockERC20("Alberta Buck",          "BUCK", 18);

        // Create + initialize pools at desired spot prices, fee tier 0.3%
        // 1 XAUT (1e6 in 6-dec) = GOLD_USD USDT (GOLD_USD * 1e6 in 6-dec)
        xautUsdt  = _createAndInitPool(address(xaut),  1e6,           address(usdt), GOLD_USD * 1e6, 3000);
        // 1 PAXG (1e18 in 18-dec) = GOLD_USD USDC (GOLD_USD * 1e6 in 6-dec)
        paxgUsdc  = _createAndInitPool(address(paxg),  1e18,          address(usdc), GOLD_USD * 1e6, 3000);
        // 1 cbBTC (1e8 in 8-dec) = BTC_USD USDC (BTC_USD * 1e6 in 6-dec)
        cbbtcUsdc = _createAndInitPool(address(cbbtc), 1e8,           address(usdc), BTC_USD  * 1e6, 3000);
        // 1 WBTC = BTC_USD USDT
        wbtcUsdt  = _createAndInitPool(address(wbtc),  1e8,           address(usdt), BTC_USD  * 1e6, 3000);
        // 1 BUCK (1e18 in 18-dec) = 1 USDT (1e6 in 6-dec)
        buckUsdt  = _createAndInitPool(address(buck),  1e18,          address(usdt), 1 * 1e6,        3000);

        // Seed liquidity in the BUCK/USDT pool only -- the four basket pools
        // are read via slot0() and don't need depth.  Liquidity = 1e17
        // approximates the design's "100k BUCK / 100k USDT" first-pool size
        // (full-range V3: amount0 ~= L/sqrtP, amount1 ~= L*sqrtP, and at the
        // 1:1 init price each side resolves to ~1e23 BUCK + ~1e11 USDT).
        buck.mint(address(this), 1e30);
        usdt.mint(address(this), 1e30);
        _mintFullRange(buckUsdt, 1e17);

        // Deploy controller targeting once-per-minute PID cycles.
        //
        // Kd left at 0 for these tests because the derivative term
        // D = (err - P_prev) * 1e18 / dt scales as 1/dt; at dT=60 even sub-
        // ppm tick-rounding "errors" produce a huge first-cycle spike from
        // P_prev = 0 -> measured.  Production tuning will introduce a
        // velocity-form derivative or a first-cycle skip; that's outside
        // the scope of wiring-up V3 reads.
        ctrl = new BuckKController(
            0.1e18,    // Kp
            0.01e18,   // Ki
            0,         // Kd (see note above)
            60,        // dT: 60s, ~5 mainnet blocks
            0.50e18,   // buckKMin
            1.50e18,   // buckKMax
            1.0e18,    // initial buckK
            buckUsdt,  // BUCK price oracle pool
            0,         // twapInterval=0 -> use slot0() spot price
            governance
        );

        // Configure BUCK price oracle (decimals + token addresses)
        vm.prank(governance);
        ctrl.setBuckPriceOracle(buckUsdt, address(buck), address(usdt), 6, 0);

        // Wire up basket pools.  Each contributes 25% of the $1.00 basket;
        // since every pool's quote token is a $1 stablecoin, the controller
        // sees: weighted_sum(price_in_quote * weight) where quote is $1.
        // With each pool at its on-chain reference price, the basket cost
        // equals weight * $price_per_unit ... which would be huge unless we
        // re-scale weights to the dollar share.  The controller treats
        // `weight` as the USD-share, but multiplies it against the quoted
        // *price of one base unit*.  So we must re-scale weights as:
        //   effectiveWeight = (USD_share * 1e18) / price_per_unit_18dec
        // so that price * effectiveWeight / 1e18 == USD_share.
        vm.startPrank(governance);
        ctrl.addBasketPool(xautUsdt,  address(xaut),  address(usdt),
            _scaleWeight(W_EACH, GOLD_USD * 1e18), 6,  6, 0);
        ctrl.addBasketPool(paxgUsdc,  address(paxg),  address(usdc),
            _scaleWeight(W_EACH, GOLD_USD * 1e18), 18, 6, 0);
        ctrl.addBasketPool(cbbtcUsdc, address(cbbtc), address(usdc),
            _scaleWeight(W_EACH, BTC_USD  * 1e18), 8,  6, 0);
        ctrl.addBasketPool(wbtcUsdt,  address(wbtc),  address(usdt),
            _scaleWeight(W_EACH, BTC_USD  * 1e18), 8,  6, 0);
        vm.stopPrank();
    }

    /// @dev `weight` parameter expected by the controller is the multiplier
    ///      applied to the 18-dec normalized price per unit.  To target a
    ///      dollar share, scale the share by (1e18 / pricePerUnit).
    function _scaleWeight(uint256 usdShare, uint256 pricePerUnit18dec) internal pure returns (uint256) {
        return (usdShare * 1e18) / pricePerUnit18dec;
    }

    // -------------------------------------------------------------------- //
    //  Pool reads                                                            //
    // -------------------------------------------------------------------- //

    function test_pool_spot_price_xaut_usdt() public view {
        // 1 XAUT should quote at GOLD_USD USDT (6-dec)
        (, int24 tick,,,,,) = IUniswapV3Pool(xautUsdt).slot0();
        uint256 q = UniswapV3OracleLib.getQuoteAtTick(tick, uint128(1e6), address(xaut), address(usdt));
        assertApproxEqRel(q, GOLD_USD * 1e6, 0.0001e18); // within 0.01%
    }

    function test_pool_spot_price_buck_usdt() public view {
        (, int24 tick,,,,,) = IUniswapV3Pool(buckUsdt).slot0();
        uint256 q = UniswapV3OracleLib.getQuoteAtTick(tick, uint128(1e18), address(buck), address(usdt));
        // 1 BUCK should quote at 1 USDT (6-dec).  Tick rounding may shift
        // by a tick or two; allow 0.05% tolerance.
        assertApproxEqRel(q, 1e6, 0.0005e18);
    }

    // -------------------------------------------------------------------- //
    //  Basket math                                                           //
    // -------------------------------------------------------------------- //

    function test_basket_cost_at_parity() public {
        int256 cost = _basketCost();
        // Target $1.00 total; allow tick-rounding noise of <0.5%
        assertApproxEqRel(uint256(cost), 1e18, 0.005e18);
        emit log_named_int("V3 basket cost (18-dec)", cost);
    }

    function test_buck_price_at_parity() public {
        int256 buckPrice = _buckPrice();
        assertApproxEqRel(uint256(buckPrice), 1e18, 0.001e18);
        emit log_named_int("V3 BUCK price (18-dec)", buckPrice);
    }

    // -------------------------------------------------------------------- //
    //  PID cycles                                                            //
    // -------------------------------------------------------------------- //

    function test_compute_at_parity() public {
        vm.warp(block.timestamp + 61);
        uint256 k = ctrl.compute();
        // BUCK and basket both at $1 -> error ~= 0 -> buckK ~= 1.0
        assertApproxEqRel(k, 1e18, 0.005e18);
    }

    function test_compute_amortizes_within_dT() public {
        // Initial: warp once past dT, run PID cycle to set lastUpdate
        vm.warp(block.timestamp + 61);
        uint256 k0 = ctrl.compute();

        // Within the next 60s, multiple "mints" share the cached buckK without
        // paying for additional oracle reads.
        for (uint i = 0; i < 5; i++) {
            vm.warp(block.timestamp + 10);   // 5x10s = 50s, all within dT
            uint256 k = ctrl.compute();
            assertEq(k, k0, "amortization broken: cache miss within dT");
        }

        // After dT elapses, next call performs the PID work.
        vm.warp(block.timestamp + 11);       // total 61s since k0
        uint256 lastUpdateBefore = ctrl.lastUpdate();
        ctrl.compute();
        assertGt(ctrl.lastUpdate(), lastUpdateBefore, "PID did not run after dT");
    }

    // -------------------------------------------------------------------- //
    //  Swap-driven price drift -> PID response                               //
    // -------------------------------------------------------------------- //

    function test_drift_buck_undervalued_pushes_buckK_down() public {
        // First PID cycle at parity to establish baseline lastUpdate / state.
        vm.warp(block.timestamp + 61);
        ctrl.compute();
        uint256 baselineK = ctrl.buckK();

        // Sell BUCK into the pool until 1 BUCK quotes at 0.95 USDT.
        _moveSpotToPrice(buckUsdt, address(buck), 1e18, address(usdt), 0.95e6);

        int256 driftedPrice = _buckPrice();
        emit log_named_int("BUCK price after drift (18-dec)", driftedPrice);
        // Confirm we actually moved the pool ~5% down (allow tick-rounding).
        assertApproxEqRel(uint256(driftedPrice), 0.95e18, 0.005e18);

        // error = BUCK - basket = -0.05 -> buckK contracts monotonically as
        // proportional + integral terms accumulate the negative error.
        uint256 prevK = baselineK;
        for (uint i = 0; i < 5; i++) {
            vm.warp(block.timestamp + 61);
            uint256 k = ctrl.compute();
            assertLe(k, prevK, "buckK regressed upward mid-drift");
            prevK = k;
        }

        assertLt(ctrl.buckK(), baselineK, "buckK did not contract under undervaluation");
        assertLt(ctrl.I(), 0, "integral did not accumulate negative error");
        emit log_named_uint("buckK after 5 cycles (BUCK 5% under)", ctrl.buckK());
        emit log_named_int ("integral after 5 cycles",                 ctrl.I());
    }

    function test_drift_buck_overvalued_pushes_buckK_up() public {
        vm.warp(block.timestamp + 61);
        ctrl.compute();
        uint256 baselineK = ctrl.buckK();

        // Buy BUCK out of the pool until 1 BUCK quotes at 1.05 USDT.
        _moveSpotToPrice(buckUsdt, address(buck), 1e18, address(usdt), 1.05e6);

        int256 driftedPrice = _buckPrice();
        emit log_named_int("BUCK price after drift (18-dec)", driftedPrice);
        assertApproxEqRel(uint256(driftedPrice), 1.05e18, 0.005e18);

        // error = BUCK - basket = +0.05 -> buckK expands monotonically.
        uint256 prevK = baselineK;
        for (uint i = 0; i < 5; i++) {
            vm.warp(block.timestamp + 61);
            uint256 k = ctrl.compute();
            assertGe(k, prevK, "buckK regressed downward mid-drift");
            prevK = k;
        }

        assertGt(ctrl.buckK(), baselineK, "buckK did not expand under overvaluation");
        assertGt(ctrl.I(), 0, "integral did not accumulate positive error");
        emit log_named_uint("buckK after 5 cycles (BUCK 5% over)",  ctrl.buckK());
        emit log_named_int ("integral after 5 cycles",              ctrl.I());
    }

    function test_drift_recovery_to_parity() public {
        // 1) Drive BUCK 5% under, run 3 cycles -> buckK contracts.
        vm.warp(block.timestamp + 61);
        ctrl.compute();

        _moveSpotToPrice(buckUsdt, address(buck), 1e18, address(usdt), 0.95e6);
        for (uint i = 0; i < 3; i++) {
            vm.warp(block.timestamp + 61);
            ctrl.compute();
        }
        uint256 contractedK = ctrl.buckK();
        assertLt(contractedK, 1e18);

        // 2) Recover BUCK back to parity (1.00) -- proportional error returns
        //    to ~0.  Integral retains its accumulated negative lean, so
        //    buckK does NOT instantly snap back; it relaxes upward.
        _moveSpotToPrice(buckUsdt, address(buck), 1e18, address(usdt), 1e6);
        for (uint i = 0; i < 3; i++) {
            vm.warp(block.timestamp + 61);
            ctrl.compute();
        }
        // After recovery, P-term contributes ~0, I-term still negative ->
        // buckK still depressed relative to neutral, but should be no lower
        // than the under-valued trough.
        assertGe(ctrl.buckK(), contractedK, "buckK kept falling after parity recovery");
        emit log_named_uint("buckK after recovery to parity", ctrl.buckK());
    }

    function test_mint_pacing_5_blocks_per_pid() public {
        // Mainnet blocks ~12s -> 5 blocks ~= 60s == dT.  Simulate a stream of
        // mints arriving roughly one per block; PID work should occur ~once
        // every 5 blocks.
        uint256 pidRuns;
        uint256 lastSeen = ctrl.lastUpdate();

        for (uint i = 0; i < 25; i++) {
            vm.warp(block.timestamp + 12);    // one block
            ctrl.compute();
            if (ctrl.lastUpdate() > lastSeen) {
                pidRuns++;
                lastSeen = ctrl.lastUpdate();
            }
        }
        // 25 blocks / 5 blocks-per-cycle = 5 expected runs (give or take 1
        // for boundary alignment).
        assertGe(pidRuns, 4, "too few PID runs");
        assertLe(pidRuns, 6, "too many PID runs");
        emit log_named_uint("PID runs over 25 blocks (12s each)", pidRuns);
    }

    // -------------------------------------------------------------------- //
    //  Internal helpers expose the controller's basket / buck-price math    //
    // -------------------------------------------------------------------- //

    function _basketCost() internal view returns (int256) {
        // Replicate _getBasketCost() against the public Uniswap math so the
        // test does not need to introspect controller internals.  Both arms
        // (Chainlink basket, V3 pools) produce 18-dec USD; for this test
        // there are no Chainlink components.
        int256 total;
        for (uint i = 0; i < ctrl.basketPoolsLength(); i++) {
            (
                address pool,
                address baseT,
                address quoteT,
                uint256 weight,
                uint8   baseDec,
                uint8   quoteDec,
                uint32  twap
            ) = ctrl.basketPools(i);
            int24 tick;
            if (twap == 0) {
                (, tick,,,,,) = IUniswapV3Pool(pool).slot0();
            } else {
                tick = UniswapV3OracleLib.consult(pool, twap);
            }
            uint256 q = UniswapV3OracleLib.getQuoteAtTick(tick, uint128(10 ** baseDec), baseT, quoteT);
            int256 normalized = int256(q * 10 ** (18 - quoteDec));
            total += normalized * int256(weight) / int256(uint256(1e18));
        }
        return total;
    }

    function _buckPrice() internal view returns (int256) {
        (, int24 tick,,,,,) = IUniswapV3Pool(ctrl.buckPricePool()).slot0();
        uint256 q = UniswapV3OracleLib.getQuoteAtTick(tick, uint128(1e18), ctrl.buckToken(), ctrl.buckQuoteToken());
        return int256(q * 10 ** (18 - ctrl.buckQuoteDecimals()));
    }
}
