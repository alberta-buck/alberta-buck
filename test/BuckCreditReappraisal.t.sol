// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {Buck} from "../src/Buck.sol";
import {BuckCredit} from "../src/BuckCredit.sol";
import {BuckCreditHarness} from "./harness/BuckCreditHarness.sol";
import {BuckKControllerStatic} from "../src/BuckKControllerStatic.sol";

/// @title BuckCreditReappraisal.t.sol -- reappraisal, the burn unwind, and
///        what happens to a position nobody closes.
///
/// Everything here goes through the real purchase path (Buck.mint), so
/// `mintsBacked` and `activatedValue` move together the way production does.
contract BuckCreditReappraisalTest is Test {

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

    uint256 internal constant YEAR = 365 days + 6 hours;
    uint256 internal t0;

    function setUp() public {
        reg    = new IdentityRegistry(GOV);
        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));
        _bind(alice, false);
        _bind(bob,   true);
        _bind(dave,  false);
        t0 = block.timestamp;
    }

    function _bind(address t, bool carrying) internal {
        vm.etch(t, hex"60006000fd");
        reg.bindContract(t, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}), true, carrying);
    }

    /// @dev Warp to t0 + offset.  Never block.timestamp + offset: the via-IR
    ///      optimiser reads block.timestamp once per function, so a second
    ///      relative warp in one test body silently does nothing.
    function _at(uint256 offset) internal { vm.warp(t0 + offset); }

    /// @dev A real purchase: pays the pool principal, moves mintsBacked and
    ///      activatedValue in lockstep.
    function _buy(uint256 face, uint32 premiumBp, uint256 amount)
        internal returns (uint256 tid)
    {
        vm.prank(INSURER);
        tid = credit.createCredit(
            alice, 0, face, 0, BuckCredit.DepreciationType.NONE, 0, 0, premiumBp
        );
        uint256[] memory ids = new uint256[](1);
        ids[0] = tid;
        vm.prank(alice);
        buck.mint(amount, ids);
    }

    function _ids(uint256 tid) internal pure returns (uint256[] memory ids) {
        ids = new uint256[](1);
        ids[0] = tid;
    }

    // ---------------------------------------------------------------------
    // Reappraisal
    // ---------------------------------------------------------------------

    /// @notice An insurer may not write down coverage the holder has already
    ///         bought.  Before this rule, the clamp moved BuckCredit's
    ///         activatedValue while Buck's mintsBacked stayed put, and the
    ///         holder could no longer unwind: the burn was sized from one
    ///         ledger and checked against the other.
    function test_reappraisal_cannotUndercutPurchasedCoverage() public {
        uint256 tid = _buy(2_000e6, 100, 900e6);

        (, uint256 activated,) = credit.creditInfo(tid);
        assertEq(activated, 1_000e6,           "coverage bought");
        assertEq(buck.mintsBacked(tid), 1_000e6, "and matched in Buck");

        vm.prank(INSURER);
        vm.expectRevert(bytes("BuckCredit: face below activated coverage"));
        credit.updateCredit(tid, 500e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 100);

        // Down to the activated line is fine, and the ledgers stay equal.
        vm.prank(INSURER);
        credit.updateCredit(tid, 1_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 100);
        (, activated,) = credit.creditInfo(tid);
        assertEq(activated, buck.mintsBacked(tid), "ledgers still in lockstep");
    }

    /// @notice The position closes cleanly, and the credit is transferable
    ///         again once it no longer backs anything.
    function test_position_closesAndReleasesTheCredit() public {
        uint256 tid = _buy(2_000e6, 100, 900e6);

        vm.prank(alice);
        vm.expectRevert(bytes("BuckCredit: credit in use"));
        credit.transferFrom(alice, dave, tid);

        vm.prank(alice);
        buck.burn(900e6, _ids(tid));

        (, uint256 activated,) = credit.creditInfo(tid);
        assertEq(activated,             0, "coverage released");
        assertEq(buck.mintsBacked(tid), 0, "and the backing with it");
        assertEq(buck.signedRawBalanceOf(alice), 0, "position square");

        vm.prank(alice);
        credit.transferFrom(alice, dave, tid);
        assertEq(credit.ownerOf(tid), dave, "credit is free to sell again");
    }

    // ---------------------------------------------------------------------
    // A position at its limit
    // ---------------------------------------------------------------------

    /// @notice Releasing coverage costs more limit than the refund repays, so
    ///         a holder at their limit must repay before they can unwind --
    ///         the same order as any loan.  Acquiring BUCK and receiving it
    ///         is the repayment.
    function test_fullyDrawnHolder_repaysThenCloses() public {
        uint256 tid = _buy(2_000e6, 100, 900e6);

        uint256 all = buck.balanceOf(alice);
        vm.prank(alice);
        buck.transfer(bob, all);                       // spend it all

        assertEq(uint256(-buck.signedRawBalanceOf(alice)), buck.creditLimit(alice),
                 "used == limit");

        vm.prank(alice);
        vm.expectRevert(bytes("BUCK: post-burn credit used exceeds limit"));
        buck.burn(1e6, _ids(tid));

        // Alice earns BUCK back and repays.  Now the unwind fits.
        vm.prank(bob);
        buck.transfer(alice, all);
        vm.prank(alice);
        buck.burn(all, _ids(tid));

        (, uint256 activated,) = credit.creditInfo(tid);
        assertEq(activated, 0, "closed after repayment");
    }

    /// @notice Doing nothing is a supported outcome.  The policy is paid up
    ///         and stays in force, and the Jubilee relief accruing on the
    ///         coverage shrinks what closing it costs, year on year, with no
    ///         action from the holder.
    function test_openPosition_decaysInFavourOfTheHolder() public {
        uint256 tid = _buy(2_000e6, 100, 900e6);

        (, uint256 activatedAtStart,) = credit.creditInfo(tid);
        uint256 costAtStart = credit.redeemCost(tid);
        assertEq(costAtStart, activatedAtStart, "nothing forgiven yet");

        _at(10 * YEAR);

        (, uint256 activatedLater,) = credit.creditInfo(tid);
        uint256 costLater = credit.redeemCost(tid);

        emit log_named_uint("coverage in force at t0     ", activatedAtStart);
        emit log_named_uint("coverage in force at t+10yr ", activatedLater);
        emit log_named_uint("redeemCost at t0            ", costAtStart);
        emit log_named_uint("redeemCost at t+10yr        ", costLater);

        assertEq(activatedLater, activatedAtStart, "insurance still in force");
        assertApproxEqRel(costLater, costAtStart * 8 / 10, 1e15,
                          "~2%/yr of the coverage forgiven by the Jubilee");
    }

    // ---------------------------------------------------------------------
    // Known gap: mint() under-delivers against a depreciated credit
    // ---------------------------------------------------------------------

    /// @notice Characterization, not an endorsement.  `_allocateMint` sizes
    ///         `take` off the *undepreciated* faceValue and grosses it up only
    ///         for the premium inversion, while `creditLimit` reads the
    ///         *depreciated* currentValue.  So a mint against a credit that
    ///         has depreciated hands the holder less spendable headroom than
    ///         they asked for -- short by exactly the depreciation factor on
    ///         the coverage taken.
    ///
    ///         Delivering the full amount would mean grossing `take` up by
    ///         face/depreciatedFace as well, which charges premium on the
    ///         larger coverage.  Whether that is the right economics is a
    ///         design question, so this test pins today's behaviour rather
    ///         than asserting a fix.
    function test_knownGap_mintUnderDeliversAgainstDepreciatedCredit() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            alice, 0, 300_000e6, 60_000e6,
            BuckCredit.DepreciationType.LINEAR, 200, uint48(t0), 200
        );
        _at(3 * YEAR);                       // 3 yr x 200bp on (300k - 60k) = 14.4k off face

        vm.prank(alice);
        buck.mint(50_000e6, _ids(tid));

        (, uint256 activated,) = credit.creditInfo(tid);
        assertEq(activated, 62_500e6, "take grossed up for the 200bp premium only");
        assertEq(buck.creditLimit(alice), 59_500e6, "limit reads the depreciated value");
        assertEq(buck.signedRawBalanceOf(alice), -int256(12_500e6), "principal paid");

        assertEq(buck.balanceOf(alice), 47_000e6, "delivered");
        assertLt(buck.balanceOf(alice), 50_000e6, "...which is short of the 50,000 requested");

        // The shortfall is exactly the depreciation applied to the coverage.
        assertEq(50_000e6 - buck.balanceOf(alice), 62_500e6 * 144 / 3000, "= take * 14.4k/300k");
    }

    /// @notice Control: with no depreciation the identity holds exactly.
    function test_mintDeliversExactlyAgainstNonDepreciatingCredit() public {
        uint256 tid = _buy(300_000e6, 200, 50_000e6);
        assertEq(buck.balanceOf(alice), 50_000e6, "delivered exactly what was asked");
        assertEq(buck.creditLimit(alice), 62_500e6, "limit == activated, undepreciated");
        tid;
    }

    // ---------------------------------------------------------------------
    // Liveness of the limit
    // ---------------------------------------------------------------------

    /// @notice BUCK_K moves inside any mint or burn that advances the PID,
    ///         from any account, with nothing to announce it.  The limit and
    ///         the balance follow it in the same block because they are read
    ///         live, not because something invalidated a copy.
    function test_creditLimit_followsBuckKWithinABlock() public {
        _buy(2_000e6, 0, 1_000e6);

        uint256 limitBefore = buck.creditLimit(alice);
        uint256 balBefore   = buck.balanceOf(alice);

        vm.prank(GOV);
        kCtrl.setBuckK(5e17);

        assertEq(buck.creditLimit(alice), limitBefore / 2, "limit halves at once");
        assertLt(buck.balanceOf(alice),   balBefore,       "and so does the balance");
    }
}
