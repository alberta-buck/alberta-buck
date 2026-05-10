// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/BuckKControllerStatic.sol";

contract BuckKControllerStaticTest is Test {
    BuckKControllerStatic ctrl;
    address governance = makeAddr("governance");
    address attacker   = makeAddr("attacker");

    uint256 constant ONE = 1e18;

    function setUp() public {
        ctrl = new BuckKControllerStatic(ONE, governance);
    }

    function test_initialState() public {
        assertEq(ctrl.buckK(), ONE);
        assertEq(ctrl.currentBuckK(), ONE);
        assertEq(ctrl.compute(), ONE);
        assertEq(ctrl.governance(), governance);
    }

    function test_constructor_rejectsZeroBuckK() public {
        vm.expectRevert(bytes("buckK=0"));
        new BuckKControllerStatic(0, governance);
    }

    function test_constructor_rejectsZeroGovernance() public {
        vm.expectRevert(bytes("governance=0"));
        new BuckKControllerStatic(ONE, address(0));
    }

    function test_setBuckK_governanceCanUpdate() public {
        vm.expectEmit(true, true, true, true);
        emit BuckKControllerStatic.BuckKSet(ONE, 1.5e18, governance);
        vm.prank(governance);
        ctrl.setBuckK(1.5e18);

        assertEq(ctrl.buckK(), 1.5e18);
        assertEq(ctrl.currentBuckK(), 1.5e18);
        assertEq(ctrl.compute(), 1.5e18);
    }

    function test_setBuckK_attackerReverts() public {
        vm.prank(attacker);
        vm.expectRevert(bytes("not governance"));
        ctrl.setBuckK(2e18);
    }

    function test_setBuckK_rejectsZero() public {
        vm.prank(governance);
        vm.expectRevert(bytes("buckK=0"));
        ctrl.setBuckK(0);
    }

    function test_transferGovernance() public {
        address next = makeAddr("nextGov");
        vm.expectEmit(true, true, true, true);
        emit BuckKControllerStatic.GovernanceTransferred(governance, next);
        vm.prank(governance);
        ctrl.transferGovernance(next);

        assertEq(ctrl.governance(), next);

        // old governance no longer authorized
        vm.prank(governance);
        vm.expectRevert(bytes("not governance"));
        ctrl.setBuckK(2e18);

        // new governance is
        vm.prank(next);
        ctrl.setBuckK(2e18);
        assertEq(ctrl.buckK(), 2e18);
    }

    function test_transferGovernance_rejectsZero() public {
        vm.prank(governance);
        vm.expectRevert(bytes("next=0"));
        ctrl.transferGovernance(address(0));
    }

    function testFuzz_setBuckK(uint256 v) public {
        vm.assume(v > 0);
        vm.prank(governance);
        ctrl.setBuckK(v);
        assertEq(ctrl.currentBuckK(), v);
    }
}
