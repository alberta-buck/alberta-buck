// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckTypes} from "../src/BuckTypes.sol";

/// @notice Exercises BuckCredit's restricted accounting hooks in isolation.
///         The legacy Buck contract does not yet call them or mint BUCK.
contract BuckCreditIssuanceAgeTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant INSURER = address(0x1551E1);
    BuckCredit internal credit;
    uint256 internal t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        credit = new BuckCredit();
        credit.setBuck(address(this));
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
    }

    function _facility(uint256 coverage) internal returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(ALICE, 0, coverage, coverage,
                                 BuckCredit.DepreciationType.NONE, 0, 0, 0);
        credit.activateFromBuck(id, ALICE, coverage);
    }

    function test_separateFacilitiesAccrueFromIssuedPrincipalNotCoverage() public {
        uint256 old = _facility(2_000_000e6);
        credit.recordIssuanceFromBuck(old, ALICE, 1_000_000e6);
        vm.warp(t0 + YEAR);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(old);
        assertEq(principal, 1_000_000e6);
        assertEq(seconds_, 1_000_000e6 * YEAR);
        assertEq(credit.issuanceRelief(old, principal), 20_000e6);

        uint256 newer = _facility(1_000_000e6);
        credit.recordIssuanceFromBuck(newer, ALICE, 500_000e6);
        vm.warp(t0 + 3 * YEAR);
        assertEq(credit.issuanceRelief(old, 1_000_000e6), 60_000e6);
        assertEq(credit.issuanceRelief(newer, 500_000e6), 20_000e6);
        assertEq(credit.recordReductionFromBuck(newer, ALICE, 250_000e6), 10_000e6);
        (principal, seconds_) = credit.issuanceOf(newer);
        assertEq(principal, 250_000e6);
        assertEq(seconds_, 250_000e6 * 2 * YEAR);
        assertEq(credit.issuanceRelief(old, 1_000_000e6), 60_000e6);
    }

    function test_additionalIssuanceDilutesAgeWithinOneFacility() public {
        uint256 id = _facility(200e6);
        credit.recordIssuanceFromBuck(id, ALICE, 100e6);
        vm.warp(t0 + YEAR);
        credit.recordIssuanceFromBuck(id, ALICE, 100e6);
        vm.warp(t0 + 2 * YEAR);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 200e6);
        assertEq(seconds_, 300e6 * YEAR);
        assertEq(credit.issuanceRelief(id, 200e6), 6e6);
        assertEq(credit.recordReductionFromBuck(id, ALICE, 100e6), 3e6);
        (principal, seconds_) = credit.issuanceOf(id);
        assertEq(principal, 100e6);
        assertEq(seconds_, 150e6 * YEAR);
    }

    function test_fiftyYearCapAndPartialReductionKeepRemainingHistory() public {
        uint256 id = _facility(100e6);
        credit.recordIssuanceFromBuck(id, ALICE, 100e6);
        vm.warp(t0 + 100 * YEAR);
        assertEq(credit.issuanceRelief(id, 100e6), 100e6);
        assertEq(credit.recordReductionFromBuck(id, ALICE, 50e6), 50e6);
        assertEq(credit.issuanceRelief(id, 50e6), 50e6);
        credit.recordReductionFromBuck(id, ALICE, 50e6);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 0);
        assertEq(seconds_, 0);
    }

    function test_newIssuanceAfterFiftyYearCapDoesNotInheritExcessAge() public {
        uint256 id = _facility(200e6);
        credit.recordIssuanceFromBuck(id, ALICE, 100e6);
        vm.warp(t0 + 100 * YEAR);
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(principal, 100e6);
        assertEq(seconds_, 100e6 * 50 * YEAR);
        credit.recordIssuanceFromBuck(id, ALICE, 100e6);
        assertEq(credit.issuanceRelief(id, 200e6), 100e6);
        vm.warp(t0 + 101 * YEAR);
        assertEq(credit.issuanceRelief(id, 200e6), 104e6);
    }

    function test_renewalCheckpointsReliefAndKeepsSameNft() public {
        uint256 id = _facility(200e6);
        credit.recordIssuanceFromBuck(id, ALICE, 100e6);
        vm.warp(t0 + 10 * YEAR);
        (uint256 baseline, uint256 principal) = credit.recordRenewalFromBuck(id, ALICE, 15e6, 50e6);
        assertEq(baseline, 20e6);
        assertEq(principal, 135e6);
        assertEq(credit.ownerOf(id), ALICE);
        (uint256 recorded, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(recorded, 135e6);
        assertEq(seconds_, 0);
        vm.warp(t0 + 11 * YEAR);
        assertEq(credit.issuanceRelief(id, principal), 2700000);
    }

    function test_reappraisalAndPremiumChangeDoNotAlterIssuedPrincipalOrAge() public {
        uint256 id = _facility(1000e6);
        credit.recordIssuanceFromBuck(id, ALICE, 600e6);
        vm.warp(t0 + 5 * YEAR);
        (uint256 beforePrincipal, uint256 beforeSeconds) = credit.issuanceOf(id);
        vm.prank(INSURER);
        credit.updateCredit(id, 1500e6, 1500e6, BuckCredit.DepreciationType.NONE, 0, 0, 50);
        (uint256 afterPrincipal, uint256 afterSeconds) = credit.issuanceOf(id);
        assertEq(afterPrincipal, beforePrincipal);
        assertEq(afterSeconds, beforeSeconds);
        assertEq(credit.issuanceRelief(id, afterPrincipal), 60e6);
    }

    function testFuzz_partialReductionKeepsItsProportionalAge(uint80 amount_, uint80 reduction_, uint16 years_) public {
        uint256 amount = bound(amount_, 1, BuckTypes.MAX_BALANCE);
        uint256 reduction = bound(reduction_, 1, amount);
        uint256 yearsElapsed = bound(years_, 0, 150);
        uint256 id = _facility(amount);
        credit.recordIssuanceFromBuck(id, ALICE, amount);
        vm.warp(t0 + yearsElapsed * YEAR);
        uint256 target = reduction * yearsElapsed / 50;
        if (target > reduction) target = reduction;
        assertEq(credit.recordReductionFromBuck(id, ALICE, reduction), target);
        (uint256 left, uint256 seconds_) = credit.issuanceOf(id);
        assertEq(left, amount - reduction);
        assertEq(seconds_, left * (yearsElapsed > 50 ? 50 : yearsElapsed) * YEAR);
    }

    function test_onlyBuckAndFacilityOwnerCanUseAccountingHooks() public {
        uint256 id = _facility(100e6);
        vm.prank(ALICE);
        vm.expectRevert(bytes("BuckCredit: not buck"));
        credit.recordIssuanceFromBuck(id, ALICE, 100e6);
        vm.expectRevert(bytes("BuckCredit: not holder"));
        credit.recordIssuanceFromBuck(id, BOB, 100e6);
        credit.recordIssuanceFromBuck(id, ALICE, 100e6);
        vm.prank(ALICE);
        vm.expectRevert(bytes("BuckCredit: not buck"));
        credit.recordReductionFromBuck(id, ALICE, 1);
        vm.prank(ALICE);
        vm.expectRevert(bytes("BuckCredit: not buck"));
        credit.recordRenewalFromBuck(id, ALICE, 1, 0);
    }

    function test_coverageAndNftCannotLeaveWhileIssuanceRemains() public {
        uint256 id = _facility(100e6);
        credit.recordIssuanceFromBuck(id, ALICE, 100e6);
        vm.expectRevert(bytes("BuckCredit: outstanding issuance"));
        credit.deactivateFromBuck(id, ALICE, 100e6);
        vm.prank(ALICE);
        vm.expectRevert(bytes("BuckCredit: credit in use"));
        credit.transferFrom(ALICE, BOB, id);
        credit.recordReductionFromBuck(id, ALICE, 100e6);
        credit.deactivateFromBuck(id, ALICE, 100e6);
        vm.prank(ALICE);
        credit.transferFrom(ALICE, BOB, id);
        assertEq(credit.ownerOf(id), BOB);
    }

    function test_rejectsUnactivatedInvalidAndOverflowingIssuance() public {
        vm.prank(INSURER);
        uint256 inactive = credit.createCredit(ALICE, 0, 100e6, 100e6,
                                                BuckCredit.DepreciationType.NONE, 0, 0, 0);
        vm.expectRevert(bytes("BuckCredit: inactive credit"));
        credit.recordIssuanceFromBuck(inactive, ALICE, 1);
        uint256 id = _facility(BuckTypes.MAX_BALANCE);
        vm.expectRevert(bytes("BuckCredit: issuance zero"));
        credit.recordIssuanceFromBuck(id, ALICE, 0);
        credit.recordIssuanceFromBuck(id, ALICE, BuckTypes.MAX_BALANCE);
        vm.expectRevert(bytes("BuckCredit: issuance overflow"));
        credit.recordIssuanceFromBuck(id, ALICE, 1);
        vm.expectRevert(bytes("BuckCredit: invalid reduction"));
        credit.recordReductionFromBuck(id, ALICE, 0);
        vm.expectRevert(bytes("BuckCredit: invalid relief"));
        credit.recordRenewalFromBuck(id, ALICE, BuckTypes.MAX_BALANCE + 1, 0);
    }
}
