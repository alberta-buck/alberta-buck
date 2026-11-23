// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BasketUpkeepSignalTrace} from "./BasketUpkeepSignalTrace.sol";

contract BasketUpkeepSignalTraceMaintenance is BasketUpkeepSignalTrace {
    constructor(address policy, address parity_, uint256 parityGain_, uint256 parityGainDear_)
        BasketUpkeepSignalTrace(policy, parity_, parityGain_, parityGainDear_) {}

    function _pairShare(uint256, uint256) internal pure override returns (uint256) { return 0; }

    function _rate(uint256, bool, uint256, uint256) internal pure override returns (uint256) { return 0; }
}
