// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {PairsRebalanceDirector} from "../../src/basket/PairsRebalanceDirector.sol";
import {RebalanceDirectorBase} from "../../src/basket/RebalanceDirectorBase.sol";

// --- Mocks -------------------------------------------------------------------- //
//
// Unlike the vrate suite (tick pinned at 0), the pairs signals RUN on the
// pool tick, so tick direction must be coherent with address ordering: the
// mock tokens (0x1000+i) sort below the MockBuck contract address, so the
// TOKEN is token0 and buckIsToken0 = false -- a positive tick means the
// TOKEN appreciating in BUCK, and the target share s shrinks as it should
// for a fixed-quantity index.  Raising a pool's tick therefore creates BOTH
// the price-ratio signal and the overweight imbalance, coherently.

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
        return (79228162514264337593543950336, tick, 0, 0, 0, 0, true);
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
                0, 0, false, 10000 / n, 0);
    }
}

// ----------------------------------------------------------------------------- //

contract PairsRebalanceDirectorTest is Test {
    uint32 constant EPOCH  = 86400;
    uint32 constant CAP    = 50;
    uint8  constant QUORUM = 3;     // windows 5/10/20 suffice in tests

    MockBuck   internal buck;
    MockBasket internal basket;
    MockPool[3] internal pools;
    PairsRebalanceDirector internal dir;

    address constant GOV = address(0xA0);

    function _params() internal pure returns (PairsRebalanceDirector.Params memory) {
        return PairsRebalanceDirector.Params({
            epochSeconds:  EPOCH,
            quorum:        QUORUM,
            kappa1e9:      500_000_000,     // kappa = 0.5
            deadband1e9:   15_000_000,      // 1.5%
            leash1e9:      300_000_000,     // 30%
            leashInner1e9: 250_000_000,     // 25%
            capBpPerEpoch: CAP,
            boundaryBp:    0                // size on |d| (toward the target)
        });
    }

    /// Same knobs, but size on the EXCESS over the deadband: the no-trade
    /// region form.  `bp` is how much of the deadband becomes the boundary.
    function _paramsBoundary(uint32 bp)
        internal pure returns (PairsRebalanceDirector.Params memory)
    {
        PairsRebalanceDirector.Params memory p = _params();
        p.boundaryBp = bp;
        return p;
    }

    function setUp() public {
        buck = new MockBuck();
        basket = new MockBasket(address(buck));
        for (uint256 i = 0; i < 3; i++) {
            pools[i] = new MockPool();
            basket.add(address(uint160(0x1000 + i)), address(pools[i]));
            buck.setBal(address(pools[i]), 100e18);
        }
        dir = new PairsRebalanceDirector(address(basket), GOV, _params());
        dir.syncConstituents();
    }

    function _warpToEpoch(uint256 e) internal {
        vm.warp(dir.genesisTime() + e * EPOCH + 1);
    }

    function _run(uint256 from, uint256 to) internal {
        for (uint256 e = from; e <= to; e++) {
            _warpToEpoch(e);
            dir.pokeAll();
        }
    }

    // --- Basics ------------------------------------------------------------- //

    function test_sync_and_firstPoke() public {
        assertEq(dir.pokeAll(), 3, "all three advanced");
        assertEq(dir.pending(), 0, "fresh after poke");
        assertEq(dir.pokeAll(), 0, "same-epoch re-poke is a no-op");
        assertEq(dir.effortOf(0), 0, "no votes possible yet");
        assertEq(dir.depositHint(), type(uint256).max, "no signal yet");
        (uint256 s, uint256 b) = dir.bestPair();
        assertEq(s, type(uint256).max);
        assertEq(b, type(uint256).max);
    }

    function test_workWheel_budget() public {
        _run(0, 0);
        _warpToEpoch(1);
        assertEq(dir.pending(), 3);
        assertEq(dir.poke(2), 2, "budget respected");
        assertEq(dir.poke(10), 1, "picks up the remainder");
        assertEq(dir.poke(10), 0, "epoch fully fresh");
    }

    // --- The amortization invariant ------------------------------------------ //
    //
    // With ticks and balances held constant across a gap, one lazy catch-up
    // poke must land every window's EMA and velocity where per-epoch pokes do.

    function test_catchup_equivalence() public {
        PairsRebalanceDirector lazy =
            new PairsRebalanceDirector(address(basket), GOV, _params());
        lazy.syncConstituents();

        pools[0].setTick(500);              // fixed imbalance before sampling
        buck.setBal(address(pools[0]), 115e18);

        dir.pokeAll();
        lazy.pokeAll();

        for (uint256 e = 1; e <= 12; e++) {
            _warpToEpoch(e);
            dir.pokeAll();
        }
        lazy.pokeAll();                     // one catch-up poke

        for (uint256 i = 0; i < 3; i++) {
            for (uint256 k = 0; k < 7; k++) {
                assertApproxEqAbs(dir.legMa(i, k), lazy.legMa(i, k), 1e3,
                    "ladder EMA: lazy == diligent");
                assertApproxEqAbs(dir.legVel(i, k), lazy.legVel(i, k), 1e3,
                    "ladder velocity: lazy == diligent");
            }
            assertEq(dir.effortOf(i), lazy.effortOf(i), "same net effort");
        }
        assertEq(dir.depositHint(), lazy.depositHint());
        assertEq(dir.redeemHint(), lazy.redeemHint());
    }

    // --- The confirmed-turn quorum -------------------------------------------- //

    function test_diverge_plateau_revert_quorum() public {
        _run(0, 24);                        // warm windows 5/10/20 at balance

        // DIVERGING: pool 0 appreciates 150 ticks/epoch for 10 epochs
        // (~+16% price => pairwise imbalance ~15%, past deadband, under leash).
        int24 t0 = 0;
        uint256 e = 25;
        for (uint256 k = 0; k < 10; k++) {
            t0 += 150;
            pools[0].setTick(t0);
            _warpToEpoch(e++);
            dir.pokeAll();
        }
        assertGt(dir.deviationOf(0), 30e15, "overweight by price");
        assertEq(dir.effortOf(0), 0, "diverging: gap opening, no votes");

        // PLATEAU: the gap holds; ladder EMAs still rising toward it, so the
        // vel criterion (gap ALREADY closing) correctly stays silent.
        for (uint256 k = 0; k < 6; k++) {
            _warpToEpoch(e++);
            dir.pokeAll();
        }
        assertEq(dir.effortOf(0), 0, "levelling is not yet confirmed turn");

        // REVERTING: price walks back; the 5/10/20-epoch gap velocities flip
        // toward equilibrium one by one; quorum fires; matched efforts appear.
        for (uint256 k = 0; k < 12; k++) {
            t0 -= 100;
            pools[0].setTick(t0);
            _warpToEpoch(e++);
            dir.pokeAll();
        }
        assertLt(dir.effortOf(0), 0, "confirmed turn: sell the rich leg");
        assertGt(dir.effortOf(1) + dir.effortOf(2), 0, "buy the poor legs");
        assertEq(dir.effortOf(0) + dir.effortOf(1) + dir.effortOf(2), 0,
            "matched pair trades: net efforts sum to zero");
        assertEq(dir.redeemHint(), 0, "draw redemptions from the rich leg");
        (uint256 sellIdx,) = dir.bestPair();
        assertEq(sellIdx, 0, "best matched trade sells leg 0");
    }

    function test_pair_leash_and_hysteresis() public {
        _run(0, 1);
        // +3000 ticks ~ +35% price: pairwise imbalance beyond the 30% leash.
        pools[0].setTick(3000);
        _warpToEpoch(2);
        dir.pokeAll();
        assertEq(dir.effortOf(0), -2 * int256(uint256(CAP)),
            "both pairs of leg 0 leashed at cap");
        assertEq(dir.effortOf(1), int256(uint256(CAP)));
        assertEq(dir.effortOf(2), int256(uint256(CAP)));

        // Inside the leash but above the inner bound: hysteresis holds.
        pools[0].setTick(2450);             // ~+27.7%
        _warpToEpoch(3);
        dir.pokeAll();
        assertEq(dir.effortOf(0), -2 * int256(uint256(CAP)), "hysteresis holds");

        // Below the inner bound: released (and back to quorum silence).
        pools[0].setTick(1800);             // ~+19.7%
        _warpToEpoch(4);
        dir.pokeAll();
        assertEq(dir.effortOf(0), 0, "released below leashInner");
    }

    // --- Work scaling ---------------------------------------------------------- //

    function test_gas_amortization() public {
        _run(0, 1);

        _warpToEpoch(2);
        uint256 g0 = gasleft();
        dir.poke(1);
        uint256 gasShort = g0 - gasleft();

        _warpToEpoch(52);                   // 50-epoch gap for the next slice
        g0 = gasleft();
        dir.poke(1);
        uint256 gasLong = g0 - gasleft();

        dir.poke(3);
        uint256 gasFresh;
        {
            uint256 g1 = gasleft();
            dir.poke(3);
            gasFresh = g1 - gasleft();
        }

        emit log_named_uint("pairs poke(1), 1-epoch gap ", gasShort);
        emit log_named_uint("pairs poke(1), 50-epoch gap", gasLong);
        emit log_named_uint("pairs poke on fresh epoch  ", gasFresh);

        assertLt(gasLong, gasShort + 30_000, "catch-up cost ~flat in gap size");
        assertLt(gasShort, 250_000, "single-slice poke stays bounded");
        assertLt(gasFresh, 30_000, "fresh-epoch guard is near-free");
    }

    // --- The no-trade boundary ------------------------------------------------ //
    //
    // Under proportional costs the optimal policy trades only to the edge of a
    // no-trade region, never to the target (Davis-Norman, Shreve-Soner).
    // `boundaryBp` selects between the two, and must do so without breaking the
    // matched-pair property that makes the engine self-financing.

    function test_boundary_sizes_on_the_excess() public {
        PairsRebalanceDirector nt =
            new PairsRebalanceDirector(address(basket), GOV,
                                       _paramsBoundary(10000));
        nt.syncConstituents();

        // Same path as the confirmed-turn test, poking both twins together.
        for (uint256 e = 0; e <= 24; e++) {
            _warpToEpoch(e);
            dir.pokeAll();
            nt.pokeAll();
        }
        int24 t0 = 0;
        uint256 ep = 25;
        for (uint256 k = 0; k < 10; k++) {
            t0 += 150;
            pools[0].setTick(t0);
            _warpToEpoch(ep++);
            dir.pokeAll();
            nt.pokeAll();
        }
        for (uint256 k = 0; k < 6; k++) {
            _warpToEpoch(ep++);
            dir.pokeAll();
            nt.pokeAll();
        }
        for (uint256 k = 0; k < 12; k++) {
            t0 -= 100;
            pools[0].setTick(t0);
            _warpToEpoch(ep++);
            dir.pokeAll();
            nt.pokeAll();
        }

        // The turn is confirmed for both: same direction, same quorum.
        assertLt(dir.effortOf(0), 0, "plain: sell the rich leg");
        assertLt(nt.effortOf(0), 0, "boundary: sell the rich leg too");

        // ... but the boundary form subtracts the deadband before sizing, so
        // it commits strictly less to the same signal.
        assertGt(nt.effortOf(0), dir.effortOf(0),
            "boundary trades less (both negative, so 'greater' is smaller)");

        // The matched-pair invariant must survive the change.
        assertEq(nt.effortOf(0) + nt.effortOf(1) + nt.effortOf(2), 0,
            "boundary form still nets to zero");
        assertEq(nt.redeemHint(), 0, "same rich leg");
    }

    function test_boundary_zero_is_the_existing_behaviour() public {
        PairsRebalanceDirector twin =
            new PairsRebalanceDirector(address(basket), GOV,
                                       _paramsBoundary(0));
        twin.syncConstituents();
        pools[0].setTick(500);
        buck.setBal(address(pools[0]), 115e18);
        for (uint256 e = 0; e <= 30; e++) {
            _warpToEpoch(e);
            dir.pokeAll();
            twin.pokeAll();
        }
        for (uint256 i = 0; i < 3; i++) {
            assertEq(twin.effortOf(i), dir.effortOf(i),
                "boundaryBp 0 is bit-identical to the default");
        }
    }

    function test_boundary_out_of_range_reverts() public {
        vm.prank(GOV);
        vm.expectRevert(RebalanceDirectorBase.BadParams.selector);
        dir.setParams(_paramsBoundary(10001));

        vm.prank(GOV);
        dir.setParams(_paramsBoundary(10000));   // the upper bound is legal
    }

    function test_params_onlyGov() public {
        PairsRebalanceDirector.Params memory p = _params();
        p.quorum = 5;
        vm.expectRevert(RebalanceDirectorBase.NotGovernance.selector);
        dir.setParams(p);
        vm.prank(GOV);
        dir.setParams(p);
        (, uint8 q,,,,,,) = dir.params();   // 8 fields since boundaryBp
        assertEq(q, 5);
    }

    function test_notSynced_guard() public {
        PairsRebalanceDirector fresh =
            new PairsRebalanceDirector(address(basket), GOV, _params());
        vm.expectRevert(RebalanceDirectorBase.NotSynced.selector);
        fresh.poke(1);
    }
}
