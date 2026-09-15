// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";

/// @title BuckCreditBacking.t.sol -- what backs a BUCK balance, and what it
///        costs to read it.
///
/// The invariant under test: activated coverage backs BUCK exactly once.  A
/// holder who has drawn against a credit cannot hand that credit to somebody
/// else and leave the obligation behind.
contract BuckCreditBackingTest is Test {

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
        reg    = new IdentityRegistryHarness(GOV);
        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);   // BUCK_K = 1.0
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        _bind(alice, /*carrying=*/false);   // non-Carrying: can draw on credit
        _bind(bob,   /*carrying=*/true);    // Carrying sink
        _bind(dave,  /*carrying=*/false);
        t0 = block.timestamp;
    }

    /// @dev Starting timestamp, snapshotted through storage.  A local
    ///      `block.timestamp` taken before a `vm.warp` is not a snapshot --
    ///      via-IR folds every read in a function to one TIMESTAMP, so the
    ///      local yields the warped value.  An SLOAD across setUp does not.
    uint256 internal t0;

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
    // The backing invariant
    // ---------------------------------------------------------------------

    /// @notice A drawn position cannot walk away from its collateral.  Before
    ///         the transfer lock this left the seller with an obligation
    ///         backed by nothing and handed the buyer headroom against
    ///         coverage that had already been spent -- 2,000 BUCK issued
    ///         against 1,000 of coverage.
    function test_drawnCredit_cannotTransferItsCollateral() public {
        uint256 tid = _activatedCredit(alice, 1_000e6);

        vm.prank(alice);
        buck.transfer(bob, 1_000e6);

        assertEq(buck.signedRawBalanceOf(alice), -int256(1_000e6), "alice drew 1000");
        assertEq(buck.creditLimit(alice),        1_000e6,          "backed by her credit");
        assertEq(buck.balanceOf(bob),            1_000e6,          "bob holds the BUCK");

        vm.prank(alice);
        vm.expectRevert(bytes("BuckCredit: credit in use"));
        credit.transferFrom(alice, dave, tid);

        assertEq(credit.ownerOf(tid),     alice, "collateral stays put");
        assertEq(buck.creditLimit(alice), 1_000e6, "and keeps backing her position");
        assertEq(buck.creditLimit(dave),  0,       "dave gains nothing");
    }

    /// @notice The lock is on use, not on ownership: an idle credit moves.
    function test_unusedCredit_movesFreely() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            alice, 0, 1_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(alice);
        credit.transferFrom(alice, dave, tid);
        assertEq(credit.ownerOf(tid), dave, "idle credit is transferable");
    }

    // ---------------------------------------------------------------------
    // What balanceOf means, and what it costs
    // ---------------------------------------------------------------------

    /// @notice Intended, not incidental: an account's spendable balance is
    ///         held BUCK plus the headroom its insured assets back.  Acquire
    ///         a credit and balanceOf rises with no transfer having occurred.
    function test_balanceOf_includesUndrawnCredit() public {
        assertEq(buck.balanceOf(alice), 0, "nothing held, nothing backed");

        _activatedCredit(alice, 5_000e6);
        assertEq(buck.rawBalanceOf(alice), 0,       "still holds no BUCK");
        assertEq(buck.balanceOf(alice),    5_000e6, "but 5000 is spendable against the asset");

        _activatedCredit(alice, 2_500e6);
        assertEq(buck.balanceOf(alice), 7_500e6, "a second asset raises it again");
    }

    /// @notice And it falls as the assets behind it depreciate, with nothing
    ///         happening on chain in between.
    function test_balanceOf_fallsWithDepreciation() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            alice, 0, 1_000e6, 0,
            BuckCredit.DepreciationType.LINEAR, 1000, uint48(t0), 0
        );
        vm.prank(alice);
        credit.forceActivate(tid, 1_000e6);
        assertEq(buck.balanceOf(alice), 1_000e6, "at t0");

        vm.warp(t0 + 365 days + 6 hours);
        assertEq(buck.balanceOf(alice), 900e6, "10%/yr off, unprompted");
    }

    /// @notice The price of that liveness, recorded rather than optimised
    ///         away: a credit-backed account pays a scan of its own credits
    ///         on every read, and an account with none pays almost nothing.
    function test_creditLimit_scanCost() public {
        for (uint256 i = 0; i < 10; i++) _activatedCredit(alice, 100e6);

        uint256 g0 = gasleft();
        buck.creditLimit(alice);
        uint256 tenCredits = g0 - gasleft();

        g0 = gasleft();
        buck.creditLimit(bob);
        uint256 noCredits = g0 - gasleft();

        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            dave, 0, 100e6, 1e6,
            BuckCredit.DepreciationType.DECLINING_BALANCE, 500, uint48(t0), 0
        );
        vm.prank(dave);
        credit.forceActivate(tid, 100e6);
        vm.warp(t0 + 40 * 365 days);

        g0 = gasleft();
        buck.creditLimit(dave);
        uint256 fortyYears = g0 - gasleft();

        emit log_named_uint("creditLimit, no credits                  ", noCredits);
        emit log_named_uint("creditLimit, 10 credits (NONE)           ", tenCredits);
        emit log_named_uint("creditLimit, 1 credit, 40yr declining    ", fortyYears);
    }
}
