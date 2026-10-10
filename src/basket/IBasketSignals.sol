// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBasketReference} from "./IBasketReference.sol";

/// @notice The EMA trend gates that only the inherited response plan still
///         reads. They retire with the ladders.
interface IBasketSignals is IBasketReference {
    function excursion(uint256 i) external view returns (int256);
    function votes(uint256 i) external view returns (uint256);
    function turned(uint256 i) external view returns (bool);
    function running(uint256 i, int256 side) external view returns (bool);
}
