// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IPoseidonT3 -- 2-input Poseidon hash on BN254's scalar field.
/// @notice Matches the function selector emitted by circomlibjs's
///         `poseidonContract.createCode(2)`: a single externally-callable
///         `poseidon(uint256[2]) pure returns (uint256)`.  The on-chain
///         contract is deployed from raw bytecode (no Solidity source); see
///         `scripts/snark/poseidon_t3_code.js`.
interface IPoseidonT3 {
    function poseidon(uint256[2] calldata input) external pure returns (uint256);
}
