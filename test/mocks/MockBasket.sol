// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice Mock BuckBasket exposing a settable `basketValueInBuck()`.  Used
///         by BuckKControllerDirect unit tests to drive the controller
///         independently of any real V3 pool wiring.
contract MockBasket {
    int256 public basketValueInBuck;
    address public controller;

    function setBasketValue(int256 v) external { basketValueInBuck = v; }
    function setController(address c) external { controller = c; }

    /// @notice Test-only passthrough that lets a test impersonate the
    ///         basket and call reprime on the controller.
    function callReprime() external {
        (bool ok,) = controller.call(abi.encodeWithSignature("reprime()"));
        require(ok, "reprime failed");
    }
}
