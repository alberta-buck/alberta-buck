// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BasketRebalanceDirector} from "../../src/basket/BasketRebalanceDirector.sol";

// --- Self-contained mocks ---------------------------------------------------- //
//
// The director reads three things: basket.constituents(i)/constituentsLength()/
// buck(), pool.slot0() (tick only), and buck.balanceOf(pool).  At tick 0 the
// quote is 1:1 for 18-dec tokens, so deviations are driven purely by the pool
// BUCK balances we set -- expected deviations are then exactly
// delta_i = bv_i * N / sum(bv) - 1 (equal target shares).

contract MockBuck {
    mapping(address => uint256) public balanceOf;
    function setBal(address who, uint256 b) external { balanceOf[who] = b; }
}

contract MockPool {
    int24 public tick;
    function setTick(int24 t) external { tick = t; }
    function slot0() external view
        returns (uint160, int24, uint16, uint16, uint16, uint8, bool)
    {
        return (79228162514264337593543950336, tick, 0, 0, 0, 0, true); // sqrtP(1.0)
    }
}

contract MockBasket {
    address public buck;
    address[] internal tokens_;
    address[] internal pools_;

    constructor(address _buck) { buck = _buck; }

    function add(address token, address pool) external {
        tokens_.push(token);
        pools_.push(pool);
    }

    function constituentsLength() external view returns (uint256) {
        return tokens_.length;
    }

    function constituents(uint256 i) external view returns (
        address token, uint8 decimals, uint256 basketAmount,
        uint256 initialPriceInBuck, uint24 feeTier, address pool,
        int24 tickLower, int24 tickUpper, bool buckIsToken0,
        uint256 targetWeightBp, uint128 treasuryLiquidity)
    {
        uint256 n = tokens_.length;
        return (tokens_[i], 18, 1e18 / n, 1e18, 500, pools_[i],
                0, 0, true, 10000 / n, 0);
    }
}

// ----------------------------------------------------------------------------- //

contract BasketRebalanceDirectorTest is Test {
    uint32 constant EPOCH = 86400;
    uint32 constant CAP   = 50;

    // Regime knobs: overridable via env so `make test-director-regimes` can
    // run the same suite across a window x rho parameter matrix in parallel.
    uint32 internal WINDOW;
    uint64 internal RHO1E9;

    MockBuck   internal buck;
    MockBasket internal basket;
    MockPool[3] internal pools;
    BasketRebalanceDirector internal dir;

    address constant GOV = address(0xA0);

    function _params() internal view returns (BasketRebalanceDirector.Params memory) {
        return BasketRebalanceDirector.Params({
            epochSeconds:  EPOCH,
            windowEpochs:  WINDOW,
            rho1e9:        RHO1E9,
            deadband1e9:   15_000_000,      // 1.5%
            epsFrac1e9:    250_000_000,     // 0.25
            leash1e9:      300_000_000,     // 30%
            leashInner1e9: 250_000_000,     // 25%
            capBpPerEpoch: CAP
        });
    }

    function setUp() public {
        WINDOW = uint32(vm.envOr("DIR_WINDOW", uint256(8)));
        RHO1E9 = uint64(vm.envOr("DIR_RHO1E9", uint256(3_000_000_000)));
        buck = new MockBuck();
        basket = new MockBasket(address(buck));
        for (uint256 i = 0; i < 3; i++) {
            pools[i] = new MockPool();
            basket.add(address(uint160(0x1000 + i)), address(pools[i]));
            buck.setBal(address(pools[i]), 100e18);
        }
        dir = new BasketRebalanceDirector(address(basket), GOV, _params());
        dir.syncConstituents();
    }

    function _warpToEpoch(uint256 e) internal {
        vm.warp(dir.genesisTime() + e * EPOCH + 1);
    }

    function _effort(uint256 i) internal view returns (int256) {
        return dir.effortOf(i);
    }

    /// Poke everything at each epoch in [from, to].
    function _run(uint256 from, uint256 to) internal {
        for (uint256 e = from; e <= to; e++) {
            _warpToEpoch(e);
            dir.pokeAll();
        }
    }

    // --- Basics ------------------------------------------------------------- //

    function test_sync_and_firstPoke() public {
        uint256 n = dir.pokeAll();
        assertEq(n, 3, "all three advanced");
        assertEq(dir.pending(), 0, "fresh after poke");
        assertEq(dir.pokeAll(), 0, "same-epoch re-poke is a no-op");
        // Warmup: no efforts yet regardless of balances.
        assertEq(_effort(0), 0);
        assertEq(dir.depositHint(), type(uint256).max, "no signal yet");
    }

    function test_workWheel_budget_and_cursor() public {
        _run(0, 0);
        _warpToEpoch(1);
        assertEq(dir.pending(), 3);
        assertEq(dir.poke(2), 2, "budget respected");
        assertEq(dir.pending(), 1, "one left");
        assertEq(dir.poke(10), 1, "picks up the remainder");
        assertEq(dir.poke(10), 0, "epoch fully fresh");
    }

    // --- The amortization invariant ------------------------------------------ //
    //
    // With the deviation held constant across a gap, one catch-up poke after n
    // epochs must land the signal where n per-epoch pokes land it.

    function test_catchup_equivalence() public {
        // Second director over the same mocks = the lazy twin.
        BasketRebalanceDirector lazy =
            new BasketRebalanceDirector(address(basket), GOV, _params());
        lazy.syncConstituents();

        // Imbalance fixed before anyone samples: [130, 90, 90].
        buck.setBal(address(pools[0]), 130e18);
        buck.setBal(address(pools[1]), 90e18);
        buck.setBal(address(pools[2]), 90e18);

        dir.pokeAll();
        lazy.pokeAll();                     // identical epoch-0 init

        for (uint256 e = 1; e <= 12; e++) { // diligent twin: every epoch
            _warpToEpoch(e);
            dir.pokeAll();
        }
        lazy.pokeAll();                     // lazy twin: one catch-up poke

        for (uint256 i = 0; i < 3; i++) {
            (int128 mD, int128 vD, int128 ddD,,,,,,,) = dir.signalOf(i);
            (int128 mL, int128 vL, int128 ddL,,,,,,,) = lazy.signalOf(i);
            assertApproxEqAbs(mD, mL, 1e9,
                "EMA catch-up == per-epoch pokes (to rounding)");
            assertApproxEqAbs(vD, vL, 1e9, "velocity closed form exact");
            assertApproxEqAbs(ddD, ddL, 1e9, "closure-rate closed form exact");
            assertEq(dir.effortOf(i), lazy.effortOf(i), "same advisory effort");
        }
        assertEq(dir.depositHint(), lazy.depositHint(), "same deposit hint");
        assertEq(dir.redeemHint(), lazy.redeemHint(), "same redeem hint");
    }

    // --- Regimes: quench while receding, act on level/gaining ----------------- //

    function test_regimes_quench_level_gain_signAgreement() public {
        _run(0, WINDOW + 2);                        // warm up balanced

        // RECEDING: pool 0 grows 3%/epoch for 6 epochs.  Deviation exceeds the
        // deadband but the MA is still moving away from target: quenched.
        uint256 b0 = 100e18;
        uint256 e = WINDOW + 3;
        for (uint256 k = 0; k < 6; k++) {
            b0 = b0 * 103 / 100;
            buck.setBal(address(pools[0]), b0);
            _warpToEpoch(e++);
            dir.pokeAll();
        }
        assertGt(dir.deviationOf(0), 15e15, "well past the deadband");
        assertEq(_effort(0), 0, "receding: rebalancing quenched");

        // LEVEL: hold the excursion.  The MA converges (v -> 0); the level
        // regime opens with the half-gap-in-a-window starter effort.
        for (uint256 k = 0; k < 2 * WINDOW; k++) {
            _warpToEpoch(e++);
            dir.pokeAll();
        }
        int256 levelEffort = _effort(0);
        assertLt(levelEffort, 0, "level: start selling the overweight");
        assertEq(dir.redeemHint(), 0, "overweight pool is the redeem source");

        // GAINING: the excursion reverts 3%/epoch.  Observed closure rate
        // takes over the sizing; with this geometry it saturates the cap.
        for (uint256 k = 0; k < 4; k++) {
            b0 = b0 * 97 / 100;
            buck.setBal(address(pools[0]), b0);
            _warpToEpoch(e++);
            dir.pokeAll();
        }
        assertLe(_effort(0), levelEffort,
            "gaining: effort at least the level starter");
        assertEq(_effort(0), -int256(uint256(CAP)),
            "rate-matched sizing saturates the cap here");

        // SIGN AGREEMENT: crash pool 0 straight through target to underweight
        // while the MA is still positive -- the stale MA must not trade.
        buck.setBal(address(pools[0]), 80e18);
        _warpToEpoch(e++);
        dir.pokeAll();
        assertLt(dir.deviationOf(0), -15e15, "now raw-underweight");
        (int128 m,,,,,,,,,) = dir.signalOf(0);
        assertGt(m, 0, "MA still remembers the overweight");
        assertEq(_effort(0), 0, "sign disagreement: no wrong-way trade");
    }

    function test_leash_overrides_quench_with_hysteresis() public {
        _run(0, 1);
        // Blow pool 0 out to ~+48% deviation immediately (no warmup needed --
        // the leash enforces the mandate regardless of signal state).
        buck.setBal(address(pools[0]), 170e18);
        _warpToEpoch(2);
        dir.pokeAll();
        assertEq(_effort(0), -int256(uint256(CAP)), "leashed: sell at cap");

        // Inside leash but above the inner release: still engaged.
        // delta = 3*b0/(b0+200) - 1 = +28.6% at b0 = 150.
        buck.setBal(address(pools[0]), 150e18);
        _warpToEpoch(3);
        dir.pokeAll();
        assertEq(_effort(0), -int256(uint256(CAP)), "hysteresis holds");

        // Below the inner bound: released (and back in warmup silence).
        buck.setBal(address(pools[0]), 110e18);     // ~+16%
        _warpToEpoch(4);
        dir.pokeAll();
        assertEq(_effort(0), 0, "released below leashInner");
    }

    function test_hints_track_both_sides() public {
        _run(0, 1);
        // Pool 0 heavily overweight, pool 2 heavily underweight -> both leash.
        buck.setBal(address(pools[0]), 160e18);
        buck.setBal(address(pools[2]), 55e18);
        _warpToEpoch(2);
        dir.pokeAll();
        assertEq(dir.redeemHint(), 0, "draw redemptions from the overweight");
        assertEq(dir.depositHint(), 2, "route deposits to the underweight");

        int256[] memory all = dir.effortsAll();
        assertLt(all[0], 0);
        assertGt(all[2], 0);
    }

    // --- Work scaling ---------------------------------------------------------- //

    function test_gas_amortization() public {
        _run(0, 1);                         // storage + cursor warmed

        _warpToEpoch(2);                    // 1-epoch gap
        uint256 g0 = gasleft();
        dir.poke(1);
        uint256 gasShort = g0 - gasleft();

        _warpToEpoch(52);                   // 50-epoch gap for the next slice
        g0 = gasleft();
        dir.poke(1);
        uint256 gasLong = g0 - gasleft();

        g0 = gasleft();
        dir.poke(3);
        dir.poke(3);                        // second call: all fresh, guard-only
        uint256 gasFresh;
        {
            uint256 g1 = gasleft();
            dir.poke(3);
            gasFresh = g1 - gasleft();
        }

        emit log_named_uint("poke(1), 1-epoch gap ", gasShort);
        emit log_named_uint("poke(1), 50-epoch gap", gasLong);
        emit log_named_uint("poke on fresh epoch  ", gasFresh);

        // The amortization claim: catching up 50 quiet epochs is one
        // closed-form update (O(log n) binexp), not 50 updates.
        assertLt(gasLong, gasShort + 20_000, "catch-up cost ~flat in gap size");
        assertLt(gasShort, 120_000, "single-constituent slice stays cheap");
        assertLt(gasFresh, 30_000, "fresh-epoch guard is near-free");
    }

    function test_epoch_zero_guard() public {
        // Nothing synced -> poke reverts; sync of an empty basket also guards.
        BasketRebalanceDirector fresh =
            new BasketRebalanceDirector(address(basket), GOV, _params());
        vm.expectRevert(BasketRebalanceDirector.NotSynced.selector);
        fresh.poke(1);
    }

    /// Fuzzed amortization invariant: for ANY gap length and ANY (constant)
    /// pool imbalance, one lazy catch-up poke must land the EMA where
    /// per-epoch diligent pokes land it (to fixed-point rounding).
    function testFuzz_catchup_m_invariant(uint32 gap, uint96 a, uint96 b, uint96 c)
        public
    {
        gap = uint32(bound(gap, 1, 90));
        buck.setBal(address(pools[0]), bound(uint256(a), 10e18, 1_000_000e18));
        buck.setBal(address(pools[1]), bound(uint256(b), 10e18, 1_000_000e18));
        buck.setBal(address(pools[2]), bound(uint256(c), 10e18, 1_000_000e18));

        BasketRebalanceDirector lazy =
            new BasketRebalanceDirector(address(basket), GOV, _params());
        lazy.syncConstituents();

        dir.pokeAll();
        lazy.pokeAll();
        for (uint256 e = 1; e <= gap; e++) {
            _warpToEpoch(e);
            dir.pokeAll();
        }
        lazy.pokeAll();

        for (uint256 i = 0; i < 3; i++) {
            (int128 mD,,, int128 pdD,,,,,,) = dir.signalOf(i);
            (int128 mL,,, int128 pdL,,,,,,) = lazy.signalOf(i);
            assertApproxEqAbs(mD, mL, 1e12, "m: lazy == diligent");
            assertEq(pdD, pdL, "prevDelta identical");
        }
    }

    function test_params_onlyGov() public {
        BasketRebalanceDirector.Params memory p = _params();
        p.capBpPerEpoch = 10;
        vm.expectRevert(BasketRebalanceDirector.NotGovernance.selector);
        dir.setParams(p);
        vm.prank(GOV);
        dir.setParams(p);
        (,,,,,,, uint32 cap) = dir.params();
        assertEq(cap, 10);
    }
}
