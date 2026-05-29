// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {IdentityRegistry}       from "../src/IdentityRegistry.sol";
import {Buck}                   from "../src/Buck.sol";
import {BuckCredit}             from "../src/BuckCredit.sol";
import {BuckCreditHarness}             from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic}  from "../src/BuckKControllerStatic.sol";

/// @title BuckCreditLimit.t.sol -- Phase 1a creditLimit() view + per-block
///        cache invalidation tests.
///
/// @notice Exercises the new live credit-limit machinery without going
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
        reg     = new IdentityRegistry(GOV);
        credit  = new BuckCreditHarness();
        kCtrl   = new BuckKControllerStatic(1e18, GOV);   // BUCK_K = 1.0
        buck    = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));
    }

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

        // Halve BUCK_K -- limit should halve.
        vm.prank(GOV);
        kCtrl.setBuckK(5e17);
        // Invalidate cache so the next read reflects the new K.  (The
        // controller change does not fire a BuckCredit hook; only NFT
        // mutations do.  The view path computes fresh when uncached.)
        // Roll to a new block so the cache is naturally stale.
        vm.roll(block.number + 1);
        assertEq(buck.creditLimit(ALICE), 250_000e6, "creditLimit must halve with BUCK_K");
    }

    /// @notice Cache invalidation: a fresh NFT mint to ALICE invalidates her cache.
    function test_cache_invalidates_on_NFT_mint() public {
        // Prime: create + activate one NFT; touch the cache.
        vm.prank(INSURER);
        credit.createCredit(
            ALICE, 0, 100_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(0, 100_000e6);
        // Drive a cache write by calling _refresh-equivalent via a known
        // path: creditLimit() view alone doesn't persist; instead use the
        // public mapping to assert raw cache state.
        assertEq(buck.creditLimitBlock(ALICE), 0, "no path persisted yet");

        // Now create a second NFT for Alice -- the hook fires _update,
        // which calls onCreditMutation(0, ALICE).  The cache for ALICE is
        // invalidated (block reset to 0).  Since it was 0 anyway this is
        // a no-op-but-confirms-no-revert test.
        vm.prank(INSURER);
        credit.createCredit(
            ALICE, 0, 50_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        // Activate the new NFT -- another hook fire.
        vm.prank(ALICE);
        credit.forceActivate(1, 50_000e6);
        assertEq(buck.creditLimit(ALICE), 150_000e6, "limit must include both NFTs");
    }

    /// @notice Transferring an NFT invalidates the cache for both parties.
    function test_cache_invalidates_on_NFT_transfer() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            ALICE, 0, 100_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(tid, 100_000e6);
        // Confirm starting state.
        assertEq(buck.creditLimit(ALICE), 100_000e6);
        assertEq(buck.creditLimit(BOB),   0);

        // Transfer -- both caches invalidate; new reads reflect the move.
        vm.prank(ALICE);
        credit.transferFrom(ALICE, BOB, tid);
        assertEq(buck.creditLimit(ALICE), 0,         "ALICE loses credit on transfer");
        assertEq(buck.creditLimit(BOB),   100_000e6, "BOB gains credit on transfer");
    }

    /// @notice updateCredit() by the insurer also invalidates the cache.
    function test_cache_invalidates_on_updateCredit() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            ALICE, 0, 100_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(ALICE);
        credit.forceActivate(tid, 100_000e6);
        assertEq(buck.creditLimit(ALICE), 100_000e6);

        // Insurer reappraises to a lower face -- activated caps down and
        // totalCurrentValue drops.  The hook fires from updateCredit.
        vm.prank(INSURER);
        credit.updateCredit(
            tid, 40_000e6, 0,
            BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        assertEq(buck.creditLimit(ALICE), 40_000e6, "limit must follow reappraisal");
    }

    /// @notice onCreditMutation is gated on the BuckCredit caller.
    function test_onCreditMutation_restricted_to_credit() public {
        vm.expectRevert(bytes("BUCK: not credit"));
        buck.onCreditMutation(ALICE, BOB);
    }

    /// @notice BuckCredit.setBuck is one-shot.
    function test_setBuck_one_shot() public {
        vm.expectRevert(bytes("BuckCredit: buck already set"));
        credit.setBuck(address(0xDEAD));
    }
}
