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
import {bindCarryingPool} from "./harness/CarryingPool.sol";

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
        reg    = new IdentityRegistryHarness(GOV);
        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        bindCarryingPool(reg, POOL);
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
    ///         lien -- the BUCK the credit actually put into circulation --
    ///         shrinks what closing it costs, year on year, with no action
    ///         from the holder.
    function test_openPosition_decaysInFavourOfTheHolder() public {
        uint256 tid = _buy(2_000e6, 100, 900e6);
        uint256 spendable = buck.balanceOf(alice);
        vm.prank(alice);
        buck.transfer(bob, spendable);                  // issue it: spend it all
        uint256 lien = uint256(-buck.signedRawBalanceOf(alice));

        (, uint256 activatedAtStart,) = credit.creditInfo(tid);
        uint256 costAtStart = buck.redeemCost(alice);
        assertEq(costAtStart, lien, "nothing forgiven yet");

        _at(10 * YEAR);

        (, uint256 activatedLater,) = credit.creditInfo(tid);
        uint256 costLater = buck.redeemCost(alice);

        emit log_named_uint("coverage in force at t0     ", activatedAtStart);
        emit log_named_uint("coverage in force at t+10yr ", activatedLater);
        emit log_named_uint("redeemCost at t0            ", costAtStart);
        emit log_named_uint("redeemCost at t+10yr        ", costLater);

        assertEq(activatedLater, activatedAtStart, "insurance still in force");
        assertApproxEqRel(costLater, lien * 8 / 10, 1e15,
                          "~2%/yr of the lien forgiven by the Jubilee");
    }

    // ---------------------------------------------------------------------
    // Minting against a depreciated credit
    // ---------------------------------------------------------------------

    /// @dev A 300,000 structure, LINEAR 200bp/yr against a 60,000 floor,
    ///      three years in: 3 x 200bp of (300,000 - 60,000) = 14,400 off, so
    ///      the appraisal today is 285,600 and rho = 0.952.
    function _depreciatedCredit() internal returns (uint256 tid) {
        vm.prank(INSURER);
        tid = credit.createCredit(
            alice, 0, 300_000e6, 60_000e6,
            BuckCredit.DepreciationType.LINEAR, 200, uint48(t0), 200
        );
        _at(3 * YEAR);
    }

    /// @notice The holder gets what they asked for.  The allocator inverts in
    ///         present insured value and grosses the face units back up by
    ///         face/depreciatedFace, so depreciation costs the holder capacity
    ///         on the credit -- not headroom on the mint.
    function test_mintDeliversInFullAgainstADepreciatedCredit() public {
        uint256 tid = _depreciatedCredit();
        assertEq(credit.depreciatedFaceValue(tid), 285_600e6, "rho = 285,600/300,000");

        vm.prank(alice);
        buck.mint(50_000e6, _ids(tid));

        assertEq(buck.balanceOf(alice), 50_000e6, "delivered exactly what was asked");

        // V = ceil(50,000 * 10000/8000) = 62,500 of *present* insured value,
        // carried by ceil(62,500 * 300,000/285,600) face units.
        (, uint256 activated,) = credit.creditInfo(tid);
        assertEq(activated, 65_651_260_505, "face units grossed up by 1/rho");
        assertEq(buck.creditLimit(alice), 62_500e6, "present insured value");
        assertEq(buck.signedRawBalanceOf(alice), -int256(12_500e6), "principal on present value");
    }

    /// @notice The premium is charged on what is actually insured, not on the
    ///         face slice the coverage is denominated in.  The pool principal
    ///         at the assumed 10% ROI funds exactly the annual premium on the
    ///         present insured value, in perpetuity.
    function test_premiumIsChargedOnPresentInsuredValue() public {
        uint256 tid = _depreciatedCredit();
        vm.prank(alice);
        buck.mint(50_000e6, _ids(tid));

        uint256 principal    = uint256(-buck.signedRawBalanceOf(alice));
        uint256 insuredValue = buck.creditLimit(alice);          // 62,500
        (, uint256 activated,) = credit.creditInfo(tid);         // 65,651.26 face units

        assertEq(principal * 10 / 100, insuredValue * 200 / 10_000,
                 "principal yield == premium on the insured value");
        assertGt(activated, insuredValue, "and NOT on the larger face slice");
        assertLt(principal * 10 / 100, activated * 200 / 10_000,
                 "which would have over-charged the holder");
    }

    /// @notice Cost per BUCK delivered does not depend on how old the asset
    ///         is -- which is why cheapest-first by premiumRate stays the
    ///         right selector.
    function test_premiumCostPerBuckIsIndependentOfDepreciation() public {
        uint256 tid = _depreciatedCredit();
        vm.prank(alice);
        buck.mint(50_000e6, _ids(tid));
        uint256 depreciatedCost = uint256(-buck.signedRawBalanceOf(alice));

        // Same face, same rate, no depreciation, different holder.
        vm.prank(INSURER);
        uint256 fresh = credit.createCredit(
            dave, 0, 300_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 200
        );
        vm.prank(dave);
        buck.mint(50_000e6, _ids(fresh));
        uint256 freshCost = uint256(-buck.signedRawBalanceOf(dave));

        assertEq(depreciatedCost, freshCost, "same premium for the same headroom");
    }

    /// @notice Mint then burn the same amount against the same credit cancels
    ///         exactly: no rounding drift to arbitrage.
    function test_mintBurnRoundTripIsExactOnADepreciatedCredit() public {
        uint256 tid = _depreciatedCredit();

        vm.prank(alice);
        buck.mint(50_000e6, _ids(tid));
        vm.prank(alice);
        buck.burn(50_000e6, _ids(tid));

        (, uint256 activated,) = credit.creditInfo(tid);
        assertEq(activated, 0, "coverage fully released");
        assertEq(buck.mintsBacked(tid), 0, "backing released with it");
        assertEq(buck.signedRawBalanceOf(alice), 0, "principal fully refunded");
    }

    /// @notice The deposit is returnable in full, whatever the appraisal did
    ///         in between.  Cover bought when the asset was worth 100,000 and
    ///         released when it is worth 50,000 still returns every unit of
    ///         principal: the pool was already paid for holding an over-sized
    ///         deposit against shrinking cover -- it earned its assumed ROI on
    ///         the whole deposit while owing premium only on what was still
    ///         insured -- so keeping the surplus principal as well would be
    ///         helping itself twice from one decline.
    function test_depositReturnsInFullAfterDepreciation() public {
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            alice, 0, 100_000e6, 0,
            BuckCredit.DepreciationType.LINEAR, 1000, uint48(t0), 200
        );
        vm.prank(alice);
        buck.mint(10_000e6, _ids(tid));

        uint256 paid = buck.mintsPrincipal(tid);
        assertEq(paid, 2_500e6, "deposit = 20% of the 12,500 insured");
        assertEq(uint256(-buck.signedRawBalanceOf(alice)), paid, "and it was debited");
        assertEq(buck.rawBalanceOf(POOL), paid, "the pool holds it");

        _at(5 * YEAR);
        assertEq(credit.depreciatedFaceValue(tid), 50_000e6, "appraisal halved");
        assertEq(buck.creditLimit(alice), 6_250e6, "so the cover halved too");

        // Her whole spendable is exactly what the cover can release, because
        // the deposit comes back with it: 6,250 of cover less the 2,500 held.
        uint256 spendable = buck.balanceOf(alice);
        assertEq(spendable, 3_750e6, "capV - deposit");

        vm.prank(alice);
        buck.burn(spendable, _ids(tid));

        assertEq(buck.mintsPrincipal(tid), 0, "deposit fully returned");
        assertEq(buck.mintsBacked(tid),    0, "cover fully released");
        assertEq(buck.rawBalanceOf(POOL),  0, "the pool gave back every unit");
        // Alice ends square.  Her lien was the deposit; the refund carries
        // the deposit's five years of demurrage and pays that fee on
        // arrival, and the relief five years of carrying the lien accrued
        // pays exactly the rest: the Jubilee's two sides balance.
        assertEq(buck.signedRawBalanceOf(alice), 0, "no principal lost to depreciation");
    }

    /// @notice A non-depreciating credit is unaffected: with rho == 1 the
    ///         gross-up is the identity.
    function test_nonDepreciatingCreditIsUnchanged() public {
        _buy(300_000e6, 200, 50_000e6);
        assertEq(buck.balanceOf(alice), 50_000e6, "delivered exactly what was asked");
        assertEq(buck.creditLimit(alice), 62_500e6, "limit == activated, undepreciated");
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
