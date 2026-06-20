// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test}             from "forge-std/Test.sol";
import {BasketRebalancer} from "../../src/basket/BasketRebalancer.sol";

contract BasketRebalancerTest is Test {
    BasketRebalancer internal reb;

    address constant GOV   = address(0xA0);
    address constant TOKEN = address(0x7);
    address constant BUCK  = address(0xB);
    address constant USDC  = address(0xC);

    function setUp() public {
        reb = new BasketRebalancer(GOV);
    }

    function test_unregistered_returnsEmpty() public view {
        assertEq(reb.pathFor(TOKEN, BUCK).length, 0, "no route");
    }

    function test_setRoute_andRead() public {
        // TOKEN -> USDC -> BUCK encoded path.
        bytes memory path = abi.encodePacked(TOKEN, uint24(500), USDC, uint24(500), BUCK);
        vm.prank(GOV);
        reb.setRoute(TOKEN, BUCK, path);

        assertEq(reb.pathFor(TOKEN, BUCK), path, "route stored");
        // Directional: the reverse pair is independent / still empty.
        assertEq(reb.pathFor(BUCK, TOKEN).length, 0, "reverse not set");
    }

    function test_setRoute_clear() public {
        bytes memory path = abi.encodePacked(TOKEN, uint24(500), BUCK);
        vm.prank(GOV); reb.setRoute(TOKEN, BUCK, path);
        assertGt(reb.pathFor(TOKEN, BUCK).length, 0, "set");
        vm.prank(GOV); reb.setRoute(TOKEN, BUCK, "");
        assertEq(reb.pathFor(TOKEN, BUCK).length, 0, "cleared");
    }

    function test_setRoute_onlyGov() public {
        vm.expectRevert(bytes("not governance"));
        reb.setRoute(TOKEN, BUCK, abi.encodePacked(TOKEN, uint24(500), BUCK));
    }

    function test_setRoute_sameToken_reverts() public {
        vm.prank(GOV);
        vm.expectRevert(bytes("same token"));
        reb.setRoute(TOKEN, TOKEN, "");
    }

    function test_transferGovernance() public {
        vm.prank(GOV);
        reb.transferGovernance(address(0xBEEF));
        assertEq(reb.governance(), address(0xBEEF));
        // old gov can no longer set routes
        vm.prank(GOV);
        vm.expectRevert(bytes("not governance"));
        reb.setRoute(TOKEN, BUCK, "");
    }
}
