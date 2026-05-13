// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}              from "forge-std/Test.sol";
import {SimulationFixture} from "./fixtures/SimulationFixture.sol";

/// @title BuckKArbScenarioTest -- 2-year seasonal Bob/Fred/Alice/PID simulation.
/// @notice Phase (a) of the BUCK arbitrage validation.  Real PID, real Alice
///         PID-spread strategy, synthetic actors against a deep BUCK/USDT pool.
///
/// Population model: counter-cyclical archetypes.
///   - Bob   (kind=0, farmer):   mints in spring (~day 75), retires early
///                                winter (~day 300).
///   - Fred  (kind=1, builder):  mints in fall (~day 255), retires late spring
///                                of the following year (~day 480).
///   Both repeat for year 2 (means + 365 days), giving 4 seasonal waves total
///   over the 730-day run.
///
/// Pool: ~10M BUCK / 10M USDT.  Individual mint (5K-50K BUCK) is 0.05-0.5 %
/// of pool depth -- too small to shift the pool alone, but the season (50
/// actors per archetype per year, clustered in time) does shift it.  Alice's
/// 100K BUCK + 100K USDT reserve mirrors a single sophisticated arbitrageur
/// with capital ~1 % of pool size.  Alice rides each seasonal wave by trading
/// the spread between her private 5x-gain PID and the official controller.
///
/// Arrival/retire times are Gaussian-clustered around season peaks via a
/// sum-of-12-uniforms approximation; overlap between Bobs and Freds is
/// expected.
contract BuckKArbScenarioTest is Test, SimulationFixture {
    address GOV = makeAddr("governance");

    uint256 constant DURATION_DAYS = 730;
    uint256 constant TICK_SECONDS  = 1 hours;
    uint256 constant SNAP_EVERY    = 1 days;

    uint256 constant N_BOBS_PER_YEAR   = 50;
    uint256 constant N_FREDS_PER_YEAR  = 50;

    uint256 constant ACTOR_MINT_MIN    =  5_000e18;
    uint256 constant ACTOR_MINT_MAX    = 50_000e18;
    uint256 constant ACTOR_KEEP_MIN_BP =  500;   //  5 %
    uint256 constant ACTOR_KEEP_MAX_BP = 3000;   // 30 %

    uint256 constant BOB_ARRIVE_MEAN_DAY  =  75;   // spring
    uint256 constant BOB_ARRIVE_STD_DAYS  =  18;
    uint256 constant BOB_RETIRE_MEAN_DAY  = 300;   // early winter
    uint256 constant BOB_RETIRE_STD_DAYS  =  18;
    uint256 constant FRED_ARRIVE_MEAN_DAY = 255;   // fall
    uint256 constant FRED_ARRIVE_STD_DAYS =  18;
    uint256 constant FRED_RETIRE_MEAN_DAY = 480;   // y2 spring
    uint256 constant FRED_RETIRE_STD_DAYS =  18;

    uint256 constant SEED = 0xa1ceb0bbeFEED;

    uint256 internal _genSeed;

    function setUp() public {
        setUpSim(GOV);
        // Alice: 100K BUCK + 100K USDT = ~1 % of pool side.
        _seedAlice(100_000e18, 100_000e6);
        _generateActors();
    }

    function test_arbitrage_scenario_2_years() public {
        _runMainLoop();
        _drainActors();
        if (alice.arbDirection != 0) _aliceForceClose();
        _snap();
        _writeSnapshotsJson("test/vectors/arb-scenario.json");
        _logAndAssert();
    }

    function _runMainLoop() internal {
        uint256 endTime  = block.timestamp + DURATION_DAYS * 1 days;
        uint256 nextSnap = block.timestamp;
        while (block.timestamp < endTime) {
            _processArrivals();
            _processRetirements();
            _aliceTick();
            if (block.timestamp >= nextSnap) {
                _snap();
                nextSnap = block.timestamp + SNAP_EVERY;
            }
            _advance(TICK_SECONDS);
        }
        _snap();
    }

    function _processArrivals() internal {
        for (uint i = 0; i < bobs.length; i++) {
            if (bobs[i].state == 0 && bobs[i].arriveTime <= block.timestamp) {
                _bobArrive(i);
            }
        }
    }

    function _processRetirements() internal {
        for (uint i = 0; i < bobs.length; i++) {
            if (bobs[i].state == 1 && bobs[i].retireTime <= block.timestamp) {
                _bobRetire(i);
            }
        }
    }

    function _drainActors() internal {
        for (uint i = 0; i < bobs.length; i++) {
            if (bobs[i].state == 0) _bobArrive(i);
        }
        for (uint i = 0; i < bobs.length; i++) {
            if (bobs[i].state == 1) _bobRetire(i);
        }
        _snap();
    }

    function _logAndAssert() internal {
        emit log_named_uint("snapshots written",       snapshots.length);
        emit log_named_uint("Alice arb cycles",        alice.arbCount);
        emit log_named_int ("Alice realized PnL USDT", alice.realizedUsdtPnl);
        emit log_named_uint("buckK at end (1e18)",     ctrl.buckK());
        emit log_named_uint("aliceK at end (1e18)",    alicePid.buckK());

        for (uint i = 0; i < bobs.length; i++) {
            assertEq(bobs[i].state, 2, "actor did not retire");
        }
        assertGe(ctrl.buckK(), 0.50e18);
        assertLe(ctrl.buckK(), 1.50e18);
    }

    // ----- actor generation ------------------------------------------ //

    function _generateActors() internal {
        _genSeed = SEED;
        for (uint y = 0; y < 2; y++) {
            uint256 yo = y * 365;
            for (uint i = 0; i < N_BOBS_PER_YEAR; i++) {
                _spawnOne(
                    yo + BOB_ARRIVE_MEAN_DAY, BOB_ARRIVE_STD_DAYS,
                    yo + BOB_RETIRE_MEAN_DAY, BOB_RETIRE_STD_DAYS,
                    0
                );
            }
            for (uint i = 0; i < N_FREDS_PER_YEAR; i++) {
                _spawnOne(
                    yo + FRED_ARRIVE_MEAN_DAY, FRED_ARRIVE_STD_DAYS,
                    yo + FRED_RETIRE_MEAN_DAY, FRED_RETIRE_STD_DAYS,
                    1
                );
            }
        }
    }

    function _spawnOne(
        uint256 arrMeanDay, uint256 arrStdDays,
        uint256 retMeanDay, uint256 retStdDays,
        uint8   kind
    ) internal {
        uint256 mintAmount = ACTOR_MINT_MIN
            + (_advanceSeed() % (ACTOR_MINT_MAX - ACTOR_MINT_MIN + 1));
        uint256 keepBp = ACTOR_KEEP_MIN_BP
            + (_advanceSeed() % (ACTOR_KEEP_MAX_BP - ACTOR_KEEP_MIN_BP + 1));
        uint64 arrive = _sampleTs(arrMeanDay, arrStdDays);
        uint64 retire = _sampleTs(retMeanDay, retStdDays);
        if (retire <= arrive) retire = arrive + uint64(1 days);
        _addBobKind(mintAmount, keepBp, arrive, retire, kind);
    }

    function _advanceSeed() internal returns (uint256) {
        _genSeed = uint256(keccak256(abi.encode(_genSeed, "n")));
        return _genSeed;
    }

    function _sampleTs(uint256 meanDay, uint256 stdDays) internal returns (uint64) {
        int256 off = _gaussianOffset(_advanceSeed(), int256(stdDays * 1 days));
        int256 ts  = int256(uint256(_simStart)) + int256(meanDay * 1 days) + off;
        if (ts < int256(uint256(_simStart))) ts = int256(uint256(_simStart));
        return uint64(uint256(ts));
    }

    /// @dev Approximate-Gaussian by sum of 12 uniforms.  Raw sum has
    ///      std ~ 1000/sqrt(12) ~= 288.7; scaled to the requested stdSeconds.
    function _gaussianOffset(uint256 seed, int256 stdSeconds) internal pure returns (int256) {
        int256 sum = 0;
        for (uint i = 0; i < 12; i++) {
            uint256 h = uint256(keccak256(abi.encode(seed, i, "g")));
            sum += int256(h % 1000) - 500;
        }
        return (sum * stdSeconds) / 289;
    }

    // ----- Alice clean-up --------------------------------------------- //

    function _aliceForceClose() internal {
        if (alice.arbDirection == 1) {
            uint256 usdtOut = _swapExactInput(
                buckUsdt, address(buck), address(usdt), alice.arbBuckEntered);
            alice.buckReserve -= alice.arbBuckEntered;
            alice.usdtReserve += usdtOut;
            alice.realizedUsdtPnl += int256(usdtOut) - int256(alice.arbUsdtSpent);
        } else if (alice.arbDirection == 2) {
            uint256 usdtIn = _swapExactOutput(
                buckUsdt, address(usdt), address(buck), alice.arbBuckEntered);
            alice.usdtReserve -= usdtIn;
            alice.buckReserve += alice.arbBuckEntered;
            alice.realizedUsdtPnl += int256(alice.arbUsdtSpent) - int256(usdtIn);
        }
        alice.arbBuckEntered = 0;
        alice.arbUsdtSpent   = 0;
        alice.arbDirection   = 0;
        alice.arbCount      += 1;
        ctrl.compute();
        alicePid.compute();
    }
}
