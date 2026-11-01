// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckTypes} from "../src/BuckTypes.sol";
import {BuckNetBurnMath} from "../src/lib/BuckNetBurnMath.sol";

contract BuckNetBurnMathTest is Test {
    uint256 constant Q = 1e36;

    function testFuzz_candidateMatchesIndependentExhaustiveSearch(uint64 seed) public pure {
        uint256 r = uint256(keccak256(abi.encode(seed)));
        uint256 principal = 1 + r % 40;
        uint256 seconds_ = principal * (1 + (r >> 8) % 100) + (r >> 16) % principal;
        uint256 gross = ((r >> 24) % 41) * Q + ((r >> 32) % 10) * Q / 10;
        BuckNetBurnMath.Snapshot memory s = BuckNetBurnMath.Snapshot({
            principal: principal,
            issuedSeconds: seconds_,
            totalSeconds: seconds_ + (r >> 40) % 200,
            fundValueScaled: ((r >> 48) % 61) * Q + ((r >> 56) % 10) * Q / 10,
            reliefLimitScaled: ((r >> 64) % 41) * Q + ((r >> 72) % 10) * Q / 10,
            premiumPrincipal: (r >> 80) % (principal + 1),
            poolGrossScaled: gross,
            poolNetScaled: gross * ((r >> 88) % 101) / 100,
            poolAvailableNominal: (r >> 96) % 41,
            walletScaled: ((r >> 104) % 51) * Q + ((r >> 112) % 10) * Q / 10
        });
        uint256 target = 1 + (r >> 120) % 45;
        uint256 face = 1 + (r >> 128) % 40;
        BuckNetBurnMath.Coverage memory c = BuckNetBurnMath.Coverage({
            face: face, active: (r >> 136) % (face + 1), mark: (r >> 144) % (face + 1),
            k: ((r >> 152) % 151) * 1e16
        });
        uint256 capacityBefore = (c.mark * c.active / c.face) * c.k / 1e18;
        uint256 oldShortfall = principal > capacityBefore ? principal - capacityBefore : 0;
        uint256 expected;
        uint256 expectedRelief;
        uint256 expectedRefund;
        uint256 expectedRefundNet;
        uint256 bestTarget;
        uint256 bestPrincipal;
        // Independent integer enumeration: this deliberately does not use the
        // planner's fixed point or its algebraic lower bound.
        for (uint256 p = 1; p <= principal; ++p) {
            uint256 refund = s.premiumPrincipal * p / principal;
            if (refund > s.poolAvailableNominal || refund * Q > s.poolGrossScaled) continue;
            uint256 refundNet = refund == 0 ? 0 : s.poolNetScaled * refund * Q / s.poolGrossScaled;
            uint256 selected = s.issuedSeconds * p / principal;
            uint256 proportional = s.fundValueScaled * selected / s.totalSeconds;
            uint256 relief = proportional < p * Q ? proportional : p * Q;
            if (relief > s.reliefLimitScaled || p * Q > s.walletScaled + refundNet + relief) continue;
            uint256 faceReduction = c.active * p / principal;
            uint256 capacityAfter = (c.mark * (c.active - faceReduction) / c.face) * c.k / 1e18;
            uint256 left = principal - p;
            uint256 newShortfall = left > capacityAfter ? left - capacityAfter : 0;
            if (newShortfall > oldShortfall) continue;
            uint256 afterScaled = s.walletScaled + refundNet + relief - p * Q;
            if (afterScaled / Q > s.walletScaled / Q) continue;
            uint256 decrease = s.walletScaled / Q - afterScaled / Q;
            uint256 candidateTarget = decrease < target ? decrease : target;
            if (candidateTarget > bestTarget) {
                bestTarget = candidateTarget;
                bestPrincipal = p;
            }
            if (decrease >= target && expected == 0) {
                expected = p;
                expectedRelief = relief;
                expectedRefund = refund;
                expectedRefundNet = refundNet;
            }
        }
        BuckNetBurnMath.Plan memory actual = BuckNetBurnMath.plan(s, c, target);
        if (expected == 0) {
            assertEq(uint256(actual.status), uint256(BuckNetBurnMath.Status.Impossible));
        } else {
            assertEq(uint256(actual.status), uint256(BuckNetBurnMath.Status.Found));
            assertEq(actual.principal, expected);
            assertEq(actual.reliefScaled, expectedRelief);
            assertEq(actual.refundNominal, expectedRefund);
            assertEq(actual.refundNetScaled, expectedRefundNet);
            assertEq(actual.coverageReduction, c.active * expected / principal);
        }
        (uint256 actualTarget, BuckNetBurnMath.Plan memory best) = BuckNetBurnMath.planAtMost(s, c, target);
        assertEq(actualTarget, bestTarget);
        if (bestTarget == 0) {
            assertEq(uint256(best.status), uint256(BuckNetBurnMath.Status.Impossible));
        } else {
            assertEq(uint256(best.status), uint256(BuckNetBurnMath.Status.Found));
            assertEq(best.principal, bestPrincipal);
        }
    }

    function test_roundingCanMakeALargerReductionLessEffective() public pure {
        // The balance and relief are a compact integer analogue of the actual
        // token/fund/pool counterexample in BuckNetBurnRoundingTest.
        BuckNetBurnMath.Snapshot memory s = BuckNetBurnMath.Snapshot({
            principal: 100, issuedSeconds: 100, totalSeconds: 100,
            fundValueScaled: 13 * Q / 10, reliefLimitScaled: 10 * Q,
            premiumPrincipal: 20, poolGrossScaled: 20 * Q, poolNetScaled: 20 * Q,
            poolAvailableNominal: 20, walletScaled: 78 * Q + 94 * Q / 100
        });
        BuckNetBurnMath.Plan memory plan = BuckNetBurnMath.plan(s, _noCoverage(), 4);
        assertEq(uint256(plan.status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(plan.principal, 4);
        assertEq(plan.refundNominal, 0);
    }

    function test_searchLimitIsNotReportedAsImpossible() public pure {
        BuckNetBurnMath.Snapshot memory s = BuckNetBurnMath.Snapshot({
            principal: 1_000_000, issuedSeconds: 1_000_000, totalSeconds: 1_000_000,
            fundValueScaled: 1_000_000 * (Q - Q / 10_000), reliefLimitScaled: 100_000 * Q,
            premiumPrincipal: 0, poolGrossScaled: 0, poolNetScaled: 0,
            poolAvailableNominal: 0, walletScaled: Q + Q / 2
        });
        BuckNetBurnMath.Plan memory plan = BuckNetBurnMath.plan(s, _noCoverage(), 1);
        assertEq(uint256(plan.status), uint256(BuckNetBurnMath.Status.WorkLimit));
        assertEq(plan.iterations, 256);
    }

    function test_coverageSearchLimitDoesNotPretendFullRetirementIsImpossible() public pure {
        BuckNetBurnMath.Snapshot memory s = BuckNetBurnMath.Snapshot({
            principal: 3438, issuedSeconds: 0, totalSeconds: 0, fundValueScaled: 0, reliefLimitScaled: 0,
            premiumPrincipal: 0, poolGrossScaled: 0, poolNetScaled: 0,
            poolAvailableNominal: 0, walletScaled: 3438 * Q
        });
        BuckNetBurnMath.Coverage memory c = BuckNetBurnMath.Coverage({
            face: 138648, active: 38527, mark: 24759, k: 0.4999e18
        });
        BuckNetBurnMath.Plan memory limited = BuckNetBurnMath.plan(s, c, 2779);
        assertEq(uint256(limited.status), uint256(BuckNetBurnMath.Status.WorkLimit));
        assertEq(limited.iterations, 256);
        (uint256 target, BuckNetBurnMath.Plan memory bounded) = BuckNetBurnMath.planAtMost(s, c, 2779);
        assertEq(target, 0);
        assertEq(uint256(bounded.status), uint256(BuckNetBurnMath.Status.WorkLimit));
        BuckNetBurnMath.Plan memory full = BuckNetBurnMath.plan(s, c, 3438);
        assertEq(uint256(full.status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(full.principal, 3438);
        assertEq(full.coverageReduction, c.active);
    }

    function test_liquidityLimitBlocksInsteadOfClippingRelief() public pure {
        BuckNetBurnMath.Snapshot memory s = BuckNetBurnMath.Snapshot({
            principal: 100, issuedSeconds: 100, totalSeconds: 100,
            fundValueScaled: 10 * Q, reliefLimitScaled: 3 * Q / 10,
            premiumPrincipal: 0, poolGrossScaled: 0, poolNetScaled: 0,
            poolAvailableNominal: 0, walletScaled: 100 * Q
        });
        BuckNetBurnMath.Plan memory three = BuckNetBurnMath.plan(s, _noCoverage(), 3);
        assertEq(uint256(three.status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(three.principal, 3);
        assertEq(three.reliefScaled, 3 * Q / 10);
        assertEq(uint256(BuckNetBurnMath.plan(s, _noCoverage(), 4).status), uint256(BuckNetBurnMath.Status.Impossible));
    }

    function test_maximumTargetIncludesTheOriginalInsurerRefund() public pure {
        BuckNetBurnMath.Snapshot memory s = BuckNetBurnMath.Snapshot({
            principal: 100, issuedSeconds: 0, totalSeconds: 0, fundValueScaled: 0, reliefLimitScaled: 0,
            premiumPrincipal: 20, poolGrossScaled: 20 * Q, poolNetScaled: 20 * Q,
            poolAvailableNominal: 20, walletScaled: 100 * Q
        });
        (uint256 target, BuckNetBurnMath.Plan memory plan) =
            BuckNetBurnMath.planAtMost(s, _noCoverage(), type(uint256).max);
        assertEq(uint256(plan.status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(target, 80);
        assertEq(plan.principal, 99);
        assertEq(plan.refundNominal, 19);
    }

    function test_accountMustHaveRoomForItsRefundBeforeSettlement() public pure {
        uint256 max = BuckTypes.MAX_BALANCE;
        BuckNetBurnMath.Snapshot memory s = BuckNetBurnMath.Snapshot({
            principal: 100, issuedSeconds: 0, totalSeconds: 0,
            fundValueScaled: 0, reliefLimitScaled: 0,
            premiumPrincipal: 20, poolGrossScaled: 20 * Q, poolNetScaled: 20 * Q,
            poolAvailableNominal: 20, walletScaled: max * Q
        });
        assertEq(uint256(BuckNetBurnMath.plan(s, _noCoverage(), 5).status), uint256(BuckNetBurnMath.Status.Impossible));
        s.premiumPrincipal = 0;
        BuckNetBurnMath.Plan memory plan = BuckNetBurnMath.plan(s, _noCoverage(), 5);
        assertEq(uint256(plan.status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(plan.principal, 5);
    }

    function test_zeroAndInvalidSnapshots() public {
        BuckNetBurnMath.Snapshot memory s;
        assertEq(uint256(BuckNetBurnMath.plan(s, _noCoverage(), 0).status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(uint256(BuckNetBurnMath.plan(s, _noCoverage(), 1).status), uint256(BuckNetBurnMath.Status.Impossible));
        (uint256 none, BuckNetBurnMath.Plan memory zero) = BuckNetBurnMath.planAtMost(s, _noCoverage(), 0);
        assertEq(none, 0);
        assertEq(uint256(zero.status), uint256(BuckNetBurnMath.Status.Found));
        s.principal = BuckTypes.MAX_BALANCE + 1;
        vm.expectRevert(BuckNetBurnMath.InvalidSnapshot.selector);
        this.callPlan(s, 0);
        s.principal = 1;
        s.poolGrossScaled = Q;
        s.poolNetScaled = Q + 1;
        vm.expectRevert(BuckNetBurnMath.InvalidSnapshot.selector);
        this.callPlan(s, 1);
    }

    function callPlan(BuckNetBurnMath.Snapshot calldata s, uint256 target)
        external pure returns (BuckNetBurnMath.Plan memory)
    {
        return BuckNetBurnMath.plan(s, _noCoverage(), target);
    }

    function _noCoverage() private pure returns (BuckNetBurnMath.Coverage memory) {
        return BuckNetBurnMath.Coverage({face: 0, active: 0, mark: 0, k: 0});
    }
}
