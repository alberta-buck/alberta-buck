// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}              from "forge-std/Test.sol";
import {SimulationFixture} from "./fixtures/SimulationFixture.sol";

/// @title BuckKArbScenarioTest -- 30-day scripted Bob/Alice/PID simulation
/// @notice Phase (a) of the BUCK arbitrage validation: pool-only.  Real PID
///         and real Alice arb policy, but Bobs are synthetic actors that
///         transfer BUCK in/out of a dead address.  The lifecycle shape
///         matches the real-Buck flow (mint+dump on arrival, retire by
///         buying BUCK back from the pool) so the BUCK/USDT pool dynamics
///         are realistic.
///
///         The test emits per-snapshot state to test/vectors/arb-scenario.json
///         which alberta_buck/test/test_arb_plot.py turns into a four-panel
///         visualization (spot vs TWAP vs basket; buckK; Alice PnL; bob
///         population over time).
contract BuckKArbScenarioTest is Test, SimulationFixture {
    address GOV = makeAddr("governance");

    // Simulation parameters
    uint256 constant DURATION_DAYS = 30;
    uint256 constant TICK_SECONDS  = 1 hours;
    uint256 constant SNAP_EVERY    = 6 hours;
    uint256 constant N_BOBS        = 10;

    // Per-Bob random ranges
    uint256 constant BOB_MINT_MIN     = 2_000e18;     // 2,000 BUCK
    uint256 constant BOB_MINT_MAX     = 10_000e18;    // 10,000 BUCK
    uint256 constant BOB_LIFESPAN_MIN = 7 days;
    uint256 constant BOB_LIFESPAN_MAX = 22 days;
    uint256 constant BOB_ARRIVE_MIN   = 0;
    uint256 constant BOB_ARRIVE_MAX   = 8 days;
    uint256 constant BOB_KEEP_MIN_BP  = 500;          //  5 % keep
    uint256 constant BOB_KEEP_MAX_BP  = 3000;         // 30 % keep

    // PRNG seed -- determinism for the scenario.
    uint256 constant SEED = 0xa1ceb0bbe;

    function setUp() public {
        setUpSim(GOV);
        // Alice's off-LP reserves: 50K BUCK, 50K USDT.
        _seedAlice(50_000e18, 50_000e6);
        _generateBobs();
    }

    function test_arbitrage_scenario_30_days() public {
        uint256 endTime = block.timestamp + DURATION_DAYS * 1 days;
        uint256 nextSnap = block.timestamp;

        while (block.timestamp < endTime) {
            // Process due Bob arrivals.
            for (uint i = 0; i < bobs.length; i++) {
                if (bobs[i].state == 0 && bobs[i].arriveTime <= block.timestamp) {
                    _bobArrive(i);
                }
            }
            // Process due Bob retirements.
            for (uint i = 0; i < bobs.length; i++) {
                if (bobs[i].state == 1 && bobs[i].retireTime <= block.timestamp) {
                    _bobRetire(i);
                }
            }
            // Alice's arb tick.
            _aliceTick();

            // Snapshot.
            if (block.timestamp >= nextSnap) {
                _snap();
                nextSnap = block.timestamp + SNAP_EVERY;
            }

            _advance(TICK_SECONDS);
        }
        // Final snapshot.
        _snap();

        // Force any remaining Bobs to retire (graceful unwind).
        for (uint i = 0; i < bobs.length; i++) {
            if (bobs[i].state == 1) _bobRetire(i);
        }
        _snap();

        // Force-close any open Alice arb position.
        if (alice.arbDirection != 0) {
            _aliceForceClose();
        }
        _snap();

        _writeSnapshotsJson("test/vectors/arb-scenario.json");

        // Sanity assertions: Alice should be net-positive over the run, every
        // Bob should be retired, the controller should still be in-band.
        emit log_named_uint("snapshots written",      snapshots.length);
        emit log_named_uint("Alice arb cycles",       alice.arbCount);
        emit log_named_int ("Alice realized PnL USDT", alice.realizedUsdtPnl);
        emit log_named_uint("buckK at end (1e18)",     ctrl.buckK());

        for (uint i = 0; i < bobs.length; i++) {
            assertEq(bobs[i].state, 2, "bob did not retire");
        }
        // buckK should respect bounds.
        assertGe(ctrl.buckK(), 0.50e18);
        assertLe(ctrl.buckK(), 1.50e18);
    }

    // -------------------------------------------------------------------- //
    //  Bob generation                                                        //
    // -------------------------------------------------------------------- //

    function _generateBobs() internal {
        uint256 seed = SEED;
        uint64 simStart = uint64(block.timestamp);
        for (uint i = 0; i < N_BOBS; i++) {
            seed = uint256(keccak256(abi.encode(seed, i)));
            uint256 mintAmount = _rng(seed, BOB_MINT_MIN, BOB_MINT_MAX);

            seed = uint256(keccak256(abi.encode(seed, "arrive")));
            uint64 arrive = simStart + uint64(_rng(seed, BOB_ARRIVE_MIN, BOB_ARRIVE_MAX));

            seed = uint256(keccak256(abi.encode(seed, "lifespan")));
            uint64 lifespan = uint64(_rng(seed, BOB_LIFESPAN_MIN, BOB_LIFESPAN_MAX));

            seed = uint256(keccak256(abi.encode(seed, "keep")));
            uint256 keepBp = _rng(seed, BOB_KEEP_MIN_BP, BOB_KEEP_MAX_BP);

            _addBob(mintAmount, keepBp, arrive, arrive + lifespan);
        }
    }

    function _rng(uint256 r, uint256 lo, uint256 hi) internal pure returns (uint256) {
        if (hi <= lo) return lo;
        return lo + (r % (hi - lo + 1));
    }

    // -------------------------------------------------------------------- //
    //  Alice clean-up                                                        //
    // -------------------------------------------------------------------- //

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
    }
}
