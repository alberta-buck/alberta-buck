// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {IdentityRegistry}       from "../src/IdentityRegistry.sol";
import {IdentityRegistryHarness} from "./harness/IdentityRegistryHarness.sol";
import {Buck}                   from "../src/Buck.sol";
import {BuckCredit}             from "../src/BuckCredit.sol";
import {BuckCreditHarness}             from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic}  from "../src/BuckKControllerStatic.sol";

/// @title BuckCreditLimit.t.sol -- the live creditLimit() view, and the
///        transferability of the credits behind it.
///
/// @notice Exercises the live credit-limit machinery without going
///         through the identity-bound mint/transfer paths (which require
///         the BN254 PS-credential machinery exercised in Buck.t.sol).
///         Holders here are unverified addresses; we touch them only via
///         BuckCredit (createCredit / activate / transferFrom / updateCredit)
///         and Buck's view surface.
contract BuckCreditLimitTest is Test {

    Buck                  internal buck;
    BuckCreditHarness            internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV    = address(0xA0);
    address internal constant POOL   = address(0xBA51C);
    address internal constant ALICE  = address(0xA11CE);
    address internal constant BOB    = address(0xB0B);
    address internal constant INSURER = address(0x1551E1);

    function setUp() public {
        reg     = new IdentityRegistryHarness(GOV);
        credit  = new BuckCreditHarness();
        kCtrl   = new BuckKControllerStatic(1e18, GOV);   // BUCK_K = 1.0
        buck    = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));
        t0 = block.timestamp;
    }

    /// @dev Snapshot of the starting timestamp, taken in setUp and read back
    ///      from storage.  `block.timestamp` cannot change inside a real
    ///      transaction, so the via-IR optimiser folds every read in a
    ///      function to one TIMESTAMP -- which means a local
    ///      `uint256 start = block.timestamp` taken before a `vm.warp` is not
    ///      a snapshot at all, it is an alias that yields the warped value.
    ///      An SLOAD across the setUp boundary is.
    uint256 internal t0;

    /// @notice Newly-created NFT with no activation contributes 0 to creditLimit.
    function test_creditLimit_zero_for_unactivated_NFT() public {
        vm.prank(INSURER);
        credit.createCredit(
            ALICE, 0, 1_000_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        assertEq(buck.creditLimit(ALICE), 0, "unactivated NFT must give 0 credit");
    }

    /// @notice After activate(), creditLimit equals activatedValue * BUCK_K.
    function test_creditLimit_tracks_activation() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            ALICE, 0, 1_000_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(tid, 600_000e6);
        // BUCK_K = 1e18, totalCurrentValue = 600_000e6 raw, limit = same.
        assertEq(buck.creditLimit(ALICE), 600_000e6, "creditLimit must equal activated");
    }

    /// @notice creditLimit scales with BUCK_K.
    function test_creditLimit_scales_with_BUCK_K() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            ALICE, 0, 1_000_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(tid, 500_000e6);

        // Halve BUCK_K -- limit halves immediately, in the same block.  No
        // NFT mutation happens here, so nothing could have signalled the
        // change; the limit is right because it is read live.
        vm.prank(GOV);
        kCtrl.setBuckK(5e17);
        assertEq(buck.creditLimit(ALICE), 250_000e6, "creditLimit must halve with BUCK_K");
    }

    /// @notice The limit follows credits as they are acquired -- no signal,
    ///         no invalidation, just a live read of what the holder owns.
    function test_creditLimit_tracksAcquiredCredits() public {
        vm.prank(INSURER);
        credit.createCredit(
            ALICE, 0, 100_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(0, 100_000e6);
        assertEq(buck.creditLimit(ALICE), 100_000e6, "one credit");

        vm.prank(INSURER);
        credit.createCredit(
            ALICE, 0, 50_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(1, 50_000e6);
        assertEq(buck.creditLimit(ALICE), 150_000e6, "limit must include both NFTs");
    }

    /// @notice The limit follows natural depreciation, with nothing at all
    ///         happening on chain in between.
    function test_creditLimit_tracksDepreciation() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            ALICE, 0, 100_000e6, /*floor=*/0,
            BuckCredit.DepreciationType.LINEAR, /*1000bp/yr=*/1000,
            uint48(t0), 0
        );
        vm.prank(ALICE);
        credit.forceActivate(tid, 100_000e6);
        assertEq(buck.creditLimit(ALICE), 100_000e6, "undepreciated at t0");

        vm.warp(t0 + 365 days + 6 hours);
        assertEq(buck.creditLimit(ALICE), 90_000e6, "10%/yr off after one year");
    }

    /// @notice An unactivated credit moves freely, and the limit follows it.
    function test_unusedCredit_transfersAndCarriesItsLimit() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            ALICE, 0, 100_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.transferFrom(ALICE, BOB, tid);

        vm.prank(BOB);
        credit.forceActivate(tid, 100_000e6);
        assertEq(buck.creditLimit(ALICE), 0,         "ALICE never activated it");
        assertEq(buck.creditLimit(BOB),   100_000e6, "BOB owns and activated it");
    }

    /// @notice A credit that is backing BUCK cannot change hands.  This is
    ///         what stops a drawn position from walking away from its
    ///         collateral and letting the same coverage back BUCK twice.
    function test_activatedCredit_cannotTransfer() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            ALICE, 0, 100_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(tid, 100_000e6);

        vm.prank(ALICE);
        vm.expectRevert(bytes("BuckCredit: credit in use"));
        credit.transferFrom(ALICE, BOB, tid);

        vm.prank(ALICE);
        vm.expectRevert(bytes("BuckCredit: credit in use"));
        credit.safeTransferFrom(ALICE, BOB, tid);

        assertEq(credit.ownerOf(tid), ALICE, "still ALICE's");
    }

    /// @notice An insurer may reappraise down to the coverage the holder has
    ///         bought, and no further.
    function test_reappraisal_cannotUndercutActivatedCoverage() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            ALICE, 0, 100_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(tid, 40_000e6);

        // Down to the activated line: allowed.
        vm.prank(INSURER);
        credit.updateCredit(
            tid, 40_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        assertEq(buck.creditLimit(ALICE), 40_000e6, "coverage survives intact");

        // Below it: refused.
        vm.prank(INSURER);
        vm.expectRevert(bytes("BuckCredit: face below activated coverage"));
        credit.updateCredit(
            tid, 39_999e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
    }

    /// @notice A reappraisal that changes the schedule still moves the limit.
    function test_creditLimit_followsReappraisedSchedule() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            ALICE, 0, 100_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(tid, 100_000e6);

        vm.warp(t0 + 365 days + 6 hours);
        assertEq(buck.creditLimit(ALICE), 100_000e6, "not depreciating yet");

        // Same face, but now on a schedule that has been running for a year.
        vm.prank(INSURER);
        credit.updateCredit(
            tid, 100_000e6, 0,
            BuckCredit.DepreciationType.LINEAR, 1000, uint48(t0), 0
        );
        assertEq(buck.creditLimit(ALICE), 90_000e6, "limit follows the new schedule");
    }

    /// @notice BuckCredit.setBuck is one-shot.
    function test_setBuck_one_shot() public {
        vm.expectRevert(bytes("BuckCredit: buck already set"));
        credit.setBuck(address(0xDEAD));
    }
}
