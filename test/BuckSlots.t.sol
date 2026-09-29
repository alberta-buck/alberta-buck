// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";

import {Buck}      from "../src/Buck.sol";
import {BuckSlots} from "./harness/BuckSlots.sol";

/// @title BuckSlots.t.sol -- the slots tests reach by position are where
///        BuckSlots says.
///
/// @notice Each constant is pinned by writing through one side and reading
///         through the other: a storage write read back by Buck's public
///         getter, or a public write read back from storage.  A layout
///         change that moves one of these slots fails here, by name, before
///         a single test seeds the wrong place.
contract BuckSlotsTest is Test {
    Buck internal buck;
    address internal constant A = address(0xA11CE);
    address internal constant C = address(0xC0FFEE);

    function setUp() public {
        // Only the storage views and a plain approve are touched: no identity,
        // credit or controller call is ever made.
        buck = new Buck(address(1), address(2), address(3), address(4));
    }

    function test_stateSlot() public {
        vm.store(address(buck), BuckSlots.state(A), bytes32(uint256(1_234_567)));
        assertEq(buck.signedRawBalanceOf(A), 1_234_567, "_state[a].balance");
    }

    function test_supplySlot() public {
        vm.store(address(buck), BuckSlots.supply(), bytes32(uint256(777)));
        assertEq(buck.totalSupply(), 777, "_totalSupply");
    }

    function test_allowanceSlot() public {
        vm.prank(A);
        buck.approve(C, 42);
        assertEq(uint256(vm.load(address(buck), BuckSlots.allowance(A, C))), 42,
                 "_allowances[owner][spender]");
    }

    function test_fragmentSlot() public {
        bytes32 v = keccak256("a receipt");
        vm.store(address(buck), BuckSlots.fragment(A, C), v);
        assertEq(buck.receiptFragment(A, C), v, "_receiptFragments[from][to]");
    }
}
