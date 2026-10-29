// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckLedger} from "../src/lib/BuckLedger.sol";
import {BuckTypes} from "../src/BuckTypes.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

/// @dev This harness exposes internal operations only for accounting tests;
///      it is not a production token and has no issuance authorization.
contract BuckLedgerHarness {
    using BuckLedger for BuckLedger.Ledger;
    BuckLedger.Ledger internal book;
    constructor(address fund) { book.initialize(fund); }
    function issue(address owner, uint256 amount, bool carrying) external { book.issue(owner, amount, carrying); }
    function retire(address owner, uint256 amount, bool carrying) external { book.retire(owner, amount, carrying); }
    function retireWithFund(address owner, uint256 amount, uint256 reliefScaled, bool carrying)
        external returns (uint256)
    {
        return book.retireWithFund(owner, amount, reliefScaled, carrying);
    }
    function send(address from, address to, uint256 amount, bool fc, bool tc) external returns (uint256) {
        return book.transfer(from, to, amount, fc, tc);
    }
    function payFundNet(address to, uint256 amount, bool carrying) external returns (uint256, uint256) {
        return book.payFundNet(to, amount, carrying);
    }
    function balanceOf(address owner) external view returns (uint256) { return book.balanceOf(owner); }
    function netOf(address owner) external view returns (uint256) { return book.netOf(owner); }
    function partsOf(address owner) external view returns (uint256, uint256) { return book.partsOf(owner); }
    function fundParts() external view returns (uint256, uint256) { return book.fundParts(); }
    function issued() external view returns (uint256) { return book.issued; }
    function maxNetPayout() external view returns (uint256) { return book.maxNetPayout(); }
    function fundNominalForNet(uint256 net) external view returns (uint256) { return book.fundNominalForNet(net); }
}

contract BuckLedgerTest is Test {
    uint256 constant Q = 1e36;
    uint256 constant YEAR = 365 days + 6 hours;
    address constant FUND = address(0xF00D);
    address constant ALICE = address(0xA11CE);
    address constant BOB = address(0xB0B);
    address constant POOL = address(0xB00);
    BuckLedgerHarness internal h;
    uint256 internal t0;

    function setUp() public {
        vm.warp(1_000_000);
        t0 = block.timestamp;
        h = new BuckLedgerHarness(FUND);
    }

    function _netConserved(address a, address b, address c) internal view {
        (uint256 ah, uint256 au) = h.partsOf(a);
        (uint256 bh, uint256 bu) = h.partsOf(b);
        (uint256 ch, uint256 cu) = h.partsOf(c);
        (, uint256 fundNet) = h.fundParts();
        assertApproxEqAbs(ah + au + bh + bu + ch + cu + fundNet, h.issued() * Q, Q);
    }

    function test_unspentIssuanceIsSpendableAndPaysDemurrageImmediately() public {
        h.issue(ALICE, 1_000_000e6, false);
        assertEq(h.issued(), 1_000_000e6);
        assertEq(h.balanceOf(ALICE), 1_000_000e6);
        (uint256 held, uint256 unspent) = h.partsOf(ALICE);
        assertEq(held, 0);
        assertEq(unspent, 1_000_000e6 * Q);
        vm.warp(t0 + YEAR);
        assertApproxEqAbs(h.balanceOf(ALICE), 980198673306, 1);
        assertApproxEqAbs(h.netOf(FUND), 19801326693, 1);
        assertEq(h.balanceOf(FUND), 20_000e6);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_receivingAndSpendingDoNotImplicitlyRetireCredit() public {
        h.issue(ALICE, 100e6, false);
        h.send(ALICE, BOB, 40e6, false, false);
        h.send(BOB, ALICE, 5e6, false, false);
        (uint256 held, uint256 unspent) = h.partsOf(ALICE);
        assertEq(held / Q, 5e6);
        assertEq(unspent / Q, 60e6);
        assertEq(h.balanceOf(ALICE), 65e6);
        assertEq(h.issued(), 100e6);
        h.send(ALICE, BOB, 50e6, false, false);
        (held, unspent) = h.partsOf(ALICE);
        assertEq(held, 0);
        assertEq(unspent / Q, 15e6);
        assertEq(h.issued(), 100e6);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_carryingPoolReturnsNominalWithItsAverageFee() public {
        h.issue(ALICE, 100e6, false);
        h.send(ALICE, POOL, 10e6, false, true);
        vm.warp(t0 + YEAR);
        assertEq(h.balanceOf(POOL), 10e6);
        assertApproxEqAbs(h.netOf(POOL), 9801986, 1);
        uint256 delivered = h.send(POOL, BOB, 10e6, true, false);
        assertApproxEqAbs(delivered / Q, 9801986, 1);
        assertEq(h.balanceOf(POOL), 0);
        assertApproxEqAbs(h.balanceOf(BOB), 9801986, 1);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_carryingTransfersPreserveMixedAgeAndFullSpendability() public {
        h.issue(ALICE, 100e6, false);
        h.send(ALICE, POOL, 10e6, false, true);
        vm.warp(t0 + YEAR);
        h.send(ALICE, POOL, 10e6, false, true);
        assertEq(h.balanceOf(POOL), 20e6);
        uint256 net = h.netOf(POOL);
        assertApproxEqAbs(net, 19801986, 1);
        uint256 sent = h.send(POOL, BOB, 10e6, true, false);
        assertApproxEqAbs(sent / Q, net / 2, 1);
        assertEq(h.balanceOf(POOL), 10e6);
        assertApproxEqAbs(h.netOf(POOL), net / 2, 1);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_fundGrossUpSuppliesFullNetAndDoesNotCreateMoney() public {
        h.issue(ALICE, 100e6, false);
        vm.warp(t0 + 50 * YEAR);
        assertEq(h.balanceOf(FUND), 100e6);
        assertApproxEqAbs(h.netOf(FUND), 63212055, 1);
        (uint256 nominal, uint256 delivered) = h.payFundNet(ALICE, 40e6, false);
        assertGt(nominal, 40e6);
        assertGe(delivered, 40e6 * Q);
        assertGe(h.balanceOf(ALICE), 76e6);
        h.retire(ALICE, 70e6, false);
        assertEq(h.issued(), 30e6);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_fundSelfDemurrageIsGrossActivityNotNewNetValue() public {
        h.issue(ALICE, 100e6, false);
        vm.warp(t0 + YEAR);
        h.send(ALICE, FUND, h.balanceOf(ALICE), false, true);
        uint256 netBefore = h.netOf(FUND);
        uint256 grossBefore = h.balanceOf(FUND);
        vm.warp(t0 + 51 * YEAR);
        assertApproxEqAbs(h.netOf(FUND), netBefore, 1);
        assertEq(h.balanceOf(FUND) - grossBefore, 100e6);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_fundInheritsCarryingFeeWithoutCountingItAsNewNet() public {
        h.issue(ALICE, 100e6, false);
        h.send(ALICE, POOL, 10e6, false, true);
        vm.warp(t0 + YEAR);
        uint256 poolNet = h.netOf(POOL);
        uint256 fundNet = h.netOf(FUND);
        uint256 fundGross = h.balanceOf(FUND);
        h.send(POOL, FUND, 10e6, true, true);
        assertApproxEqAbs(h.netOf(FUND), fundNet + poolNet, 1);
        assertEq(h.balanceOf(FUND), fundGross + 10e6);
        assertEq(h.balanceOf(POOL), 0);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_idleMillionCanRetireAtFiftyYearsUsingAllAvailableFundCash() public {
        h.issue(ALICE, 1_000_000e6, false);
        vm.warp(t0 + 50 * YEAR);
        uint256 netAvailable = h.maxNetPayout();
        assertApproxEqAbs(netAvailable, 632120558829, 1);
        h.payFundNet(ALICE, netAvailable, false);
        assertEq(h.balanceOf(ALICE), 1_000_000e6);
        h.retire(ALICE, 1_000_000e6, false);
        assertEq(h.issued(), 0);
        assertEq(h.balanceOf(ALICE), 0);
        assertEq(h.netOf(FUND), 0);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_subRawOwnerAndFundBalancesCanCloseTogetherWithoutCreatingBuck() public {
        h.issue(ALICE, 3, false);
        vm.warp(t0 + 50 * YEAR);
        assertEq(h.maxNetPayout(), 1);
        h.payFundNet(ALICE, 1, false);
        assertEq(h.balanceOf(ALICE), 2);
        h.retire(ALICE, 2, false);
        assertEq(h.balanceOf(ALICE), 0);
        assertEq(h.maxNetPayout(), 0);
        (uint256 held, uint256 unspent) = h.partsOf(ALICE);
        (, uint256 fundNet) = h.fundParts();
        assertEq(held + unspent + fundNet, Q);
        h.retireWithFund(ALICE, 1, fundNet, false);
        assertEq(h.issued(), 0);
        (held, unspent) = h.partsOf(ALICE);
        (, fundNet) = h.fundParts();
        assertEq(held + unspent + fundNet, 0);
    }

    function test_combinedFundRetirementCannotOverdrawOrLeavePartialChanges() public {
        h.issue(ALICE, 100e6, false);
        vm.warp(t0 + YEAR);
        uint256 beforeBalance = h.balanceOf(ALICE);
        (uint256 gross, uint256 net) = h.fundParts();
        vm.expectRevert(BuckLedger.InsufficientBalance.selector);
        h.retireWithFund(ALICE, 100e6, net + 1, false);
        assertEq(h.balanceOf(ALICE), beforeBalance);
        (uint256 grossAfter, uint256 netAfter) = h.fundParts();
        assertEq(grossAfter, gross);
        assertEq(netAfter, net);
        assertEq(h.issued(), 100e6);
        h.retireWithFund(ALICE, 100e6, net, false);
        assertEq(h.issued(), 0);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_manyTouchesDoNotChangeSpendableBalanceByAWholeRawUnit() public {
        h.issue(ALICE, 1000e6, false);
        BuckLedgerHarness lazy = new BuckLedgerHarness(FUND);
        lazy.issue(ALICE, 1000e6, false);
        for (uint256 i = 1; i <= 100; ++i) {
            vm.warp(t0 + i * YEAR / 100);
            h.send(ALICE, BOB, 1, false, false);
            h.send(BOB, ALICE, 1, false, false);
        }
        assertApproxEqAbs(h.balanceOf(ALICE), lazy.balanceOf(ALICE), 1);
        assertApproxEqAbs(h.netOf(FUND), lazy.netOf(FUND), 1);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_fundCheckpointsBeforeIssuanceAndRetirementChanges() public {
        h.issue(ALICE, 100e6, false);
        vm.warp(t0 + YEAR);
        h.issue(BOB, 50e6, false);
        vm.warp(t0 + 3 * YEAR);
        assertEq(h.balanceOf(FUND), 8e6);
        uint256 available = h.maxNetPayout();
        h.payFundNet(ALICE, available / 2, false);
        h.retire(ALICE, 50e6, false);
        assertEq(h.issued(), 100e6);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_modesBoundsAndFailedOperationsDoNotChangeBalances() public {
        h.issue(ALICE, 100e6, false);
        vm.expectRevert(BuckLedger.CarryingModeChanged.selector);
        h.send(ALICE, BOB, 1, true, false);
        vm.expectRevert(BuckLedger.CarryingCannotIssue.selector);
        h.issue(POOL, 1, true);
        vm.expectRevert(BuckLedger.InsufficientBalance.selector);
        h.send(ALICE, BOB, 101e6, false, false);
        vm.expectRevert(BuckLedger.InsufficientIssued.selector);
        h.retire(ALICE, 101e6, false);
        assertEq(h.balanceOf(ALICE), 100e6);
        assertEq(h.issued(), 100e6);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_ordinaryAccountLimitAndInsufficientFundPayoutAreAtomic() public {
        h.issue(ALICE, BuckTypes.MAX_BALANCE, false);
        vm.expectRevert(BuckLedger.AccountOverflow.selector);
        h.issue(ALICE, 1, false);
        assertEq(h.issued(), BuckTypes.MAX_BALANCE);
        assertEq(h.balanceOf(ALICE), BuckTypes.MAX_BALANCE);
        vm.warp(t0 + YEAR);
        uint256 grossBefore = h.balanceOf(FUND);
        uint256 netBefore = h.netOf(FUND);
        uint256 maxPayable = h.maxNetPayout();
        vm.expectRevert(BuckLedger.InsufficientBalance.selector);
        h.payFundNet(ALICE, maxPayable + 1, false);
        assertEq(h.balanceOf(FUND), grossBefore);
        assertEq(h.netOf(FUND), netBefore);
        _netConserved(ALICE, BOB, POOL);
    }

    function test_zeroTransferDoesNotBindRecipientMode() public {
        h.issue(ALICE, 100e6, false);
        h.send(ALICE, POOL, 0, false, false);
        h.send(ALICE, POOL, 1, false, true);
        assertEq(h.balanceOf(POOL), 1);
        _netConserved(ALICE, BOB, POOL);
    }

    function testFuzz_mixedTransfersAndDemurrageConserveIssued(uint32 principal_, uint32 transfer_, uint24 days_) public {
        uint256 principal = bound(principal_, 2, 1_000_000) * 1e6;
        uint256 transfer = bound(transfer_, 1, principal / 2);
        uint256 elapsed = bound(days_, 0, 3650) * 1 days;
        h.issue(ALICE, principal, false);
        h.send(ALICE, POOL, transfer, false, true);
        vm.warp(t0 + elapsed);
        uint256 nominal = h.balanceOf(POOL) / 2;
        h.send(POOL, BOB, nominal, true, false);
        _netConserved(ALICE, BOB, POOL);
    }

    function testFuzz_fundQuoteIsTheSmallestNominalAmountDeliveringFullNet(uint64 principal_, uint64 requested_, uint16 days_)
        public
    {
        uint256 principal = bound(principal_, 1e6, 1e13);
        h.issue(ALICE, principal, false);
        vm.warp(t0 + bound(days_, 1, 10_000) * 1 days);
        uint256 maxPayout = h.maxNetPayout();
        uint256 requested = bound(requested_, 1, maxPayout);
        (uint256 gross, uint256 net) = h.fundParts();
        uint256 nominal = h.fundNominalForNet(requested);
        assertLe(nominal * Q, gross);
        assertLt(Math.mulDiv(net, (nominal - 1) * Q, gross), requested * Q);
        (uint256 paid, uint256 delivered) = h.payFundNet(BOB, requested, false);
        assertEq(paid, nominal);
        assertGe(delivered, requested * Q);
        _netConserved(ALICE, BOB, POOL);
    }
}
