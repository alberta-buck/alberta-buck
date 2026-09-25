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

/// @title BuckDelegatedDemurrage.t.sol -- an account's demurrage carried by a
///        designated payer.
///
/// The load-bearing property is conservation: delegation MOVES buck-seconds
/// between slots, it never destroys them.  Every test that matters here is a
/// comparison against an identical unsponsored control pair.
///
/// @dev Note the `_at()` time helper.  `block.timestamp` cannot change inside
///      a real transaction, so the via-IR optimiser is entitled to read it
///      once per function -- which silently turns a second
///      `vm.warp(block.timestamp + T)` in the same test body into a no-op.
///      All times here are computed off a single `t0` captured up front.
contract BuckDelegatedDemurrageTest is Test {

    Buck                  internal buck;
    BuckCreditHarness     internal credit;
    BuckKControllerStatic internal kCtrl;
    IdentityRegistry      internal reg;

    address internal constant GOV     = address(0xA0);
    address internal constant POOL    = address(0xBA51C);
    address internal constant INSURER = address(0x1551E1);

    address internal alice = address(0xA11CE);   // funding source (has credit)
    address internal bob   = address(0xB0B);     // sponsored
    address internal carol = address(0xCA401);   // bob's payer
    address internal dave  = address(0xDA5E);    // control for bob
    address internal erin  = address(0xE21);     // control for carol
    address internal pool  = address(0xF001);    // a Carrying account

    uint256 internal constant YEAR = 365 days + 6 hours;
    uint256 internal t0;

    function setUp() public {
        reg    = new IdentityRegistryHarness(GOV);
        credit = new BuckCreditHarness();
        kCtrl  = new BuckKControllerStatic(1e18, GOV);
        buck   = new Buck(address(credit), address(kCtrl), address(reg), POOL);
        vm.prank(GOV);
        reg.setBuck(address(buck));
        credit.setBuck(address(buck));

        _bind(alice, false);
        _bind(bob,   false);
        _bind(carol, false);
        _bind(dave,  false);
        _bind(erin,  false);
        _bind(pool,  true);

        // Alice draws on credit to seed everyone with positive raw BUCK.
        vm.prank(INSURER);
        uint256 tid = credit.createCredit(
            alice, 0, 1_000_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        vm.prank(alice);
        credit.forceActivate(tid, 1_000_000e6);

        t0 = block.timestamp;
    }

    function _bind(address target, bool carrying) internal {
        vm.etch(target, hex"60006000fd");
        reg.bindContract(
            target, BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}),
            /*isPublicIdentity=*/true, carrying
        );
    }

    /// @dev Warp to `t0 + offset`.  Never `block.timestamp + offset`.
    function _at(uint256 offset) internal { vm.warp(t0 + offset); }

    function _fund(address who, uint256 amount) internal {
        vm.prank(alice);
        buck.transfer(who, amount);
    }

    function _delegate(address account, address payer) internal {
        vm.prank(account);
        buck.requestDemurragePayer(payer);
        vm.prank(payer);
        buck.acceptDemurragePayer(account);
    }

    // ---------------------------------------------------------------------
    // Conservation -- the property everything else rests on
    // ---------------------------------------------------------------------

    /// @notice Delegation relocates the fee; it does not reduce the total.
    function test_totalFeeIsConserved() public {
        _fund(bob,   1_000e6);
        _fund(carol, 1_000e6);
        _fund(dave,  1_000e6);   // control for bob
        _fund(erin,  1_000e6);   // control for carol

        _delegate(bob, carol);
        _at(YEAR);
        buck.settleDemurrage(bob);          // hand bob's year to carol

        uint256 sponsored = buck.feeOwing(bob) + buck.feeOwing(carol);
        uint256 control   = buck.feeOwing(dave) + buck.feeOwing(erin);

        emit log_named_uint("bob   fee (sponsored)", buck.feeOwing(bob));
        emit log_named_uint("carol fee (payer)    ", buck.feeOwing(carol));
        emit log_named_uint("dave  fee (control)  ", buck.feeOwing(dave));
        emit log_named_uint("erin  fee (control)  ", buck.feeOwing(erin));

        assertEq(buck.feeOwing(bob), 0, "sponsored account carries no fee");

        // The fee is floor(buckSeconds * RATE / SCALE).  Pooling two accounts'
        // buck-seconds into one slot floors once instead of twice, so merging
        // can collect up to one raw unit (1e-6 BUCK) MORE per account merged.
        // The direction is what matters: rounding lands on the system's side
        // of the ledger, never the holder's, so delegation can never be used
        // to shave demurrage.
        assertGe(sponsored, control,     "delegation must never collect less");
        assertLe(sponsored - control, 1, "and at most one raw unit more per merge");
    }

    /// @notice The sponsored account's spendable balance stops eroding; the
    ///         payer's absorbs both.
    function test_sponsoredBalanceDoesNotErode() public {
        _fund(bob,   1_000e6);
        _fund(carol, 1_000e6);
        _fund(dave,  1_000e6);

        _delegate(bob, carol);
        _at(YEAR);
        buck.settleDemurrage(bob);

        uint256 oneYearsFee = 1_000e6 - buck.balanceOf(dave);

        assertEq(buck.balanceOf(bob), 1_000e6, "sponsored balance intact");
        assertLt(buck.balanceOf(dave), 1_000e6, "control eroded");
        assertApproxEqAbs(
            buck.balanceOf(carol), 1_000e6 - oneYearsFee * 2, 1,
            "payer eats exactly two accounts' worth"
        );
        assertLe(
            buck.balanceOf(carol), 1_000e6 - oneYearsFee * 2,
            "rounding favours the system, not the payer"
        );
    }

    /// @notice Settlement writes through to storage, not just to the view.
    function test_routingSurvivesCrystallisation() public {
        _fund(bob,   1_000e6);
        _fund(carol, 1_000e6);
        _delegate(bob, carol);

        _at(YEAR);
        // Any touch of bob crystallises him; his rectangle lands on carol.
        vm.prank(bob);
        buck.transfer(pool, 1e6);

        _at(YEAR + 1);
        assertEq(buck.feeOwing(bob), 0, "still nothing owing after crystallisation");
        assertApproxEqRel(buck.feeOwing(carol), 40e6, 1e15, "carol carries ~2%/yr on both");
    }

    /// @notice Stated semantics, not an accident: the payer's own view lags
    ///         until each sponsee is touched, because finding pending
    ///         sponsee exposure would mean enumerating sponsees.  The poke
    ///         exists precisely to close it.
    function test_payerViewLagsUntilSettled() public {
        _fund(bob,   1_000e6);
        _fund(carol, 1_000e6);
        _delegate(bob, carol);
        _at(YEAR);

        uint256 before = buck.feeOwing(carol);
        buck.settleDemurrage(bob);
        uint256 afterSettle = buck.feeOwing(carol);

        assertApproxEqRel(before,      20e6, 1e15, "pre-settle: carol sees only its own");
        assertApproxEqRel(afterSettle, 40e6, 1e15, "post-settle: carol sees both");
        assertEq(buck.feeOwing(bob), 0, "bob's view is correct throughout");
    }

    // ---------------------------------------------------------------------
    // The cap: delegation must not become an escape hatch
    // ---------------------------------------------------------------------

    /// @notice A payer can only absorb up to the point where its own fee
    ///         would exceed its balance.  Past that the exposure stays with
    ///         the sponsored account instead of evaporating.
    function test_payerCapacityCap_remainderStaysWithSponsee() public {
        _fund(bob,   1_000e6);
        _fund(carol,     1e6);   // a thousandth of what it is asked to carry
        _fund(dave,  1_000e6);
        _fund(erin,      1e6);

        _delegate(bob, carol);
        _at(YEAR);
        buck.settleDemurrage(bob);

        uint256 sponsored = buck.feeOwing(bob) + buck.feeOwing(carol);
        uint256 control   = buck.feeOwing(dave) + buck.feeOwing(erin);

        emit log_named_uint("bob   fee (overflowed back)", buck.feeOwing(bob));
        emit log_named_uint("carol fee (capped at raw)  ", buck.feeOwing(carol));

        assertLe(buck.feeOwing(carol), 1e6, "never liened beyond the payer's own balance");
        assertApproxEqAbs(buck.feeOwing(carol), 1e6, 1, "but liened right up to it");
        assertLe(buck.balanceOf(carol), 1,  "payer effectively consumed");
        assertGt(buck.feeOwing(bob),   0,   "overflow stayed with the sponsored account");
        assertGe(sponsored, control,        "still conserved once the payer is tapped out");
        assertLe(sponsored - control, 1,    "to within the same one-unit rounding");
    }

    /// @notice A payer with nothing at all absorbs nothing -- the sponsored
    ///         account is exactly as it would be undelegated.
    function test_emptyPayerAbsorbsNothing() public {
        _fund(bob,  1_000e6);
        _fund(dave, 1_000e6);
        _delegate(bob, carol);           // carol holds zero BUCK

        _at(YEAR);
        assertEq(buck.feeOwing(bob), buck.feeOwing(dave), "no free ride from an empty payer");
        buck.settleDemurrage(bob);
        assertEq(buck.feeOwing(bob), buck.feeOwing(dave), "and none after settlement either");
    }

    /// @notice Delegation cannot be used to park exposure where it will never
    ///         be collected: with the payer's balance drained to nothing the
    ///         sponsored account ends up owing exactly what it would have
    ///         owed alone.
    function test_cannotEscapeDemurrageByDrainingThePayer() public {
        _fund(bob,   1_000e6);
        _fund(carol, 1_000e6);
        _fund(dave,  1_000e6);

        _delegate(bob, carol);

        // Carol empties herself the moment the delegation is live.  (Compute
        // the amount first: vm.prank binds to the very next call, and
        // balanceOf would swallow it.)
        _at(1);
        uint256 all = buck.balanceOf(carol);
        vm.prank(carol);
        buck.transfer(pool, all);

        _at(YEAR);
        buck.settleDemurrage(bob);

        assertApproxEqRel(buck.feeOwing(bob), buck.feeOwing(dave), 1e12,
            "a drained payer leaves the sponsee exactly where it started");
    }

    // ---------------------------------------------------------------------
    // Consent and shape constraints
    // ---------------------------------------------------------------------

    function test_requiresRequestFromSponsee() public {
        vm.prank(carol);
        vm.expectRevert("BUCK: not requested");
        buck.acceptDemurragePayer(bob);
    }

    function test_requestAloneDoesNothing() public {
        _fund(bob,  1_000e6);
        _fund(dave, 1_000e6);
        vm.prank(bob);
        buck.requestDemurragePayer(carol);

        _at(YEAR);
        assertEq(buck.feeOwing(bob), buck.feeOwing(dave), "unaccepted request must be inert");
    }

    function test_rejectsSelfPayment() public {
        vm.prank(bob);
        vm.expectRevert("BUCK: self payer");
        buck.requestDemurragePayer(bob);
    }

    function test_rejectsCarryingPayer() public {
        vm.prank(bob);
        buck.requestDemurragePayer(pool);
        vm.prank(pool);
        vm.expectRevert("BUCK: payer is Carrying");
        buck.acceptDemurragePayer(bob);
    }

    function test_rejectsCarryingSponsee() public {
        vm.prank(pool);
        buck.requestDemurragePayer(carol);
        vm.prank(carol);
        vm.expectRevert("BUCK: account is Carrying");
        buck.acceptDemurragePayer(pool);
    }

    function test_rejectsUnverifiedParty() public {
        address stranger = address(0x5747);
        vm.prank(stranger);
        buck.requestDemurragePayer(carol);
        vm.prank(carol);
        vm.expectRevert("BUCK: account not verified");
        buck.acceptDemurragePayer(stranger);
    }

    function test_rejectsChain_payerCannotBeSponsored() public {
        _delegate(carol, erin);          // carol is sponsored by erin
        vm.prank(bob);
        buck.requestDemurragePayer(carol);
        vm.prank(carol);
        vm.expectRevert("BUCK: payer is sponsored");
        buck.acceptDemurragePayer(bob);
    }

    function test_rejectsChain_sponseeCannotBeAPayer() public {
        _delegate(dave, carol);          // carol already pays for dave
        vm.prank(carol);
        buck.requestDemurragePayer(erin);
        vm.prank(erin);
        vm.expectRevert("BUCK: account is a payer");
        buck.acceptDemurragePayer(carol);
    }

    function test_rejectsDoubleSponsorship() public {
        _delegate(bob, carol);
        vm.prank(bob);
        buck.requestDemurragePayer(erin);
        vm.prank(erin);
        vm.expectRevert("BUCK: already sponsored");
        buck.acceptDemurragePayer(bob);
    }

    /// @notice One payer, several accounts -- the shape the feature exists for.
    function test_onePayerCarriesSeveralAccounts() public {
        _fund(bob,   1_000e6);
        _fund(dave,  1_000e6);
        _fund(erin,  1_000e6);
        _fund(carol, 1_000e6);

        _delegate(bob,  carol);
        _delegate(dave, carol);
        _delegate(erin, carol);
        assertEq(buck.sponseeCount(carol), 3, "three sponsees");

        _at(YEAR);
        buck.settleDemurrage(bob);
        buck.settleDemurrage(dave);
        buck.settleDemurrage(erin);

        assertEq(buck.balanceOf(bob),  1_000e6, "bob intact");
        assertEq(buck.balanceOf(dave), 1_000e6, "dave intact");
        assertEq(buck.balanceOf(erin), 1_000e6, "erin intact");
        assertApproxEqRel(buck.feeOwing(carol), 80e6, 1e15, "carol carries all four");
    }

    // ---------------------------------------------------------------------
    // Teardown
    // ---------------------------------------------------------------------

    function test_eitherPartyMayClear() public {
        _delegate(bob, carol);
        vm.prank(bob);
        buck.clearDemurragePayer(bob);
        assertEq(buck.demurragePayer(bob), address(0), "sponsee may walk away");

        _delegate(bob, carol);
        vm.prank(carol);
        buck.clearDemurragePayer(bob);
        assertEq(buck.demurragePayer(bob), address(0), "payer may stop the bleeding");

        _delegate(bob, carol);
        vm.prank(dave);
        vm.expectRevert("BUCK: not a party");
        buck.clearDemurragePayer(bob);
    }

    /// @notice Clearing crystallises first, so the exposure accrued under the
    ///         delegation stays with the payer rather than snapping back.
    function test_clearCrystallisesBeforeDetaching() public {
        _fund(bob,   1_000e6);
        _fund(carol, 1_000e6);
        _delegate(bob, carol);

        _at(YEAR);
        vm.prank(bob);
        buck.clearDemurragePayer(bob);

        assertEq(buck.feeOwing(bob), 0, "the sponsored year does not snap back onto bob");
        assertApproxEqRel(buck.feeOwing(carol), 40e6, 1e15, "carol keeps what it took on");

        // From here bob pays his own way again.
        _at(2 * YEAR);
        assertApproxEqRel(buck.feeOwing(bob), 20e6, 1e15, "bob accrues alone after release");
    }

    /// @notice Sponsee count is released so a former payer can be sponsored.
    function test_clearReleasesPayerSlot() public {
        _delegate(bob, carol);
        assertEq(buck.sponseeCount(carol), 1, "carol carries one");
        vm.prank(carol);
        buck.clearDemurragePayer(bob);
        assertEq(buck.sponseeCount(carol), 0, "released");

        _delegate(carol, erin);          // now legal
        assertEq(buck.demurragePayer(carol), erin, "former payer may now be sponsored");
    }

    // ---------------------------------------------------------------------
    // Cost
    // ---------------------------------------------------------------------

    /// @notice The feature must be free for accounts that do not use it, and
    ///         its cost must be bounded for accounts that do.
    function test_gas_sponsoredVsUnsponsoredTransfer() public {
        _fund(bob,   1_000e6);
        _fund(carol, 1_000e6);
        _fund(dave,  1_000e6);
        _delegate(bob, carol);
        _at(30 days);

        // Warm both senders' and the sink's slots so the first pair of
        // measurements differ only by the routing work.
        vm.prank(dave); buck.transfer(pool, 1e6);
        vm.prank(bob);  buck.transfer(pool, 1e6);

        // Each measurement must have the SAME elapsed time to fold, or it is
        // measuring a no-op crystallisation rather than the routing.
        _at(60 days);
        vm.prank(dave);
        uint256 g0 = gasleft();
        buck.transfer(pool, 1e6);
        uint256 unsponsoredWarm = g0 - gasleft();

        vm.prank(bob);
        g0 = gasleft();
        buck.transfer(pool, 1e6);
        uint256 sponsoredWarm = g0 - gasleft();

        // The honest worst case: first BUCK touch of a transaction, so every
        // slot the routing needs is cold.
        _at(90 days);
        vm.cool(address(buck));
        vm.prank(bob);
        g0 = gasleft();
        buck.transfer(pool, 1e6);
        uint256 sponsoredCold = g0 - gasleft();

        _at(120 days);
        vm.cool(address(buck));
        vm.prank(dave);
        g0 = gasleft();
        buck.transfer(pool, 1e6);
        uint256 unsponsoredCold = g0 - gasleft();

        emit log_named_uint("transfer, unsponsored (warm)", unsponsoredWarm);
        emit log_named_uint("transfer, sponsored   (warm)", sponsoredWarm);
        emit log_named_int ("delta                 (warm)",
            int256(sponsoredWarm) - int256(unsponsoredWarm));
        emit log_named_uint("transfer, unsponsored (cold)", unsponsoredCold);
        emit log_named_uint("transfer, sponsored   (cold)", sponsoredCold);
        emit log_named_int ("delta                 (cold)",
            int256(sponsoredCold) - int256(unsponsoredCold));

        // The unsponsored path must not pay for a feature it does not use.
        // Proof that it does not is the gas-snapshot diff across this change
        // (see .gas-snapshot); here we only pin that routing stays cheap.
        assertLt(sponsoredWarm - unsponsoredWarm, 1500,
            "routing a warm payer should cost about one extra slot's traffic");
    }
}
