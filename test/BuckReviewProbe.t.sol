// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";

/// @title BuckReviewProbe.t.sol -- scratch probes for the Buck.sol review.
///
/// @notice These are NOT assertions of intended behaviour; they are executable
///         probes that pin down what the code actually does today, so the
///         review's claims are measured rather than argued.
contract BuckReviewProbeTest is Test {

    Buck                  internal buck;
    BuckCreditHarness     internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV     = address(0xA0);
    address internal constant POOL    = address(0xBA51C);
    address internal constant INSURER = address(0x1551E1);

    address internal alice = address(0xA11CE);
    address internal bob   = address(0xB0B);
    address internal dave  = address(0xDA5E);

    function setUp() public {
        reg    = new IdentityRegistry(GOV);
        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);   // BUCK_K = 1.0
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        // Public-identity bindings on both sides so _identityCheckedTransfer's
        // receipt-fragment gate is satisfied without CP proofs.
        _bind(alice, /*carrying=*/false);   // non-Carrying: can draw on credit
        _bind(bob,   /*carrying=*/true);    // Carrying sink
        _bind(dave,  /*carrying=*/false);
    }

    function _bind(address target, bool carrying) internal {
        vm.etch(target, hex"60006000fd");
        reg.bindContract(
            target,
            BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}),
            /*isPublicIdentity=*/true,
            carrying
        );
    }

    /// @dev Give `holder` an activated credit position of `amount` without
    ///      going through mint()'s pool-principal purchase (harness path).
    function _activatedCredit(address holder, uint256 amount) internal returns (uint256 tid) {
        vm.prank(INSURER);
        tid = credit.createCredit(
            holder, 0, amount, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(holder);
        credit.forceActivate(tid, amount);
    }

    // ---------------------------------------------------------------------
    // Probe 1: does the per-block credit-limit cache ever populate?
    // ---------------------------------------------------------------------

    function test_probe_creditLimitCache_neverPopulates() public {
        _activatedCredit(alice, 1_000e6);

        assertEq(buck.creditLimit(alice), 1_000e6, "limit visible");

        // A live transfer is the hottest path there is: it reads balanceOf,
        // which reads creditLimit.  If the cache were wired, this would
        // stamp creditLimitBlock[alice] = block.number.
        vm.prank(alice);
        buck.transfer(bob, 100e6);

        assertEq(
            buck.creditLimitBlock(alice), 0,
            "cache block stamped -- _refreshCreditLimit is reachable after all"
        );
        assertEq(
            buck.creditLimitCache(alice), 0,
            "cache value written -- _refreshCreditLimit is reachable after all"
        );
    }

    /// @dev Cost of the uncached scan, as a function of NFT count.  Every
    ///      transfer out of a credit-backed account pays this.
    function test_probe_creditLimit_scanCost() public {
        for (uint256 i = 0; i < 10; i++) _activatedCredit(alice, 100e6);

        uint256 g0 = gasleft();
        buck.creditLimit(alice);
        uint256 used10 = g0 - gasleft();

        emit log_named_uint("creditLimit gas, 10 NFTs (NONE depreciation)", used10);

        // Same, but with DECLINING_BALANCE 40 years in: the per-NFT loop in
        // BuckCredit._depreciate compounds year by year.
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            dave, 0, 100e6, 1e6,
            BuckCredit.DepreciationType.DECLINING_BALANCE, 500, uint48(block.timestamp), 0
        );
        vm.prank(dave);
        credit.forceActivate(tid, 100e6);
        vm.warp(block.timestamp + 40 * 365 days);

        g0 = gasleft();
        buck.creditLimit(dave);
        uint256 usedDep = g0 - gasleft();
        emit log_named_uint("creditLimit gas, 1 NFT, 40yr declining-balance", usedDep);
    }

    // ---------------------------------------------------------------------
    // Probe 2: can a drawn credit position walk away from its collateral?
    // ---------------------------------------------------------------------

    function test_probe_drawnCredit_survivesNFTTransfer() public {
        uint256 tid = _activatedCredit(alice, 1_000e6);

        // Alice spends her entire headroom -- raw goes negative.
        vm.prank(alice);
        buck.transfer(bob, 1_000e6);

        assertEq(buck.signedRawBalanceOf(alice), -int256(1_000e6), "alice drew 1000");
        assertEq(buck.creditLimit(alice),        1_000e6,          "backed by her NFT");
        assertEq(buck.balanceOf(bob),            1_000e6,          "bob holds the BUCK");

        // Now Alice hands the collateral to Dave.  Plain transferFrom -- no
        // ERC721 receiver hook involved, nothing exotic.
        vm.prank(alice);
        credit.transferFrom(alice, dave, tid);

        emit log_named_int ("alice signed raw after NFT transfer", buck.signedRawBalanceOf(alice));
        emit log_named_uint("alice creditLimit after NFT transfer", buck.creditLimit(alice));
        emit log_named_uint("dave  creditLimit after NFT transfer", buck.creditLimit(dave));

        assertEq(buck.creditLimit(alice), 0,        "alice's collateral is gone");
        assertEq(buck.signedRawBalanceOf(alice), -int256(1_000e6),
                 "alice's drawn position survives, now uncollateralized");
        assertEq(buck.creditLimit(dave), 1_000e6,
                 "the SAME coverage now grants Dave fresh headroom");

        // And Dave can draw on it, so the same activated coverage has now
        // backed 2000 BUCK of issuance.
        vm.prank(dave);
        buck.transfer(bob, 1_000e6);
        assertEq(buck.balanceOf(bob), 2_000e6, "2000 BUCK backed by 1000 of coverage");
    }

    // ---------------------------------------------------------------------
    // Probe 3: balanceOf reports credit headroom as spendable ERC-20 balance.
    // ---------------------------------------------------------------------

    function test_probe_balanceOf_includesUndrawnCredit() public {
        _activatedCredit(alice, 5_000e6);
        assertEq(buck.rawBalanceOf(alice), 0,       "holds no BUCK at all");
        assertEq(buck.balanceOf(alice),    5_000e6, "but ERC-20 balanceOf says 5000");
    }
}
