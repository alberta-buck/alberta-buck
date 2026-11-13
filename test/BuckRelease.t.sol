// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Vm} from "forge-std/Vm.sol";
import {JubileeFund, JubileeFundFixture, JubileeInsurerPoolMock} from "./JubileeFund.t.sol";
import {TestWheel} from "./wheel/WorkWheel.t.sol";

/// @dev burn(0) releases what it can: every selected facility's coverage that
///      no principal needs at the current K, with the premium deposit refunded
///      in the same proportion. It retires no principal and creates no shortfall.
contract BuckReleaseTest is JubileeFundFixture {
    bytes32 constant RELEASED = keccak256("FacilityReleased(address,uint256,uint256,uint256)");

    function _facility(address owner, uint256 face, uint32 rate) internal returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(owner, 0, face, face, BuckCredit.DepreciationType.NONE, 0, 0, rate);
    }
    function _active(uint256 id) internal view returns (uint256 active) { (, active,) = credit.creditInfo(id); }
    function _principal(uint256 id) internal view returns (uint256 principal) { (principal,) = credit.issuanceOf(id); }
    function _one(uint256 id) internal pure returns (uint256[] memory ids) { ids = new uint256[](1); ids[0] = id; }
    function _quote(address owner, uint256[] memory ids) internal returns (uint256 coverage, uint256 refund) {
        vm.prank(owner);
        return buck.quoteBurn(0, ids);
    }
    /// @dev Release as `owner`, returning the events' totals and their count.
    function _release(address owner) internal returns (uint256 coverage, uint256 refund, uint256 count) {
        vm.recordLogs();
        vm.prank(owner);
        buck.burn(0);
        return _released();
    }
    function _released() internal returns (uint256 coverage, uint256 refund, uint256 count) {
        Vm.Log[] memory logs = vm.getRecordedLogs();
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter != address(buck) || logs[i].topics[0] != RELEASED) continue;
            (uint256 c, uint256 r) = abi.decode(logs[i].data, (uint256, uint256));
            coverage += c; refund += r; ++count;
        }
    }

    function test_releaseFreesCoverageNothingWasDrawnAgainst() public {
        uint256 id = _facility(ALICE, 100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 0, 0, address(0));
        assertEq(_active(id), 100e6);
        assertEq(_principal(id), 0);
        (uint256 quoted, uint256 quotedRefund) = _quote(ALICE, _one(id));
        assertEq(quoted, 100e6);
        assertEq(quotedRefund, 0);
        (uint256 coverage, uint256 refund, uint256 count) = _release(ALICE);
        assertEq(coverage, 100e6);
        assertEq(refund, 0);
        assertEq(count, 1);
        assertEq(_active(id), 0);
        assertEq(buck.balanceOf(ALICE), 0);
        assertEq(buck.totalIssued(), 0);
        (, , count) = _release(ALICE);        // nothing is left to release
        assertEq(count, 0);
    }

    function test_releaseRefundsTheDepositInProportionAndALaterBurnClosesTheRest() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 id = _facility(ALICE, 1000e6, 100);
        vm.prank(ALICE);
        buck.issue(id, 1000e6, 0, type(uint256).max, address(pool));
        assertEq(_principal(id), 100e6);                // the premium deposit is real principal
        assertEq(buck.balanceOf(ALICE), 0);
        uint256 beforeSeconds = buck.claimSeconds();
        (uint256 quoted, uint256 quotedRefund) = _quote(ALICE, _one(id));
        (uint256 coverage, uint256 refund, uint256 count) = _release(ALICE);
        assertEq(count, 1);
        assertEq(coverage, 900e6);                      // 100e6 of principal needs 100e6 at K = 1
        assertEq(refund, 90e6);                         // 100e6 * 900 / 1000
        assertEq(quoted, coverage);
        assertEq(quotedRefund, refund);
        assertEq(_active(id), 100e6);
        assertEq(buck.premiumPrincipal(id), 10e6);
        assertEq(buck.balanceOf(ALICE), 90e6);
        assertEq(buck.balanceOf(address(pool)), 10e6);
        assertEq(_principal(id), 100e6);
        assertEq(buck.totalIssued(), 100e6);
        assertEq(buck.claimSeconds(), beforeSeconds);
        assertEq(buck.facilityPool(id), address(pool));
        assertEq(pool.calls(), 0);                      // no insurer callback under the token guard

        // The refund retires the remaining lien. A numeric burn settles the
        // least principal that lowers the balance by its amount, so one raw
        // unit of principal, coverage and deposit can remain.
        vm.prank(ALICE);
        buck.burn(90e6);
        assertEq(buck.balanceOf(ALICE), 0);
        assertLe(_principal(id), 1);
        assertLe(_active(id), 1);
        assertLe(buck.premiumPrincipal(id), 1);
        assertEq(buck.balanceOf(address(pool)), buck.premiumPrincipal(id));
        assertEq(buck.totalIssued(), _principal(id));
    }

    function test_releaseFreesTheExcessOnceKRises() public {
        JubileeInsurerPoolMock pool = _pool();
        k.setK(0.5e18);
        uint256 id = _facility(ALICE, 1000e6, 100);
        vm.prank(ALICE);
        buck.issue(id, 1000e6, 400e6, type(uint256).max, address(pool));
        assertEq(_principal(id), 500e6);                // exactly the limit at K = 0.5
        (, , uint256 count) = _release(ALICE);
        assertEq(count, 0);
        assertEq(_active(id), 1000e6);

        k.setK(0.8e18);
        (uint256 coverage, uint256 refund,) = _release(ALICE);
        assertEq(coverage, 375e6);                      // 500e6 / 0.8 = 625e6 is kept
        assertEq(refund, 37.5e6);
        assertEq(_active(id), 625e6);
        assertEq(buck.premiumPrincipal(id), 62.5e6);
        assertEq(buck.balanceOf(ALICE), 437.5e6);
        assertEq(buck.balanceOf(address(pool)), 62.5e6);
        assertEq(_principal(id), 500e6);
        assertEq(buck.totalIssued(), 500e6);
        (, , count) = _release(ALICE);                  // now exactly at the limit again
        assertEq(count, 0);

        vm.prank(ALICE);
        buck.burn(437.5e6);                             // the deposit kept its ratio: the rest closes together
        assertEq(buck.balanceOf(ALICE), 0);
        assertLe(_principal(id), 1);
        assertLe(_active(id), 2);
        assertLe(buck.premiumPrincipal(id), 1);
        assertEq(buck.totalIssued(), _principal(id));
    }

    function test_releaseNeverMovesAFacilityInShortfall() public {
        uint256 id = _credit(ALICE, 100e6, 0);
        k.setK(0.5e18);
        (uint256 quoted,) = _quote(ALICE, _one(id));
        assertEq(quoted, 0);
        (, , uint256 count) = _release(ALICE);
        assertEq(count, 0);
        assertEq(_active(id), 100e6);
        assertEq(_principal(id), 100e6);
        assertEq(buck.balanceOf(ALICE), 100e6);
    }

    function test_releaseHonoursTheSelectionAndRejectsDuplicateAndForeignFacilities() public {
        uint256 first = _facility(ALICE, 10e6, 0);
        uint256 second = _facility(ALICE, 20e6, 0);
        uint256 foreign = _facility(BOB, 30e6, 0);
        vm.startPrank(ALICE);
        buck.issue(first, 10e6, 0, 0, address(0));
        buck.issue(second, 20e6, 0, 0, address(0));
        vm.stopPrank();
        vm.prank(BOB);
        buck.issue(foreign, 30e6, 0, 0, address(0));

        uint256[] memory ids = new uint256[](2);
        ids[0] = second;
        ids[1] = second;
        vm.expectRevert(Buck.BadAmount.selector);
        vm.prank(ALICE);
        buck.burn(0, ids);
        ids[1] = foreign;
        vm.expectRevert(Buck.NotCreditOwner.selector);
        vm.prank(ALICE);
        buck.burn(0, ids);
        vm.expectRevert(Buck.NotCreditOwner.selector);
        vm.prank(ALICE);
        buck.quoteBurn(0, ids);

        vm.prank(ALICE);
        buck.burn(0, _one(second));
        assertEq(_active(first), 10e6);
        assertEq(_active(second), 0);
        assertEq(_active(foreign), 30e6);
        vm.prank(ALICE);
        buck.burn(0);
        assertEq(_active(first), 0);
        assertEq(_active(foreign), 30e6);
    }

    function test_releaseSkipsAFacilityItsPoolCannotRefundAndReleasesTheRest() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 insured = _facility(ALICE, 1000e6, 100);
        uint256 free = _facility(ALICE, 50e6, 0);
        vm.startPrank(ALICE);
        buck.issue(insured, 1000e6, 0, type(uint256).max, address(pool));
        buck.issue(free, 50e6, 0, 0, address(0));
        vm.stopPrank();
        uint256[] memory ids = new uint256[](2);
        ids[0] = insured;
        ids[1] = free;

        pool.invest(100e6);                             // the pool holds nothing liquid
        (uint256 quoted, uint256 quotedRefund) = _quote(ALICE, ids);
        assertEq(quoted, 50e6);
        assertEq(quotedRefund, 0);
        vm.recordLogs();
        vm.prank(ALICE);
        buck.burn(0, ids);
        (uint256 coverage, uint256 refund, uint256 count) = _released();
        assertEq(count, 1);
        assertEq(coverage, 50e6);
        assertEq(refund, 0);
        assertEq(_active(free), 0);
        assertEq(_active(insured), 1000e6);             // coverage is never released without its deposit
        assertEq(buck.premiumPrincipal(insured), 100e6);
        assertEq(pool.calls(), 0);

        pool.strategy().send(IERC20(address(buck)), address(pool), 100e6);
        vm.prank(address(pool));
        buck.approve(address(buck), 89e6);              // one raw BUCK short of the 90e6 refund
        (quoted,) = _quote(ALICE, ids);
        assertEq(quoted, 0);
        (, , count) = _release(ALICE);
        assertEq(count, 0);

        vm.prank(address(pool));
        buck.approve(address(buck), 90e6);
        (coverage, refund, count) = _release(ALICE);
        assertEq(count, 1);
        assertEq(coverage, 900e6);
        assertEq(refund, 90e6);
        assertEq(buck.allowance(address(pool), address(buck)), 0);
        assertEq(buck.balanceOf(ALICE), 90e6);
    }

    function test_releaseSharesOnePoolAcrossTheBatchInOrder() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 first = _facility(ALICE, 1000e6, 100);
        uint256 second = _facility(ALICE, 1000e6, 100);
        vm.startPrank(ALICE);
        buck.issue(first, 1000e6, 0, type(uint256).max, address(pool));
        buck.issue(second, 1000e6, 0, type(uint256).max, address(pool));
        vm.stopPrank();
        pool.invest(100e6);                             // 100e6 liquid: enough for one 90e6 refund only
        uint256[] memory ids = new uint256[](2);
        ids[0] = second;
        ids[1] = first;
        (uint256 quoted, uint256 quotedRefund) = _quote(ALICE, ids);
        vm.recordLogs();
        vm.prank(ALICE);
        buck.burn(0, ids);
        (uint256 coverage, uint256 refund, uint256 count) = _released();
        assertEq(count, 1);
        assertEq(coverage, 900e6);
        assertEq(refund, 90e6);
        assertEq(quoted, coverage);
        assertEq(quotedRefund, refund);
        assertEq(_active(second), 100e6);               // the first in the given order
        assertEq(_active(first), 1000e6);
    }

    function test_releaseRequiresAVerifiedOwnerAndServesACarryingOne() public {
        uint256 id = _facility(BOB, 100e6, 0);
        vm.prank(BOB);
        buck.issue(id, 100e6, 0, 0, address(0));
        vm.expectRevert(Buck.SenderNotVerified.selector);
        vm.prank(address(0xCAFE));
        buck.burn(0);
        vm.expectRevert(Buck.SenderNotVerified.selector);
        vm.prank(address(0xCAFE));
        buck.quoteBurn(0, new uint256[](0));
        identity.setIsCarrying(BOB, true);
        (uint256 coverage,, uint256 count) = _release(BOB);
        assertEq(count, 1);
        assertEq(coverage, 100e6);
        assertEq(_active(id), 0);
    }

    function test_releaseDoesNotDependOnTheFundValuation() public {
        uint256 drawn = _credit(ALICE, 100e6, 0);
        _credit(BOB, 100e6, 0);
        uint256 unused = _facility(ALICE, 40e6, 0);
        vm.prank(ALICE);
        buck.issue(unused, 40e6, 0, 0, address(0));
        vm.warp(t0 + YEAR);
        _invest(2e6);
        TestWheel wheel = _valuationWheel();
        vm.prank(GOV);
        wheel.setValuationSource(address(basket));
        vm.expectRevert(JubileeFund.MarkUnavailable.selector);
        vm.prank(ALICE);
        buck.burn(1e6);
        uint256 beforeBalance = buck.balanceOf(ALICE);
        (uint256 coverage,, uint256 count) = _release(ALICE);
        assertEq(count, 1);
        assertEq(coverage, 40e6);
        assertEq(_active(unused), 0);
        assertEq(_active(drawn), 100e6);
        assertEq(_principal(drawn), 100e6);
        assertEq(buck.balanceOf(ALICE), beforeBalance);
        assertFalse(wheel.valuationReady());
    }

    function test_releaseRefundsASponsoredOwnerInFullOrSkipsTheFacility() public {
        JubileeInsurerPoolMock pool = _pool();
        uint256 id = _facility(ALICE, 1000e6, 100);
        vm.prank(ALICE);
        buck.issue(id, 1000e6, 0, type(uint256).max, address(pool));
        _credit(BOB, 2e6, 0);
        vm.prank(ALICE); buck.requestDemurragePayer(BOB);
        vm.prank(BOB); buck.acceptDemurragePayer(ALICE);
        vm.warp(t0 + YEAR);                             // the pool's deposit now carries a year's fee

        // The sponsor cannot restore the 90e6 refund's carried fee (about 1.8e6).
        uint256 spare = buck.balanceOf(BOB) - 1e6;
        vm.prank(BOB);
        buck.transfer(INSURER, spare);
        (uint256 quoted,) = _quote(ALICE, _one(id));
        assertEq(quoted, 0);
        (, , uint256 count) = _release(ALICE);
        assertEq(count, 0);
        assertEq(_active(id), 1000e6);

        _credit(BOB, 10e6, 0);
        (uint256 coverage, uint256 refund,) = _release(ALICE);
        assertEq(coverage, 900e6);
        assertEq(refund, 90e6);
        assertEq(buck.balanceOf(ALICE), 90e6);          // full nominal: the sponsor paid the carried fee
        assertEq(_active(id), 100e6);
    }

    /// @dev The kept coverage is the least that still backs the principal:
    ///      one raw unit less would leave it short.
    function testFuzz_releaseKeepsTheLeastCoverageThatBacksThePrincipal(uint256 face, uint256 elapsed,
                                                                         uint256 issueK, uint256 releaseK, uint256 draw)
        public
    {
        face = bound(face, 100, 1e15);
        elapsed = bound(elapsed, 0, 4 * YEAR);
        issueK = bound(issueK, 0.05e18, 1e18);
        releaseK = bound(releaseK, 0.05e18, 1e18);
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, face, 0, BuckCredit.DepreciationType.LINEAR,
                                         2000, uint48(t0), 0);
        vm.warp(t0 + elapsed);
        uint256 mark = credit.depreciatedFaceValue(id);
        k.setK(issueK);
        uint256 limit = Math.mulDiv(mark, issueK, 1e18);
        vm.assume(limit != 0);
        draw = bound(draw, 1, limit);
        vm.prank(ALICE);
        buck.issue(id, face, draw, 0, address(0));

        k.setK(releaseK);
        uint256 capacity = Math.mulDiv(mark, releaseK, 1e18);
        (uint256 quoted,) = _quote(ALICE, _one(id));
        (uint256 coverage,,) = _release(ALICE);
        assertEq(quoted, coverage);
        uint256 active = _active(id);
        assertEq(active + coverage, face);
        assertEq(_principal(id), draw);
        assertEq(buck.balanceOf(ALICE), draw);
        assertEq(buck.totalIssued(), draw);
        if (draw >= capacity) {
            assertEq(coverage, 0);                      // short, or exactly at its limit
            return;
        }
        assertGe(Math.mulDiv(credit.currentValue(id), releaseK, 1e18), draw);
        assertLt(Math.mulDiv(Math.mulDiv(mark, active - 1, face), releaseK, 1e18), draw);
    }
}
