// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IBuckJubilee} from "./IBuckJubilee.sol";

interface IBuckCashK {
    function currentBuckK() external view returns (uint256);
    function fundingFactor() external view returns (uint256);
}

/// @notice Token-independent cash/capacity observations for optional clients.
/// @dev Does not label fungible cash by origin or expose a signed wallet.
///      Accounting-model/version binding belongs to the deployment record.
interface IBuckCashView is IBuckJubilee {
    function buckK() external view returns (IBuckCashK);
    function totalIssued() external view returns (uint256);
    function accountBalancesScaled(address owner) external view returns (uint256 gross, uint256 net);
    function netBalanceOf(address owner) external view returns (uint256);
    function approvedCarryingIssuer(address owner) external view returns (bool);
}
