// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {JubileeClaimBook} from "../src/JubileeClaimBook.sol";
import {JubileeClaims} from "../src/lib/JubileeClaims.sol";

/// @dev The test contract acts as the trusted Buck for the isolated book.
///      Real token/fund atomicity is tested separately after integration.
contract JubileeObligationsTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    address constant ALICE = address(0xA11CE);
    address constant INSURER = address(0x1551E1);
    BuckCredit credit;
    JubileeClaimBook book;
    uint256 t0;
    mapping(uint256 => address) public facilityPool;
    mapping(uint256 => uint256) public premiumPrincipal;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        credit = new BuckCredit();
        credit.setBuck(address(this));
        book = new JubileeClaimBook(address(this), address(credit));
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
    }

    function _issue(uint256 amount) private returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(ALICE, 0, amount, amount, BuckCredit.DepreciationType.NONE, 0, 0, 0);
        credit.activateFromBuck(id, ALICE, amount);
        credit.recordIssuanceFromBuck(id, ALICE, amount);
        book.sync(id);
    }

    function test_deferralPreservesTheBenchmarkUntilFundActuallyBurns() public {
        uint256 id = _issue(1_000_000e6);
        vm.warp(t0 + 50 * YEAR);
        (uint256 processed, bool current) = book.process(1);
        assertEq(processed, 1);
        assertTrue(current);
        uint256 originalSeconds = book.totalSeconds();
        assertEq(originalSeconds, 1_000_000e6 * 50 * YEAR);
        assertEq(book.deferMatured(id), 1_000_000e6);
        assertEq(book.deferredPrincipal(), 1_000_000e6);
        assertEq(book.positionSeconds(id), 0);
        assertEq(book.totalSeconds(), originalSeconds);
        vm.expectRevert(JubileeClaimBook.NotMature.selector);
        book.deferMatured(id);
        credit.recordReductionFromBuck(id, ALICE, 1_000_000e6);
        credit.deactivateFromBuck(id, ALICE, 1_000_000e6);
        credit.closeFromBuck(id, ALICE);
        vm.expectRevert();
        credit.ownerOf(id);
        book.consumeDeferred(200_000e6);
        assertEq(book.deferredPrincipal(), 800_000e6);
        assertEq(book.totalSeconds(), 800_000e6 * 50 * YEAR);
    }

    function test_deferredObligationsDoNotAgeFurtherWhileOpenClaimsDo() public {
        uint256 first = _issue(100e6);
        vm.warp(t0 + 25 * YEAR);
        _issue(100e6);
        vm.warp(t0 + 50 * YEAR);
        book.process(1);
        uint256 benchmark = (100e6 * 50 + 100e6 * 25) * YEAR;
        assertEq(book.totalSeconds(), benchmark);
        book.deferMatured(first);
        credit.recordReductionFromBuck(first, ALICE, 100e6);
        credit.deactivateFromBuck(first, ALICE, 100e6);
        credit.closeFromBuck(first, ALICE);
        vm.warp(t0 + 55 * YEAR);
        assertEq(book.totalSeconds(), (100e6 * 50 + 100e6 * 30) * YEAR);
        book.consumeDeferred(40e6);
        assertEq(book.totalSeconds(), (60e6 * 50 + 100e6 * 30) * YEAR);
    }

    function test_onlyTokenCanDeferOrBurnAndMaturityBacklogMustBeProcessed() public {
        uint256 id = _issue(100e6);
        vm.prank(ALICE);
        vm.expectRevert(JubileeClaimBook.NotBuck.selector);
        book.deferMatured(id);
        vm.prank(ALICE);
        vm.expectRevert(JubileeClaimBook.NotBuck.selector);
        book.consumeDeferred(1);
        vm.expectRevert(JubileeClaimBook.NotMature.selector);
        book.deferMatured(id);
        vm.warp(t0 + 50 * YEAR);
        vm.expectRevert(JubileeClaims.PendingMaturities.selector);
        book.deferMatured(id);
        book.process(1);
        book.deferMatured(id);
        vm.expectRevert(JubileeClaimBook.BadDeferredAmount.selector);
        book.consumeDeferred(0);
        vm.expectRevert(JubileeClaimBook.BadDeferredAmount.selector);
        book.consumeDeferred(100e6 + 1);
        assertEq(book.deferredPrincipal(), 100e6);
    }

    function test_maturityPinsThePremiumRefundToItsOriginalOwnerAndPool() public {
        uint256 id = _issue(100e6);
        facilityPool[id] = address(0x1234);
        premiumPrincipal[id] = 10e6;
        vm.warp(t0 + 50 * YEAR);
        book.process(1);
        book.deferMatured(id);
        credit.recordReductionFromBuck(id, ALICE, 100e6);
        credit.deactivateFromBuck(id, ALICE, 100e6);
        credit.closeFromBuck(id, ALICE);
        delete facilityPool[id];
        delete premiumPrincipal[id];
        (address owner, address pool, uint256 nominal) = book.premiumRefunds(id);
        assertEq(owner, ALICE);
        assertEq(pool, address(0x1234));
        assertEq(nominal, 10e6);
        vm.prank(ALICE);
        vm.expectRevert(JubileeClaimBook.NotBuck.selector);
        book.consumePremiumRefund(id);
        (owner, pool, nominal) = book.consumePremiumRefund(id);
        assertEq(owner, ALICE);
        assertEq(pool, address(0x1234));
        assertEq(nominal, 10e6);
        vm.expectRevert(JubileeClaimBook.BadPremiumRefund.selector);
        book.consumePremiumRefund(id);
    }
}
