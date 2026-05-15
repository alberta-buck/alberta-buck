// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckKController}    from "../src/BuckKController.sol";
import {UniswapV3OracleLib} from "../src/lib/UniswapV3OracleLib.sol";
import {MockERC20}          from "./mocks/MockERC20.sol";
import {UniswapV3Fixture}   from "./fixtures/UniswapV3Fixture.sol";
import {IUniswapV3Pool}     from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

/// @title BuckKControllerV3TwapTest -- TWAP-based oracle reads
/// @notice Mirrors `BuckKControllerV3Test` but configures the controller with
///         `twapInterval = 600` (10 minutes) on every basket pool and the
///         BUCK reference pool.  Verifies that:
///           - consult() returns the time-weighted average rather than spot
///           - a brief, large spike on the BUCK pool is heavily smoothed
///             (manipulation-resistance)
///           - a sustained drift converges into the TWAP only after the
///             window has fully elapsed (slow but eventual response)
///         The fixture seeds tiny liquidity on every pool (basket included)
///         so we can write observations via `pool.burn(MIN, MAX, 0)`.
contract BuckKControllerV3TwapTest is Test, UniswapV3Fixture {
    BuckKController public ctrl;

    MockERC20 public usdt;
    MockERC20 public usdc;
    MockERC20 public xaut;
    MockERC20 public paxg;
    MockERC20 public cbbtc;
    MockERC20 public wbtc;
    MockERC20 public buck;

    address public xautUsdt;
    address public paxgUsdc;
    address public cbbtcUsdc;
    address public wbtcUsdt;
    address public buckUsdt;

    address governance = makeAddr("governance");

    uint32  constant TWAP    = 600;     // 10-minute TWAP window
    uint256 constant GOLD_USD = 4000;
    uint256 constant BTC_USD  = 100000;
    uint256 constant W_EACH   = 0.25e18;

    function setUp() public {
        setUpV3();

        usdt  = new MockERC20("Tether USD",            "USDT",  6);
        usdc  = new MockERC20("USD Coin",              "USDC",  6);
        xaut  = new MockERC20("Tether Gold",           "XAUT",  6);
        paxg  = new MockERC20("PAX Gold",              "PAXG", 18);
        cbbtc = new MockERC20("Coinbase Wrapped BTC",  "cbBTC", 8);
        wbtc  = new MockERC20("Wrapped BTC",           "WBTC",  8);
        buck  = new MockERC20("Alberta Buck",          "BUCK", 18);

        xautUsdt  = _createAndInitPool(address(xaut),  1e6,  address(usdt), GOLD_USD * 1e6, 3000);
        paxgUsdc  = _createAndInitPool(address(paxg),  1e18, address(usdc), GOLD_USD * 1e6, 3000);
        cbbtcUsdc = _createAndInitPool(address(cbbtc), 1e8,  address(usdc), BTC_USD  * 1e6, 3000);
        wbtcUsdt  = _createAndInitPool(address(wbtc),  1e8,  address(usdt), BTC_USD  * 1e6, 3000);
        buckUsdt  = _createAndInitPool(address(buck),  1e18, address(usdt), 1 * 1e6,        3000);

        // Generously fund the fixture so it can mint liquidity in every pool
        // and later swap to drive the BUCK pool.
        usdt.mint (address(this), 1e30);
        usdc.mint (address(this), 1e30);
        xaut.mint (address(this), 1e30);
        paxg.mint (address(this), 1e30);
        cbbtc.mint(address(this), 1e30);
        wbtc.mint (address(this), 1e30);
        buck.mint (address(this), 1e30);

        // Seed liquidity in every pool so observations can be written via
        // burn(MIN, MAX, 0).  Small L on basket pools (we don't need depth);
        // larger L on the BUCK pool so swap drifts behave sensibly.
        _mintFullRange(xautUsdt,  1e15);
        _mintFullRange(paxgUsdc,  1e15);
        _mintFullRange(cbbtcUsdc, 1e15);
        _mintFullRange(wbtcUsdt,  1e15);
        _mintFullRange(buckUsdt,  1e17);

        // Bump cardinality so the pool can retain enough observations to
        // span the TWAP window.  64 is comfortably more than the expected
        // touch count over 600s.
        _bumpCardinality(xautUsdt,  64);
        _bumpCardinality(paxgUsdc,  64);
        _bumpCardinality(cbbtcUsdc, 64);
        _bumpCardinality(wbtcUsdt,  64);
        _bumpCardinality(buckUsdt,  64);

        // Warm up the TWAP: walk forward TWAP+ seconds, writing an
        // observation roughly every 30s on every pool.  This populates the
        // observation array so consult(pool, TWAP) does a real interpolation
        // rather than extrapolating off a single stale slot.
        for (uint i = 0; i < 22; i++) {              // 22 * 30s = 660s > TWAP
            vm.warp(block.timestamp + 30);
            _touchPool(xautUsdt);
            _touchPool(paxgUsdc);
            _touchPool(cbbtcUsdc);
            _touchPool(wbtcUsdt);
            _touchPool(buckUsdt);
        }

        // Deploy controller with TWAP enabled.  Same dT, gains, bounds as
        // the spot-price test except every pool feeds via TWAP.
        ctrl = new BuckKController(
            0.1e18, 0.01e18, 0,        // Kp, Ki, Kd
            60,                         // dT
            0.50e18, 1.50e18,           // bounds
            1.0e18,                     // initial buckK
            buckUsdt, TWAP,             // BUCK price oracle pool + window
            governance
        );
        vm.prank(governance);
        ctrl.setBuckPriceOracle(buckUsdt, address(buck), address(usdt), 6, TWAP);

        vm.startPrank(governance);
        ctrl.addBasketPool(xautUsdt,  address(xaut),  address(usdt),
            _scaleWeight(W_EACH, GOLD_USD * 1e18), 6,  6, TWAP);
        ctrl.addBasketPool(paxgUsdc,  address(paxg),  address(usdc),
            _scaleWeight(W_EACH, GOLD_USD * 1e18), 18, 6, TWAP);
        ctrl.addBasketPool(cbbtcUsdc, address(cbbtc), address(usdc),
            _scaleWeight(W_EACH, BTC_USD  * 1e18), 8,  6, TWAP);
        ctrl.addBasketPool(wbtcUsdt,  address(wbtc),  address(usdt),
            _scaleWeight(W_EACH, BTC_USD  * 1e18), 8,  6, TWAP);
        vm.stopPrank();
    }

    function _scaleWeight(uint256 usdShare, uint256 pricePerUnit18dec) internal pure returns (uint256) {
        return (usdShare * 1e18) / pricePerUnit18dec;
    }

    // -------------------------------------------------------------------- //
    //  Basic TWAP reads                                                      //
    // -------------------------------------------------------------------- //

    function test_twap_basket_cost_at_parity() public view {
        int256 cost = _twapBasketCost();
        assertApproxEqRel(uint256(cost), 1e18, 0.005e18);
        // For comparison, also read the spot basket cost -- after a quiet
        // warm-up they should be near-identical.
        int256 spot = _spotBasketCost();
        assertApproxEqRel(uint256(cost), uint256(spot), 0.001e18);
    }

    function test_twap_buck_price_at_parity() public view {
        int256 twap = _twapBuckPrice();
        int256 spot = _spotBuckPrice();
        assertApproxEqRel(uint256(twap), 1e18, 0.001e18);
        assertApproxEqRel(uint256(twap), uint256(spot), 0.001e18);
    }

    function test_twap_pid_at_parity() public {
        vm.warp(block.timestamp + 61);
        uint256 k = ctrl.compute();
        assertApproxEqRel(k, 1e18, 0.005e18);
    }

    // -------------------------------------------------------------------- //
    //  Manipulation-resistance: brief instantaneous spike                   //
    // -------------------------------------------------------------------- //

    function test_twap_smooths_brief_buck_spike() public {
        // Snapshot the parity state.
        int256 spotBefore = _spotBuckPrice();
        int256 twapBefore = _twapBuckPrice();
        assertApproxEqRel(uint256(spotBefore), 1e18, 0.001e18);
        assertApproxEqRel(uint256(twapBefore), 1e18, 0.001e18);

        // Flash-loan-shaped attack: drive BUCK to $0.50 (50% drop) in a
        // single block.  No warp/touch -> the latest observation is still
        // pre-attack at the parity tick, so consult() only blends a tiny
        // "extrapolate from latest using current tick" sliver of the spike.
        _moveSpotToPrice(buckUsdt, address(buck), 1e18, address(usdt), 0.50e6);

        int256 spotDuring = _spotBuckPrice();
        int256 twapDuring = _twapBuckPrice();
        emit log_named_int("Spot during attack", spotDuring);
        emit log_named_int("TWAP during attack", twapDuring);

        // Spot reflects the manipulated price (within tick rounding).
        assertApproxEqRel(uint256(spotDuring), 0.50e18, 0.01e18);

        // TWAP barely moves.  Window holds ~600s of pre-attack tick;
        // at-most a few seconds of the attack tick get blended in.
        // Tolerate 5% of parity deviation -- the actual deviation should
        // be far smaller, but the bound is what matters.
        assertGt(uint256(twapDuring), 0.95e18, "TWAP collapsed under flash attack");
    }

    // -------------------------------------------------------------------- //
    //  Sustained drift -- TWAP eventually converges                          //
    // -------------------------------------------------------------------- //

    function test_twap_lags_then_tracks_sustained_drift() public {
        // Drive BUCK to 0.95 USDT.
        _moveSpotToPrice(buckUsdt, address(buck), 1e18, address(usdt), 0.95e6);

        // Sample the TWAP at three points across one window:
        //   - immediately after the swap (TWAP ~= parity, all old history)
        //   - half-way through (TWAP ~= midway between 1.00 and 0.95)
        //   - after a full window has elapsed at 0.95 (TWAP ~= 0.95)
        //
        // Touch every 30s during the drift hold so observations get
        // written; otherwise consult would extrapolate from a single
        // stale obs and immediately match spot.

        int256 twap0 = _twapBuckPrice();
        emit log_named_int("TWAP immediately after drift starts", twap0);
        // Immediately after, almost all of the window predates the swap.
        assertApproxEqRel(uint256(twap0), 1e18, 0.02e18);

        // Hold at 0.95 for 300s (half the window) with periodic touches
        for (uint i = 0; i < 10; i++) {
            vm.warp(block.timestamp + 30);
            _touchPool(buckUsdt);
        }
        int256 twapMid = _twapBuckPrice();
        emit log_named_int("TWAP at half-window", twapMid);
        // ~half the window now covers 0.95, half covers 1.00 -> ~0.975
        assertGt(uint256(twapMid), 0.96e18);
        assertLt(uint256(twapMid), 0.99e18);

        // Hold for another 400s (more than the window) -> TWAP converges
        for (uint i = 0; i < 14; i++) {
            vm.warp(block.timestamp + 30);
            _touchPool(buckUsdt);
        }
        int256 twapFinal = _twapBuckPrice();
        emit log_named_int("TWAP after full window at 0.95", twapFinal);
        assertApproxEqRel(uint256(twapFinal), 0.95e18, 0.01e18);
    }

    // -------------------------------------------------------------------- //
    //  PID via TWAP path                                                     //
    // -------------------------------------------------------------------- //

    function test_pid_via_twap_under_sustained_drift() public {
        // Drive BUCK to 0.95 and let it sit through a full TWAP window with
        // periodic touches.  The PID should then see a real -5% error
        // (BUCK - basket) and contract buckK below 1.0.
        _moveSpotToPrice(buckUsdt, address(buck), 1e18, address(usdt), 0.95e6);
        for (uint i = 0; i < 25; i++) {
            vm.warp(block.timestamp + 30);
            _touchPool(buckUsdt);
            // Touch basket pools too so their TWAPs stay fresh, otherwise
            // their consult observation index would lag and skew the basket.
            _touchPool(xautUsdt);
            _touchPool(paxgUsdc);
            _touchPool(cbbtcUsdc);
            _touchPool(wbtcUsdt);
        }

        // Now run a couple of PID cycles via TWAP.  K must fall.
        uint256 baselineK = ctrl.buckK();
        for (uint i = 0; i < 3; i++) {
            vm.warp(block.timestamp + 61);
            _touchPool(buckUsdt);    // keep TWAP fresh between cycles
            _touchPool(xautUsdt);
            _touchPool(paxgUsdc);
            _touchPool(cbbtcUsdc);
            _touchPool(wbtcUsdt);
            ctrl.compute();
        }
        assertLt(ctrl.buckK(), baselineK, "PID did not respond to TWAP'd drift");
        emit log_named_uint("buckK after TWAP'd 5% under drift", ctrl.buckK());
    }

    // -------------------------------------------------------------------- //
    //  Helpers                                                               //
    // -------------------------------------------------------------------- //

    function _twapBasketCost() internal view returns (int256) {
        return _basketCost(true);
    }

    function _spotBasketCost() internal view returns (int256) {
        return _basketCost(false);
    }

    function _basketCost(bool useTwap) internal view returns (int256) {
        int256 total;
        for (uint i = 0; i < ctrl.basketPoolsLength(); i++) {
            (address pool, address baseT, address quoteT, uint256 weight,
             uint8 baseDec, uint8 quoteDec, uint32 twap) = ctrl.basketPools(i);
            int24 tick;
            if (useTwap) {
                tick = UniswapV3OracleLib.consult(pool, twap);
            } else {
                (, tick,,,,,) = IUniswapV3Pool(pool).slot0();
            }
            uint256 q = UniswapV3OracleLib.getQuoteAtTick(tick, uint128(10 ** baseDec), baseT, quoteT);
            int256 normalized = int256(q * 10 ** (18 - quoteDec));
            total += normalized * int256(weight) / int256(uint256(1e18));
        }
        return total;
    }

    function _twapBuckPrice() internal view returns (int256) {
        int24 tick = UniswapV3OracleLib.consult(ctrl.buckPricePool(), TWAP);
        uint256 q = UniswapV3OracleLib.getQuoteAtTick(tick, uint128(1e18), ctrl.buckToken(), ctrl.buckQuoteToken());
        return int256(q * 10 ** (18 - ctrl.buckQuoteDecimals()));
    }

    function _spotBuckPrice() internal view returns (int256) {
        (, int24 tick,,,,,) = IUniswapV3Pool(ctrl.buckPricePool()).slot0();
        uint256 q = UniswapV3OracleLib.getQuoteAtTick(tick, uint128(1e18), ctrl.buckToken(), ctrl.buckQuoteToken());
        return int256(q * 10 ** (18 - ctrl.buckQuoteDecimals()));
    }
}
