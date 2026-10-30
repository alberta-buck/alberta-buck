// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BuckCredit} from "../src/BuckCredit.sol";

/// @dev A factory deploying credit remains the credit's deployer; its own
///      authorization controls who can ask it to perform one-shot wiring.
contract AuthorizedCreditFactory {
    address public immutable owner;
    BuckCredit public immutable credit;

    constructor() {
        owner = msg.sender;
        credit = new BuckCredit();
    }

    function setBuck(address token) external {
        require(msg.sender == owner, "factory: not owner");
        credit.setBuck(token);
    }
}

contract BuckCreditWiringTest is Test {
    address constant ATTACKER = address(0xBAD);
    address constant TOKEN = address(0xB0CC);

    function test_thirdPartyCannotTakeTheOneShotTokenSlot() public {
        BuckCredit credit = new BuckCredit();
        vm.expectRevert("BuckCredit: not deployer");
        vm.prank(ATTACKER);
        credit.setBuck(ATTACKER);
        assertEq(credit.buck(), address(0));

        vm.expectRevert("BuckCredit: buck=0");
        credit.setBuck(address(0));
        credit.setBuck(TOKEN);
        assertEq(credit.buck(), TOKEN);
        vm.expectRevert("BuckCredit: buck already set");
        credit.setBuck(ATTACKER);
    }

    function test_aFactoryMustWireItsOwnCreditThroughItsAuthorizedPath() public {
        AuthorizedCreditFactory factory = new AuthorizedCreditFactory();
        BuckCredit credit = factory.credit();
        vm.expectRevert("BuckCredit: not deployer");
        credit.setBuck(TOKEN);
        vm.expectRevert("factory: not owner");
        vm.prank(ATTACKER);
        factory.setBuck(ATTACKER);
        assertEq(credit.buck(), address(0));
        factory.setBuck(TOKEN);
        assertEq(credit.buck(), TOKEN);
    }
}
