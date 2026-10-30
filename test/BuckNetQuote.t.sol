// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckNet} from "../src/BuckNet.sol";
import {BuckNetQuoter} from "../src/BuckNetQuoter.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {BuckLedger} from "../src/lib/BuckLedger.sol";
import {JubileeClaims} from "../src/lib/JubileeClaims.sol";
import {NetK, MockNetFund, MockNetInsurerPool} from "./BuckNet.t.sol";

/// @dev Exercise quotes against actual issuance, including fixed-point dust
///      and a changing K. The quote is a current estimate, not an execution
///      guarantee: controller, insurer, identity and fund state can change.
contract BuckNetQuoteTest is Test {
    uint256 constant YEAR = 365 days + 6 hours;
    address constant GOV = address(0xA0);
    address constant ALICE = address(0xA11CE);
    address constant INSURER = address(0x1551E1);

    IdentityRegistryHarness identity;
    BuckCredit credit;
    BuckNet buck;
    BuckNetQuoter quoter;
    MockNetFund fund;
    MockNetInsurerPool pool;
    NetK k;
    uint256 t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        identity = new IdentityRegistryHarness(GOV);
        credit = new BuckCredit();
        k = new NetK();
        fund = new MockNetFund();
        buck = new BuckNet(address(credit), address(k), address(identity), address(fund));
        quoter = new BuckNetQuoter(buck);
        credit.setBuck(address(buck));
        vm.prank(GOV);
        identity.setBuck(address(buck));
        _bind(ALICE, false, address(this));
        _bind(INSURER, false, address(this));
        _bind(address(fund), true, address(this));
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
    }

    function _bind(address target, bool carrying, address binder) private {
        if (target.code.length == 0) vm.etch(target, hex"60006000fd");
        vm.prank(binder);
        identity.bindContract(target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    function _credit(uint256 face, uint32 rate) private returns (uint256 id) {
        vm.prank(INSURER);
        id = credit.createCredit(ALICE, 0, face, face, BuckCredit.DepreciationType.NONE, 0, 0, rate);
    }

    function _pool() private {
        pool = new MockNetInsurerPool();
        _bind(address(pool), true, INSURER);
        vm.prank(INSURER);
        buck.setInsurerPool(address(pool));
    }

    function _fundOwner(uint256 amount) private {
        uint256 id = _credit(amount, 0);
        vm.prank(ALICE);
        buck.issue(id, amount, amount);
    }

    function test_quoteKeepsTheTokenWithinTheEvmRuntimeLimit() public view {
        assertLe(address(buck).code.length, 24_576);
        assertLe(address(quoter).code.length, 24_576);
    }

    function test_quoteMatchesIssuanceForSuccessiveCoverageTranches() public {
        _pool();
        _fundOwner(300e6);
        k.setK(0.75e18);
        k.setFactor(2e18);
        uint256 id = _credit(1000e6, 100);
        (uint256 value, uint256 premium, uint256 reserve, uint256 spendable) = quoter.quoteIssue(id, 500e6);
        assertEq(value, 500e6);
        assertEq(premium, 50e6);
        assertEq(reserve, 100e6);
        assertEq(spendable, 325e6);
        vm.prank(ALICE);
        (uint256 principal, uint256 paid) = buck.issue(id, 500e6, spendable);
        assertEq(principal, spendable + premium);
        assertEq(paid, premium);
        assertEq(buck.balanceOf(ALICE), 300e6 + spendable);
        assertEq(buck.balanceOf(address(pool)), premium);

        (value, premium, reserve, spendable) = quoter.quoteIssue(id, 500e6);
        assertEq(value, 1000e6);
        assertEq(premium, 50e6);
        assertEq(reserve, 100e6);
        assertEq(spendable, 325e6);
        vm.prank(ALICE);
        buck.issue(id, 500e6, spendable);
        (principal,) = credit.issuanceOf(id);
        assertEq(principal, 750e6);
        assertEq(buck.balanceOf(address(pool)), 100e6);
    }

    function test_quoteIncludesActualDepreciationAndRejectsExcessCoverage() public {
        _pool();
        k.setK(0.75e18);
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 1000e6, 0, BuckCredit.DepreciationType.LINEAR,
                                         1000, uint48(t0), 100);
        vm.warp(t0 + YEAR);
        (uint256 value, uint256 premium, uint256 reserve, uint256 spendable) = quoter.quoteIssue(id, 1000e6);
        assertEq(value, 900e6);
        assertEq(premium, 90e6);
        assertEq(reserve, 0);
        assertEq(spendable, 585e6);
        vm.prank(ALICE);
        (uint256 principal, uint256 paid) = buck.issue(id, 1000e6, spendable);
        assertEq(principal, 675e6);
        assertEq(paid, premium);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        quoter.quoteIssue(id, 1);
    }

    function test_quoteIsNotAnExecutionPromiseWhenKChanges() public {
        uint256 id = _credit(100e6, 0);
        (,,, uint256 previouslyQuoted) = quoter.quoteIssue(id, 100e6);
        assertEq(previouslyQuoted, 100e6);
        k.setK(0.5e18);
        (,,, uint256 currentQuote) = quoter.quoteIssue(id, 100e6);
        assertEq(currentQuote, 50e6);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        buck.issue(id, 100e6, previouslyQuoted);
        assertEq(buck.totalIssued(), 0);
    }

    function test_quoteBlocksNewIssueAgainstAnAlreadyOverIssuedFacility() public {
        uint256 id = _credit(100e6, 0);
        vm.prank(ALICE);
        buck.issue(id, 100e6, 100e6);
        k.setK(0.5e18);
        (,,, uint256 spendable) = quoter.quoteIssue(id, 0);
        assertEq(spendable, 0);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        vm.prank(ALICE);
        buck.issue(id, 0, 1);
    }

    function test_quoteUsesTheIncrementalPresentValueAfterDepreciation() public {
        _pool();
        vm.prank(INSURER);
        uint256 id = credit.createCredit(ALICE, 0, 1000e6, 0, BuckCredit.DepreciationType.LINEAR,
                                         1000, uint48(t0), 100);
        vm.prank(ALICE);
        buck.issue(id, 400e6, 360e6);
        vm.warp(t0 + YEAR);
        (uint256 value, uint256 premium,, uint256 spendable) = quoter.quoteIssue(id, 600e6);
        assertEq(value, 900e6);
        assertEq(premium, 54e6); // 10% of the 540-BUCK increment, not the whole 900.
        assertEq(spendable, 446e6);
        vm.prank(ALICE);
        (uint256 principal, uint256 paid) = buck.issue(id, 600e6, spendable);
        assertEq(principal, 500e6);
        assertEq(paid, premium);
        (uint256 total,) = credit.issuanceOf(id);
        assertEq(total, 900e6);
    }

    function test_quoteRespectsTheTemporaryAccountBalanceLimit() public {
        uint256 accountLimit = (uint256(1) << 79) - 1;
        _fundOwner(accountLimit);
        uint256 id = _credit(100, 0);
        (,,, uint256 spendable) = quoter.quoteIssue(id, 100);
        assertEq(spendable, 0);
        vm.expectRevert(BuckLedger.AccountOverflow.selector);
        vm.prank(ALICE);
        buck.issue(id, 100, 1);
    }

    function test_quotePreservesFractionalHeadroomAtTheAccountLimit() public {
        uint256 accountLimit = (uint256(1) << 79) - 1;
        _fundOwner(accountLimit);
        vm.warp(t0 + 1);
        (uint256 held, uint256 unspent) = buck.heldAndUnspentScaled(ALICE);
        uint256 room = (accountLimit * 1e36 - held - unspent) / 1e36;
        assertGt(room, 0);
        uint256 id = _credit(room + 1, 0);
        (,,, uint256 spendable) = quoter.quoteIssue(id, room + 1);
        assertEq(spendable, room);
        vm.prank(ALICE);
        buck.issue(id, room + 1, spendable);
        (,,, spendable) = quoter.quoteIssue(id, 0);
        assertEq(spendable, 0);
        vm.expectRevert(BuckLedger.AccountOverflow.selector);
        vm.prank(ALICE);
        buck.issue(id, 0, 1);
    }

    function test_quoteDoesNotBypassAMaturityBacklog() public {
        _fundOwner(100e6);
        uint256 id = _credit(50e6, 0);
        vm.warp(t0 + 50 * YEAR);
        assertTrue(buck.maturitiesPending());
        (,,, uint256 spendable) = quoter.quoteIssue(id, 50e6);
        assertEq(spendable, 50e6);
        vm.expectRevert(JubileeClaims.PendingMaturities.selector);
        vm.prank(ALICE);
        buck.issue(id, 50e6, spendable);
        buck.processMaturities(1);
        vm.prank(ALICE);
        buck.issue(id, 50e6, spendable);
        (uint256 principal,) = credit.issuanceOf(id);
        assertEq(principal, spendable);
    }

    function test_quoteRejectsInvalidPremiumAndCarryingOwnersCannotIssue() public {
        uint256 invalid = _credit(100e6, 1000);
        vm.expectRevert(BuckNet.BadPremiumRate.selector);
        quoter.quoteIssue(invalid, 100e6);
        _pool();
        vm.prank(address(pool));
        credit.setCreditIssuer(INSURER, true);
        vm.prank(INSURER);
        uint256 carrying = credit.createCredit(address(pool), 0, 100e6, 100e6,
                                               BuckCredit.DepreciationType.NONE, 0, 0, 0);
        (,,, uint256 spendable) = quoter.quoteIssue(carrying, 100e6);
        assertEq(spendable, 0);
        vm.expectRevert(BuckNet.NotCarryingPool.selector);
        vm.prank(address(pool));
        buck.issue(carrying, 100e6, 1);
    }

    function test_quoteWorksForZeroFaceAndDoesNotPromiseAPool() public {
        uint256 empty = _credit(0, 0);
        (uint256 value, uint256 premium, uint256 reserve, uint256 spendable) = quoter.quoteIssue(empty, 0);
        assertEq(value + premium + reserve + spendable, 0);
        vm.expectRevert(BuckNet.CreditExceeded.selector);
        quoter.quoteIssue(empty, 1);
        uint256 id = _credit(100e6, 100);
        (value, premium,, spendable) = quoter.quoteIssue(id, 100e6);
        assertEq(value, 100e6);
        assertEq(premium, 10e6);
        assertEq(spendable, 90e6);
        vm.expectRevert(BuckNet.NoInsurerPool.selector);
        vm.prank(ALICE);
        buck.issue(id, 100e6, spendable);
    }

    function testFuzz_quoteAgreesWithActualIssuance(
        uint64 face_, uint64 coverage_, uint16 rate_, uint16 kBps_, uint16 factorBps_
    ) public {
        _pool();
        _fundOwner(1e12);
        uint256 face = bound(uint256(face_), 1e6, 1e12);
        uint256 coverage = bound(uint256(coverage_), 1, face);
        uint32 rate = uint32(bound(uint256(rate_), 0, 999));
        uint256 kBps = bound(uint256(kBps_), 5000, 10000);
        uint256 factorBps = bound(uint256(factorBps_), 0, 10000);
        k.setK(kBps * 1e14);
        k.setFactor(factorBps * 1e14);
        uint256 id = _credit(face, rate);
        (, uint256 premium, uint256 reserve, uint256 spendable) = quoter.quoteIssue(id, coverage);
        assertLe(reserve, buck.balanceOf(ALICE));
        if (spendable == 0) return;
        uint256 ownerBefore = buck.balanceOf(ALICE);
        vm.prank(ALICE);
        (uint256 issued, uint256 paid) = buck.issue(id, coverage, spendable);
        assertEq(issued, spendable + premium);
        assertEq(paid, premium);
        assertEq(buck.balanceOf(ALICE) - ownerBefore, spendable);
        assertEq(buck.balanceOf(address(pool)), premium);
    }
}
