// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketUpkeepFacet} from "../../src/basket/BasketUpkeepFacet.sol";

/// @notice Experimental control: the same upkeep with no view on prices. It
/// pairs what waits, keeps the book its size, every constituent inside its
/// leash and the issue under its ceiling, and never works a gap on the
/// policy's word.
contract BasketUpkeepMaintenanceControl is BasketUpkeepFacet {
    constructor(address policy, address parity_, uint256 parityGain_, uint256 parityGainDear_)
        BasketUpkeepFacet(policy, parity_, parityGain_, parityGainDear_) {}

    function _rate(uint256, bool, uint256) internal pure override returns (uint256) { return 0; }
}
