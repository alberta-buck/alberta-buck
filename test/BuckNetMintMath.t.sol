// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {BuckNetMintMath} from "../src/lib/BuckNetMintMath.sol";
import {BuckTypes} from "../src/BuckTypes.sol";

contract BuckNetMintMathTest is Test {
    uint256 private constant K_SCALE = 1e18;

    function callPlan(BuckNetMintMath.Snapshot memory snapshot, uint256 target)
        external pure returns (BuckNetMintMath.Plan memory)
    {
        return BuckNetMintMath.plan(snapshot, target);
    }

    function testFuzz_planMatchesAnIndependentExhaustiveSearch(
        uint8 f, uint8 a, uint8 m, uint8 p, uint8 k, uint16 r, uint8 room, uint8 target
    ) public pure {
        uint256 face = uint256(f) % 40 + 1;
        uint256 mark = uint256(m) % (face + 1);
        uint256 active = uint256(a) % (face + 1);
        uint256 issued = uint256(p) % 60;
        uint256 factor = uint256(k) * 1e16;
        uint256 rate = uint256(r) % 10_000;
        uint256 accountRoom = uint256(room) % 61;
        uint256 wanted = uint256(target) % 60 + 1;
        BuckNetMintMath.Snapshot memory s = BuckNetMintMath.Snapshot({
            face: face, activated: active, mark: mark, principal: issued,
            k: factor, effectiveRate: rate, accountRoom: accountRoom
        });

        bool found;
        uint256 expectedCoverage;
        uint256 expectedPremium;
        uint256 beforeValue = mark * active / face;
        // This reference enumerates every legal face amount instead of using
        // the planner's fixed-point inversion or its analytic lower bound.
        for (uint256 extra; extra <= face - active; ++extra) {
            uint256 present = mark * (active + extra) / face;
            uint256 premium = (present - beforeValue) * rate / 10_000;
            if (wanted + premium <= accountRoom && issued + wanted + premium <= present * factor / K_SCALE) {
                found = true;
                expectedCoverage = extra;
                expectedPremium = premium;
                break;
            }
        }

        BuckNetMintMath.Plan memory result = BuckNetMintMath.plan(s, wanted);
        if (found) {
            assertEq(uint256(result.status), uint256(BuckNetMintMath.Status.Found));
            assertEq(result.coverage, expectedCoverage);
            assertEq(result.premium, expectedPremium);
        } else {
            assertEq(uint256(result.status), uint256(BuckNetMintMath.Status.Impossible));
        }
    }

    function testFuzz_planAtMostMatchesTheBestExhaustiveCandidate(
        uint8 f, uint8 a, uint8 m, uint8 p, uint8 k, uint16 r, uint8 room, uint8 target
    ) public pure {
        uint256 face = uint256(f) % 40 + 1;
        uint256 mark = uint256(m) % (face + 1);
        uint256 active = uint256(a) % (face + 1);
        uint256 issued = uint256(p) % 60;
        uint256 factor = uint256(k) * 1e16;
        uint256 rate = uint256(r) % 10_000;
        uint256 accountRoom = uint256(room) % 61;
        uint256 requested = uint256(target) % 60 + 1;
        BuckNetMintMath.Snapshot memory s = BuckNetMintMath.Snapshot({
            face: face, activated: active, mark: mark, principal: issued,
            k: factor, effectiveRate: rate, accountRoom: accountRoom
        });

        uint256 best;
        uint256 expectedCoverage;
        uint256 expectedPremium;
        uint256 beforeValue = mark * active / face;
        for (uint256 extra; extra <= face - active; ++extra) {
            uint256 present = mark * (active + extra) / face;
            uint256 premium = (present - beforeValue) * rate / 10_000;
            uint256 cap = present * factor / K_SCALE;
            if (premium > accountRoom || cap <= issued + premium) continue;
            uint256 available = cap - issued - premium;
            available = Math.min(available, accountRoom - premium);
            available = Math.min(available, requested);
            if (available > best) {
                best = available;
                expectedCoverage = extra;
                expectedPremium = premium;
            }
        }

        (uint256 amount, BuckNetMintMath.Plan memory result) = BuckNetMintMath.planAtMost(s, requested);
        assertEq(amount, best);
        if (best == 0) {
            assertEq(uint256(result.status), uint256(BuckNetMintMath.Status.Impossible));
        } else {
            assertEq(uint256(result.status), uint256(BuckNetMintMath.Status.Found));
            assertEq(result.coverage, expectedCoverage);
            assertEq(result.premium, expectedPremium);
        }
    }

    function test_planAtMostFindsTheBestNonMonotoneCoverage() public pure {
        BuckNetMintMath.Snapshot memory s = BuckNetMintMath.Snapshot({
            face: 5, activated: 0, mark: 5, principal: 0,
            k: 0.75e18, effectiveRate: 2000, accountRoom: 5
        });
        (uint256 amount, BuckNetMintMath.Plan memory result) = BuckNetMintMath.planAtMost(s, 5);
        assertEq(uint256(result.status), uint256(BuckNetMintMath.Status.Found));
        assertEq(amount, 3);
        assertEq(result.coverage, 4);
        assertEq(result.premium, 0);
    }

    function test_lowerBoundAcceleratesNearEqualKAndPremium() public pure {
        BuckNetMintMath.Snapshot memory s = BuckNetMintMath.Snapshot({
            face: 100_000, activated: 0, mark: 100_000, principal: 0,
            k: 0.5e18, effectiveRate: 4990, accountRoom: 100_000
        });
        BuckNetMintMath.Plan memory result = BuckNetMintMath.plan(s, 100);
        assertEq(uint256(result.status), uint256(BuckNetMintMath.Status.Found));
        assertEq(result.coverage, 99_002);
        assertEq(result.premium, 49_401);
        assertLe(result.iterations, 2);
    }

    function test_workLimitIsDistinctFromImpossible() public pure {
        BuckNetMintMath.Snapshot memory s = BuckNetMintMath.Snapshot({
            face: 10_000_000_000, activated: 238_720_887, mark: 10_000_000_000, principal: 0,
            k: 39_998_000_000_000_000, effectiveRate: 400, accountRoom: 10_000_000
        });
        BuckNetMintMath.Plan memory result = BuckNetMintMath.plan(s, 9_548_359);
        assertEq(uint256(result.status), uint256(BuckNetMintMath.Status.WorkLimit));
        assertEq(result.iterations, 256);
    }

    function test_maximumValueSnapshotRemainsExact() public pure {
        uint256 max = BuckTypes.MAX_BALANCE;
        BuckNetMintMath.Snapshot memory s = BuckNetMintMath.Snapshot({
            face: max, activated: 0, mark: max, principal: 0,
            k: 1e18, effectiveRate: 1000, accountRoom: max
        });
        uint256 wanted = max / 2;
        BuckNetMintMath.Plan memory result = BuckNetMintMath.plan(s, wanted);
        assertEq(uint256(result.status), uint256(BuckNetMintMath.Status.Found));
        uint256 present = Math.mulDiv(s.mark, result.coverage, s.face);
        uint256 premium = Math.mulDiv(present, s.effectiveRate, 10_000);
        assertEq(result.premium, premium);
        assertGe(present, wanted + premium);
        assertLe(wanted + premium, max);
        uint256 previous = result.coverage - 1;
        uint256 previousPremium = Math.mulDiv(previous, s.effectiveRate, 10_000);
        assertLt(previous, wanted + previousPremium);
    }

    function test_invalidSnapshotAndZeroTargetRevert() public {
        BuckNetMintMath.Snapshot memory s = BuckNetMintMath.Snapshot({
            face: 1, activated: 2, mark: 1, principal: 0,
            k: 1e18, effectiveRate: 0, accountRoom: 1
        });
        vm.expectRevert(BuckNetMintMath.InvalidSnapshot.selector);
        this.callPlan(s, 1);
        s.activated = 1;
        vm.expectRevert(BuckNetMintMath.InvalidSnapshot.selector);
        this.callPlan(s, 0);
    }
}
