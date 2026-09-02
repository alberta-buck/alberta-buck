// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckCredit} from "../src/BuckCredit.sol";

/// @title BuckCreditIssuance.t.sol -- who may put a credit into whose hands.
///
/// Deliberately against the production contract, not `BuckCreditHarness`:
/// the harness stands the opt-in down so fixtures can hand credits to
/// addresses that never transact, so it is the wrong place to test the gate.
contract BuckCreditIssuanceTest is Test {

    BuckCredit internal credit;

    address internal constant INSURER = address(0x1551E1);
    address internal constant OTHER   = address(0x07AE2);         // second insurer
    address internal constant ALICE   = address(0xA11CE);

    function setUp() public {
        credit = new BuckCredit();
    }

    function _create(address insurer, address client) internal returns (uint256) {
        vm.prank(insurer);
        return credit.createCredit(
            client, 0, 1_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
    }

    /// @notice The whole point: a credit cannot be pushed onto somebody who
    ///         did not ask for it.  Unsolicited credits are not harmless --
    ///         each one is walked by `totalCurrentValue` on every outbound
    ///         BUCK transfer the holder ever makes.
    function test_unsolicitedIssuanceIsRefused() public {
        vm.prank(INSURER);
        vm.expectRevert(bytes("BuckCredit: insurer not accepted by client"));
        credit.createCredit(
            ALICE, 0, 1_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        assertEq(credit.balanceOf(ALICE), 0, "nothing landed");
    }

    function test_issuanceSucceedsAfterOptIn() public {
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);

        uint256 tid = _create(INSURER, ALICE);
        assertEq(credit.ownerOf(tid), ALICE, "credit issued");
        assertEq(credit.balanceOf(ALICE), 1, "and held");
    }

    /// @notice Accepting one insurer does not accept the rest.
    function test_optInIsPerInsurer() public {
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);

        vm.prank(OTHER);
        vm.expectRevert(bytes("BuckCredit: insurer not accepted by client"));
        credit.createCredit(
            ALICE, 0, 1_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
    }

    /// @notice Revocable, and it governs only new issuance -- credits already
    ///         held stay put, because they may be backing BUCK.
    function test_optInIsRevocableAndOnlyGovernsNewIssuance() public {
        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, true);
        uint256 tid = _create(INSURER, ALICE);

        vm.prank(ALICE);
        credit.setCreditIssuer(INSURER, false);

        vm.prank(INSURER);
        vm.expectRevert(bytes("BuckCredit: insurer not accepted by client"));
        credit.createCredit(
            ALICE, 0, 1_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );
        assertEq(credit.ownerOf(tid), ALICE, "the credit already held is untouched");
    }

    /// @notice The rule is uniform: naming yourself the insurer does not
    ///         exempt you.  There is no reading of "I am my own insurer" that
    ///         needs a carve-out, and not opening one keeps the invariant
    ///         statable in a line.
    function test_selfIssuanceIsAlsoGated() public {
        vm.prank(ALICE);
        vm.expectRevert(bytes("BuckCredit: insurer not accepted by client"));
        credit.createCredit(
            ALICE, 0, 1_000e6, 0, BuckCredit.DepreciationType.NONE, 0, 0, 0
        );

        vm.prank(ALICE);
        credit.setCreditIssuer(ALICE, true);
        uint256 tid = _create(ALICE, ALICE);
        assertEq(credit.ownerOf(tid), ALICE, "explicit, and now allowed");
    }

    /// @notice A client can take on many insurers.
    function test_multipleInsurersMayBeAccepted() public {
        vm.startPrank(ALICE);
        credit.setCreditIssuer(INSURER, true);
        credit.setCreditIssuer(OTHER, true);
        vm.stopPrank();

        _create(INSURER, ALICE);
        _create(OTHER, ALICE);
        assertEq(credit.balanceOf(ALICE), 2, "both landed");
    }
}
