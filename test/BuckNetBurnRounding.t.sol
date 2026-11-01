// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {JubileeFundTest, JubileeInsurerPoolMock} from "./JubileeFund.t.sol";
import {BuckNetBurnMath} from "../src/lib/BuckNetBurnMath.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckNet} from "../src/BuckNet.sol";

/// @dev A burn allocator must use the complete relief and refund arithmetic,
///      not assume a larger principal always makes balanceOf fall further.
contract BuckNetBurnRoundingTest is JubileeFundTest {
    function test_largerPrincipalCanProduceASmallerVisibleBalanceDecrease() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 alice = _credit(ALICE, 100, 200); // 100 issued, 20 deposited, 80 in wallet
        _credit(BOB, 1_000, 0);
        vm.warp(t0 + 242 days);

        // The insurer invests its aged cash and replaces it with a full-value
        // transfer. Its next nominal refund therefore carries no older fees.
        pool.invest(20);
        vm.prank(BOB);
        buck.transfer(address(pool), 20);
        assertEq(buck.balanceOf(address(pool)), 20);
        uint256 beforeBalance = buck.balanceOf(ALICE);
        BuckNetBurnMath.Snapshot memory candidate = _candidate(alice, pool);
        BuckNetBurnMath.Coverage memory c = _coverage(alice);
        BuckNetBurnMath.Plan memory four = BuckNetBurnMath.plan(candidate, c, 4);
        BuckNetBurnMath.Plan memory five = BuckNetBurnMath.plan(candidate, c, 5);
        assertEq(uint256(four.status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(uint256(five.status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(four.principal, 4);
        assertEq(five.principal, 7);
        uint256 snapshot = vm.snapshotState();

        _authorize(ALICE, alice, 4, 0);
        vm.prank(ALICE);
        (uint256 firstRelief, uint256 firstRefund) = fund.reduce(alice, 4);
        uint256 firstDecrease = beforeBalance - buck.balanceOf(ALICE);
        assertEq(firstRelief, four.reliefScaled);
        assertEq(firstRefund, four.refundNominal);
        assertTrue(vm.revertToState(snapshot));
        snapshot = vm.snapshotState();

        _authorize(ALICE, alice, 5, 0);
        vm.prank(ALICE);
        (uint256 secondRelief, uint256 secondRefund) = fund.reduce(alice, 5);
        uint256 secondDecrease = beforeBalance - buck.balanceOf(ALICE);

        assertGt(secondRelief, firstRelief);
        assertEq(firstRefund, 0);
        assertEq(secondRefund, 1);
        assertEq(firstDecrease, 4);
        assertEq(secondDecrease, 3);
        assertTrue(vm.revertToState(snapshot));

        _authorize(ALICE, alice, five.principal, 0);
        vm.prank(ALICE);
        (uint256 thirdRelief, uint256 thirdRefund) = fund.reduce(alice, five.principal);
        assertEq(beforeBalance - buck.balanceOf(ALICE), 5);
        assertEq(thirdRelief, five.reliefScaled);
        assertEq(thirdRefund, five.refundNominal);
    }

    function test_candidateAccountsForTheInsurerPoolsCarriedFees() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 alice = _credit(ALICE, 1_000_000e6, 100);
        _credit(BOB, 1_000_000e6, 0);
        vm.warp(t0 + 3 * YEAR);
        BuckNetBurnMath.Snapshot memory candidate = _candidate(alice, pool);
        uint256 beforeBalance = buck.balanceOf(ALICE);
        BuckNetBurnMath.Plan memory plan = BuckNetBurnMath.plan(candidate, _coverage(alice), 100e6);
        assertEq(uint256(plan.status), uint256(BuckNetBurnMath.Status.Found));
        assertGt(plan.refundNominal, 0);
        assertLt(plan.refundNetScaled, plan.refundNominal * Q);
        _authorize(ALICE, alice, plan.principal, 0);
        vm.prank(ALICE);
        (uint256 relief, uint256 refund) = fund.reduce(alice, plan.principal);
        assertEq(beforeBalance - buck.balanceOf(ALICE), 100e6);
        assertEq(relief, plan.reliefScaled);
        assertEq(refund, plan.refundNominal);
    }

    function test_candidateSkipsAProportionalCoverageShortfall() public {
        JubileeInsurerPoolMock pool = _pool();
        _credit(BOB, 1_000, 0);
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 4, 0, BuckCredit.DepreciationType.LINEAR,
                                         2500, uint48(t0), 0);
        vm.warp(t0 + YEAR);
        k.setK(0.7e18);
        vm.prank(ALICE);
        buck.issue(id, 4, 2, 0, address(0));
        assertEq(credit.depreciatedFaceValue(id), 3);
        BuckNetBurnMath.Snapshot memory candidate = _candidate(id, pool);
        BuckNetBurnMath.Plan memory one = BuckNetBurnMath.plan(candidate, _coverage(id), 1);
        assertEq(uint256(one.status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(one.principal, 2);
        _authorize(ALICE, id, 1, 0);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        fund.reduce(id, 1); // proportional face reduction would create a K shortfall
        assertEq(buck.balanceOf(ALICE), 2);
        (uint256 stillIssued,) = credit.issuanceOf(id);
        assertEq(stillIssued, 2);
        _authorize(ALICE, id, one.principal, 0);
        vm.prank(ALICE);
        fund.reduce(id, one.principal); // full retirement is possible
        assertEq(buck.balanceOf(ALICE), 0);
    }

    function test_coverageEligibilityCanAlsoBeNonMonotone() public {
        JubileeInsurerPoolMock pool = _pool();
        _credit(BOB, 1_000, 0);
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 5, 0, BuckCredit.DepreciationType.LINEAR,
                                         2000, uint48(t0), 0);
        vm.warp(t0 + YEAR);
        k.setK(0.8e18);
        vm.prank(ALICE);
        buck.issue(id, 5, 3, 0, address(0));
        assertEq(credit.depreciatedFaceValue(id), 4);
        BuckNetBurnMath.Plan memory two = BuckNetBurnMath.plan(_candidate(id, pool), _coverage(id), 2);
        assertEq(uint256(two.status), uint256(BuckNetBurnMath.Status.Found));
        assertEq(two.principal, 3);
        uint256 snapshot = vm.snapshotState();
        _authorize(ALICE, id, 1, 0);
        vm.prank(ALICE);
        fund.reduce(id, 1); // 1 is permitted
        assertTrue(vm.revertToState(snapshot));
        _authorize(ALICE, id, 2, 0);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        fund.reduce(id, 2); // 2 would create a K shortfall
        _authorize(ALICE, id, two.principal, 0);
        vm.prank(ALICE);
        fund.reduce(id, two.principal); // 3 is permitted
        assertEq(buck.balanceOf(ALICE), 0);
    }

    function _coverage(uint256 id) internal view returns (BuckNetBurnMath.Coverage memory) {
        (uint256 face, uint256 active,) = credit.creditInfo(id);
        return BuckNetBurnMath.Coverage({face: face, active: active,
                                         mark: credit.depreciatedFaceValue(id), k: k.currentBuckK()});
    }

    function _candidate(uint256 id, JubileeInsurerPoolMock pool)
        internal view returns (BuckNetBurnMath.Snapshot memory candidate)
    {
        (uint256 issued, uint256 seconds_) = credit.issuanceOf(id);
        (, uint256 totalSeconds) = buck.selectedClaimSeconds(id, 1);
        (, uint256 cash) = buck.fundBalancesScaled();
        (uint256 poolGross, uint256 poolNet) = buck.accountBalancesScaled(address(pool));
        (uint256 held, uint256 unspent) = buck.heldAndUnspentScaled(ALICE);
        return BuckNetBurnMath.Snapshot({
            principal: issued, issuedSeconds: seconds_, totalSeconds: totalSeconds,
            fundValueScaled: cash, reliefLimitScaled: buck.availableReliefScaled(),
            premiumPrincipal: buck.premiumPrincipal(id), poolGrossScaled: poolGross,
            poolNetScaled: poolNet, poolAvailableNominal: buck.allowance(address(pool), address(buck)),
            walletScaled: held + unspent
        });
    }
}
