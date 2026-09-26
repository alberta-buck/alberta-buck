// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IPoseidonT4 -- 3-input Poseidon hash on BN254's scalar field.
/// @notice Matches the function selector emitted by circomlibjs's
///         `poseidonContract.createCode(3)`: a single externally-callable
///         `poseidon(uint256[3]) pure returns (uint256)`.  Deployed from raw
///         bytecode; see `scripts/snark/poseidon_code.js`.  Hashes the tagged
///         public identity leaf Poseidon(TAG, M.x, M.y).
interface IPoseidonT4 {
    function poseidon(uint256[3] calldata input) external pure returns (uint256);
}
