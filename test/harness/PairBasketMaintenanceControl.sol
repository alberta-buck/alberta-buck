// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";

/// @notice Experimental control: retain maintenance, LP placement and risk
/// overrides, but disable discretionary paired transfers. No invented prices.
contract PairBasketMaintenanceControl is BasketUpkeepFacet {
    constructor(address policy) BasketUpkeepFacet(policy) {}

    function _allocation(Snap memory s) internal view override returns (Allocation memory a) {
        a = super._allocation(s);
        a.donor = type(uint256).max;
        a.total = 0;
        a.recipients = new uint256[](s.m.length);
    }
}
