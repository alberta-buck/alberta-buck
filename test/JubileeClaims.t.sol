// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {JubileeClaims} from "../src/lib/JubileeClaims.sol";

/// @dev Test-only host. A production host must restrict updates to Buck and
///      source every update from its actual BuckCredit contract.
contract JubileeClaimsHarness {
    using JubileeClaims for JubileeClaims.Book;
    JubileeClaims.Book internal book;
    BuckCredit internal immutable credit;
    constructor(BuckCredit credit_) { credit = credit_; book.initialize(); }
    function activate(uint256 id, address owner, uint256 amount) external {
        credit.activateFromBuck(id, owner, amount);
    }
    function issue(uint256 id, address owner, uint256 amount) external {
        credit.recordIssuanceFromBuck(id, owner, amount);
        _sync(id);
    }
    function reduce(uint256 id, address owner, uint256 amount) external {
        credit.recordReductionFromBuck(id, owner, amount);
        _sync(id);
    }
    function sync(uint256 id) external { _sync(id); }
    function _sync(uint256 id) private {
        (uint256 principal, uint256 seconds_) = credit.issuanceOf(id);
        book.update(id, principal, seconds_);
    }
    function setRaw(uint256 id, uint256 principal, uint256 seconds_) external {
        book.update(id, principal, seconds_);
    }
    function process(uint256 maxSteps) external returns (uint256, bool) { return book.process(maxSteps); }
    function totalSeconds() external view returns (uint256) { return book.totalSeconds(); }
    function positionSeconds(uint256 id) external view returns (uint256) { return book.positionSeconds(id); }
    function pending() external view returns (bool) { return book.pending(); }
    function nextMaturity() external view returns (uint256, uint256) { return book.nextMaturity(); }
}

contract JubileeClaimsTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    uint256 constant MATURITY = 50 * YEAR;
    address constant ALICE = address(0xA11CE);
    address constant INSURER = address(0x1551E1);
    BuckCredit internal credit;
    JubileeClaimsHarness internal h;
    uint256 internal t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        credit = new BuckCredit();
        h = new JubileeClaimsHarness(credit);
        credit.setBuck(address(h));
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
    }

    function _facility(uint256 coverage, uint256 issued) internal returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(ALICE, 0, coverage, coverage,
                                 BuckCredit.DepreciationType.NONE, 0, 0, 0);
        h.activate(id, ALICE, coverage);
        h.issue(id, ALICE, issued);
    }

    function test_aggregateTracksSeparatelyAgedNftsAcrossTheFiftyYearCap() public {
        uint256 a = _facility(100e6, 100e6);
        vm.warp(t0 + 25 * YEAR);
        uint256 b = _facility(200e6, 200e6);
        assertEq(h.totalSeconds(), 100e6 * 25 * YEAR);
        vm.warp(t0 + 60 * YEAR);
        assertTrue(h.pending());
        vm.expectRevert(JubileeClaims.PendingMaturities.selector);
        h.totalSeconds();
        (uint256 processed, bool current) = h.process(1);
        assertEq(processed, 1);
        assertTrue(current);
        assertEq(h.totalSeconds(), 100e6 * 50 * YEAR + 200e6 * 35 * YEAR);
        (, uint256 aSeconds) = credit.issuanceOf(a);
        (, uint256 bSeconds) = credit.issuanceOf(b);
        assertEq(h.totalSeconds(), aSeconds + bSeconds);
        (uint256 nextId, uint256 nextAt) = h.nextMaturity();
        assertEq(nextId, b);
        assertEq(nextAt, t0 + 75 * YEAR);
    }

    function test_boundedBacklogMustBeProcessedBeforeUpdateAndRollsBackCredit() public {
        uint256 first = _facility(101e6, 100e6);
        _facility(100e6, 100e6);
        _facility(100e6, 100e6);
        vm.warp(t0 + 50 * YEAR);
        vm.expectRevert(JubileeClaims.PendingMaturities.selector);
        h.issue(first, ALICE, 1e6);
        (uint256 beforePrincipal,) = credit.issuanceOf(first);
        assertEq(beforePrincipal, 100e6, "failed sync rolls back the credit call");
        (uint256 processed, bool current) = h.process(1);
        assertEq(processed, 1);
        assertFalse(current);
        assertTrue(h.pending());
        vm.expectRevert(JubileeClaims.PendingMaturities.selector);
        h.totalSeconds();
        (processed, current) = h.process(2);
        assertEq(processed, 2);
        assertTrue(current);
        assertFalse(h.pending());
        assertEq(h.totalSeconds(), 300e6 * MATURITY);
        h.issue(first, ALICE, 1e6);
        assertEq(h.totalSeconds(), 300e6 * MATURITY, "new credit has no prior claimable age");
    }

    function test_manySimultaneousMaturitiesCanBeClearedInBoundedBatches() public {
        for (uint256 i = 1; i <= 64; ++i) h.setRaw(i, 1e6, 0);
        vm.warp(t0 + 50 * YEAR);
        uint256 maxBatchGas;
        for (uint256 batch; batch < 8; ++batch) {
            uint256 beforeGas = gasleft();
            (uint256 processed, bool current) = h.process(8);
            uint256 used = beforeGas - gasleft();
            if (used > maxBatchGas) maxBatchGas = used;
            assertEq(processed, 8);
            assertEq(current, batch == 7);
        }
        emit log_named_uint("maximum gas to process eight maturities", maxBatchGas);
        assertEq(h.totalSeconds(), 64e6 * MATURITY);
    }

    function test_partialReductionsAndNewIssuanceRescheduleTheSameNft() public {
        uint256 a = _facility(200e6, 100e6);
        vm.warp(t0 + 20 * YEAR);
        uint256 b = _facility(200e6, 100e6);
        vm.warp(t0 + 30 * YEAR);
        h.issue(a, ALICE, 50e6);
        h.reduce(b, ALICE, 25e6);
        (, uint256 aSeconds) = credit.issuanceOf(a);
        (, uint256 bSeconds) = credit.issuanceOf(b);
        assertEq(h.totalSeconds(), aSeconds + bSeconds);
        (uint256 nextId, uint256 nextAt) = h.nextMaturity();
        assertEq(nextId, a);
        assertEq(nextAt, t0 + 60 * YEAR);
        vm.warp(t0 + 61 * YEAR);
        (uint256 processed, bool current) = h.process(10);
        assertEq(processed, 1);
        assertTrue(current);
        (, aSeconds) = credit.issuanceOf(a);
        (, bSeconds) = credit.issuanceOf(b);
        assertEq(h.totalSeconds(), aSeconds + bSeconds);
    }

    function test_fractionalMaturityDoesNotOvercountTheCeilingSecond() public {
        h.setRaw(7, 3, 3 * MATURITY - 1);
        (uint256 id, uint256 at) = h.nextMaturity();
        assertEq(id, 7);
        assertEq(at, t0 + 1);
        vm.warp(t0 + 1);
        (uint256 processed, bool current) = h.process(1);
        assertEq(processed, 1);
        assertTrue(current);
        assertEq(h.totalSeconds(), 3 * MATURITY);
    }

    function test_fullReductionRemovesPositionAndEmptyBookKeepsZeroBenchmark() public {
        uint256 id = _facility(100e6, 100e6);
        vm.warp(t0 + 10 * YEAR);
        h.reduce(id, ALICE, 100e6);
        assertEq(h.totalSeconds(), 0);
        (, uint256 nextAt) = h.nextMaturity();
        assertEq(nextAt, 0);
        vm.warp(t0 + 100 * YEAR);
        assertEq(h.totalSeconds(), 0);
        assertFalse(h.pending());
    }

    function test_invalidInputsAndEmptyProcessing() public {
        vm.expectRevert(JubileeClaims.ZeroStepLimit.selector);
        h.process(0);
        vm.expectRevert(JubileeClaims.InvalidPosition.selector);
        h.setRaw(7, 0, 1);
        vm.expectRevert(JubileeClaims.InvalidPosition.selector);
        h.setRaw(7, 1, MATURITY + 1);
        (uint256 processed, bool current) = h.process(1);
        assertEq(processed, 0);
        assertTrue(current);
        assertEq(h.totalSeconds(), 0);
    }

    function testFuzz_aggregateEqualsIndividualCappedPositionsAfterMaturities(
        uint32 seed, uint16 elapsedDays
    ) public {
        for (uint256 i = 1; i <= 5; ++i) {
            uint256 principal = bound(uint256(keccak256(abi.encode(seed, i))), 1, 1e12);
            uint256 seconds_ = bound(uint256(keccak256(abi.encode(seed, i, 1))), 0, principal * MATURITY);
            h.setRaw(i, principal, seconds_);
        }
        vm.warp(t0 + bound(elapsedDays, 0, 36_525) * 1 days);
        (, bool current) = h.process(5);
        assertTrue(current);
        uint256 expected;
        for (uint256 i = 1; i <= 5; ++i) expected += h.positionSeconds(i);
        assertEq(h.totalSeconds(), expected);
    }

    function testFuzz_heapReplacementAndRemovalKeepTheAggregateExact(uint32 seed, uint16 elapsedDays) public {
        for (uint256 i = 1; i <= 5; ++i) {
            uint256 principal = bound(uint256(keccak256(abi.encode(seed, i))), 1, 1e12);
            uint256 seconds_ = bound(uint256(keccak256(abi.encode(seed, i, 1))), 0, principal * MATURITY);
            h.setRaw(i, principal, seconds_);
        }
        uint256 checkAt = t0 + bound(elapsedDays, 0, 18_000) * 1 days;
        vm.warp(checkAt);
        (, bool current) = h.process(5);
        assertTrue(current);
        h.setRaw(3, 0, 0);
        uint256 replacement = bound(uint256(keccak256(abi.encode(seed, "replacement"))), 1, 1e12);
        h.setRaw(2, replacement, replacement * MATURITY / 3);
        h.setRaw(4, replacement, replacement * MATURITY - 1);
        uint256 expected;
        for (uint256 i = 1; i <= 5; ++i) expected += h.positionSeconds(i);
        assertEq(h.totalSeconds(), expected);
        vm.warp(checkAt + 2);
        (, current) = h.process(5);
        assertTrue(current);
        expected = 0;
        for (uint256 i = 1; i <= 5; ++i) expected += h.positionSeconds(i);
        assertEq(h.totalSeconds(), expected);
    }
}
