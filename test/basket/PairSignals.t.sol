// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {MockERC20} from "../mocks/MockERC20.sol";
import {UniswapV3Fixture} from "../fixtures/UniswapV3Fixture.sol";
import {PairSignals} from "../../src/basket/PairSignals.sol";
import {BasketMachineWheel} from "../../src/wheel/BasketMachineWheel.sol";

// Deterministic accumulator fixture for rates, wraps, failures and cadence.
// Real-pool tests below separately verify the oracle/execution boundary.
contract PairPool {
    address public token0;
    address public token1;
    uint128 public liquidity = 1e18;
    int24 public tick;
    int56 public cumulative;
    uint256 public at;
    bool public broken;
    constructor(address a, address b) { token0 = a; token1 = b; at = block.timestamp; }
    function setBroken(bool value) external { broken = value; }
    function setLiquidity(uint128 value) external { liquidity = value; }
    function set(int24 value) external {
        unchecked { cumulative += int56(tick) * int56(uint56(block.timestamp - at)); }
        at = block.timestamp; tick = value;
    }
    function setCumulative(int56 value) external { cumulative = value; at = block.timestamp; }
    function observe(uint32[] calldata ago) external view returns (int56[] memory c, uint160[] memory l) {
        require(!broken, "unavailable");
        c = new int56[](ago.length); l = new uint160[](ago.length);
        unchecked { c[0] = cumulative + int56(tick) * int56(uint56(block.timestamp - at)); }
    }
    function slot0() external view returns (uint160, int24, uint16, uint16, uint16, uint8, bool) {
        return (1 << 96, tick, 0, 1, 1, 0, true);
    }
}

contract PairSignalsTest is Test {
    PairSignals s;
    PairPool[6] pools;
    address[] tokens;
    address[] quotes;
    uint256 time;

    function setUp() public {
        time = 1_000_000; vm.warp(time); vm.roll(10);
        tokens = new address[](3); quotes = new address[](2);
        for (uint256 i; i < 3; ++i) tokens[i] = address(new MockERC20("Token", "T", 18));
        for (uint256 q; q < 2; ++q) quotes[q] = address(new MockERC20("Quote", "Q", 6));
        address[] memory addresses = new address[](6);
        for (uint256 q; q < 2; ++q) for (uint256 i; i < 3; ++i) {
            // Exercise both pool orientations, including across quotes.
            bool forward = (i + q) % 2 == 0;
            uint256 p = q * 3 + i;
            pools[p] = new PairPool(forward ? tokens[i] : quotes[q], forward ? quotes[q] : tokens[i]);
            addresses[p] = address(pools[p]);
        }
        s = new PairSignals(tokens, quotes, addresses, 60, 120, 3600, 240, 1e12);
    }
    function setTicks(int24 a, int24 b, int24 c, int24 quoteShift) internal {
        int24[3] memory value = [a, b, c];
        for (uint256 q; q < 2; ++q) for (uint256 i; i < 3; ++i) {
            int24 v = value[i] + (q == 1 ? quoteShift : int24(0));
            pools[q * 3 + i].set((i + q) % 2 == 0 ? v : -v);
        }
    }
    function step(uint256 dt) internal {
        time += dt; vm.warp(time); vm.roll(vm.getBlockNumber() + 1); s.observe();
    }
    function warm() internal { s.observe(); for (uint256 i; i < 30; ++i) step(12); }

    function test_bootstrapIsUnreadyAndRepeatedCallsCannotInventTime() public {
        s.observe(); assertEq(s.readyMask(), 0);
        uint256 epoch = s.epoch(); s.observe(); assertEq(s.epoch(), epoch);
        step(12); (, , , uint32 count,,) = s.venues(0); assertEq(count, 1);
        step(0); (, , , uint32 afterCount,,) = s.venues(0); assertEq(afterCount, count);
        assertEq(s.readyMask(), 0);
        warm(); assertEq(s.readyMask(), 3);
    }

    function test_currencyMoveCancelsInRateAndErrorAndGraph() public {
        setTicks(100, 300, -50, 0); warm();
        for (uint256 k = 1; k < 50; ++k) {
            setTicks(100, 300, -50, int24(int256(k * 70))); step(12);
            PairSignals.Pair memory ab = s.pair(0, 1);
            assertEq(ab.rate, 0); assertEq(ab.priceError, 0); assertEq(ab.rateSpread, 0);
            assertEq(ab.levelSpread, 0);
            assertEq(ab.level + s.pair(1, 2).level, s.pair(0, 2).level);
        }
    }

    function test_rampThenCessationThenReversal() public {
        warm();
        for (uint256 k = 1; k <= 100; ++k) { setTicks(int24(int256(k * 12)), 0, 0, 0); step(12); }
        int256 outgoing = s.pair(0, 1).rate;
        assertApproxEqAbs(outgoing, 1e18, 1e12, "one tick/second");
        for (uint256 k; k < 10; ++k) step(12);
        int256 slowing = s.pair(0, 1).rate;
        assertLt(slowing, outgoing); assertGt(slowing, 0, "cessation precedes sign reversal");
        for (uint256 k = 1; k <= 60; ++k) { setTicks(1200 - int24(int256(k * 12)), 0, 0, 0); step(12); }
        assertLt(s.pair(0, 1).high, 0, "closure clears empirical allowance");
        PairSignals.Pair memory reverse = s.pair(1, 0);
        assertEq(reverse.rate, -s.pair(0, 1).rate);
        assertEq(reverse.high, -s.pair(0, 1).low);
    }

    function test_dislocatedSpotIsVisibleBeforeItEntersHistory() public {
        warm();
        pools[0].set(8000); // Same timestamp, new block: current spot, no elapsed signal.
        step(0);
        (, int24 spot,,) = s.spots(0); assertEq(spot, 8000);
        assertEq(s.pair(0, 1).rate, 0);
        step(12);
        assertGt(s.pair(0, 1).levelSpread, 0);
        assertGt(s.pair(0, 1).priceError, 0);
    }

    function test_missingRouteRecoveryNeverMixesIntervalEndpoints() public {
        warm(); pools[3].setBroken(true); step(12);
        assertEq(s.availableMask(), 1); assertEq(s.readyMask(), 1);
        setTicks(400, 0, 0, 0); step(12);
        pools[3].setBroken(false); step(12);
        assertEq(s.availableMask(), 3); assertEq(s.readyMask(), 1);
        (, , , uint32 count,,) = s.venues(1); assertEq(count, 0, "reprime full vector");
        step(12); (, , , count,,) = s.venues(1); assertEq(count, 1);
        assertEq(s.readyMask(), 1, "no zero-rate recovery vote");
        for (uint256 k; k < 30; ++k) step(12);
        assertEq(s.readyMask(), 3);
    }

    function test_emptyAndFailedVenuesPublishUnavailableInsteadOfZeroMotion() public {
        warm();
        uint64 at = s.lastSampleAt();
        pools[0].setLiquidity(0); pools[3].setBroken(true); step(12);
        assertEq(s.availableMask(), 0); assertEq(s.readyMask(), 0);
        assertEq(s.pair(0, 1).readyMask, 0, "zero-valued unavailable pair cannot be a turn vote");
        assertEq(s.lastSampleAt(), at, "failed sampling earns no new publication timestamp");
        pools[0].setLiquidity(1e18); pools[3].setBroken(false); step(12);
        assertEq(s.availableMask(), 3); assertEq(s.readyMask(), 0);
    }

    function test_staleGapReprimesAndCumulativeWrapIsValid() public {
        for (uint256 i; i < 6; ++i) pools[i].setCumulative(type(int56).max - 50);
        setTicks(100, 0, 0, 0); warm();
        assertEq(s.pair(0, 1).level, 100e18);
        step(3601); assertEq(s.readyMask(), 0);
        step(12); assertEq(s.readyMask(), 0);
    }

    function test_irregularIntervalsUseMidpoints() public {
        warm();
        // A linearly changing instantaneous tick, represented by exact interval means.
        uint256 elapsed;
        for (uint256 k; k < 100; ++k) {
            uint256 dt = k % 2 == 0 ? 12 : 36;
            setTicks(int24(int256(2 * elapsed + dt)), 0, 0, 0);
            elapsed += dt; step(dt);
        }
        assertApproxEqAbs(s.pair(0, 1).rate, 2e18, 1e10, "midpoint time yields true ramp rate");
    }

    function test_accelerationLagNeedsMoreThanOneSampleOfRateChange() public {
        warm();
        uint256 elapsed;
        for (uint256 k; k < 200; ++k) {
            // Exact mean of x(t)=t^2/200 over this interval, then tick rounding.
            uint256 nextAt = elapsed + 12;
            uint256 mean = (nextAt**3 - elapsed**3) / (600 * 12);
            setTicks(int24(int256(mean)), 0, 0, 0);
            elapsed = nextAt; step(12);
        }
        int256 truth = int256(elapsed - 6) * 1e18 / 100;
        PairSignals.Pair memory p = s.pair(0, 1);
        (uint128 priceError, uint128 acceleration) = s.errors(0);
        int256 oldHigh = p.rate + int256(uint256(s.rateFloor()) + uint256(priceError) / 60 + uint256(acceleration) * 12);
        assertGt(truth, oldHigh, "one-sample rate allowance misses deterministic lag");
        assertLe(truth, p.high, "response-time allowance covers this acceleration");
        emit log_named_int("midpoint true rate", truth);
        emit log_named_int("estimated rate", p.rate);
        emit log_named_int("allowance upper", p.high);
    }

    function test_existingWheelServicesObserverWithoutChangingLegacySource() public {
        BasketMachineWheel wheel = new BasketMachineWheel(quotes[0], quotes[1], address(this), 0, 0);
        wheel.setSample(address(s));
        (uint256 work,) = wheel.tick(1, 0); assertEq(work, 1);
        assertEq(s.epoch(), 1); (work,) = wheel.tick(1, 0); assertEq(work, 0);
        time += 12; vm.warp(time); vm.roll(vm.getBlockNumber() + 1);
        (work,) = wheel.tick(1, 0); assertEq(work, 1);
    }
}

contract PairSignalsV3Test is UniswapV3Fixture {
    PairSignals s;
    MockERC20[3] token;
    MockERC20[2] quote;
    address[6] pools;
    uint256 time;
    function setUp() public {
        time = 1_000_000; vm.warp(time); vm.roll(10); setUpV3();
        address[] memory ts = new address[](3); address[] memory qs = new address[](2);
        address[] memory ps = new address[](6);
        for (uint256 i; i < 3; ++i) {
            token[i] = new MockERC20("Token", "T", i == 1 ? 8 : 18);
            token[i].mint(address(this), 1e36); ts[i] = address(token[i]);
        }
        for (uint256 q; q < 2; ++q) {
            quote[q] = new MockERC20("Quote", "Q", q == 0 ? 6 : 18);
            quote[q].mint(address(this), 1e36); qs[q] = address(quote[q]);
            for (uint256 i; i < 3; ++i) {
                address pool = _createAndInitPool(ts[i], 10 ** token[i].decimals(), qs[q], 100 * 10 ** quote[q].decimals(), 3000);
                _mintFullRange(pool, 1e16); pools[q * 3 + i] = pool; ps[q * 3 + i] = pool;
            }
        }
        s = new PairSignals(ts, qs, ps, 60, 120, 3600, 240, 1e12);
    }
    function next(uint256 dt) internal { time += dt; vm.warp(time); vm.roll(vm.getBlockNumber() + 1); }
    function test_realPoolsDecimalsSwapIsolationAndGas() public {
        uint256 gasStart = gasleft(); s.observe(); emit log_named_uint("six-pool prime gas", gasStart - gasleft());
        for (uint256 k; k < 30; ++k) { next(12); s.observe(); }
        assertEq(s.readyMask(), 3);
        assertLt(uint256(s.pair(0, 1).level < 0 ? -s.pair(0, 1).level : s.pair(0, 1).level), 2e18, "decimal-normalized ratio within tick quantization");
        PairSignals.Pair memory before = s.pair(0, 1);
        _moveSpotToPrice(pools[0], address(token[0]), 1e18, address(quote[0]), 120e6);
        next(0); s.observe();
        assertEq(s.pair(0, 1).rate, before.rate, "own swap is not an elapsed observation");
        next(12); gasStart = gasleft(); s.observe();
        emit log_named_uint("six-pool update gas", gasStart - gasleft());
        assertGt(s.pair(0, 1).levelSpread, before.levelSpread);
        assertGt(s.pair(0, 1).priceError, 0);
    }

    function test_realOracleUint32TimestampWrap() public {
        time = uint256(type(uint32).max) - 100;
        vm.warp(time); s.observe();
        for (uint256 k; k < 30; ++k) { next(12); s.observe(); }
        assertEq(s.readyMask(), 3);
        assertEq(s.pair(0, 1).rate, 0);
        assertEq(s.pair(0, 1).priceError, 0);
    }
}
