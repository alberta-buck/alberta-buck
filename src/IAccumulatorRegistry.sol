// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254} from "./BN254.sol";

/// @title IAccumulatorRegistry -- what the insurer gate reads from IdentityRegistry.
/// @notice An account's registered key and credential, root acceptance per
///         consumer, the public identity leaf, and public membership paths
///         (doc/review/accumulator-spec.org, sections 5, 11.2 and 12).
interface IAccumulatorRegistry {
    /// @dev ABI-identical to IdentityRegistry.ElGamalCT.
    struct Ciphertext {
        BN254.G1Point R;
        BN254.G1Point C;
    }

    function pkOf(address account) external view returns (BN254.G1Point memory);
    function ciphertextOf(address account) external view returns (Ciphertext memory);
    function isVerified(address account) external view returns (bool);
    function acceptsRoot(uint256 root, bytes32 consumer) external view returns (bool);
    function rootPostedAt(uint256 root) external view returns (uint64);
    function publicIdentityLeaf(BN254.G1Point calldata M) external view returns (uint256);
    function verifyPublicMembership(
        bytes32 id,
        uint256 leaf,
        uint256[] calldata subSiblings,
        uint256 subIndex,
        uint256[] calldata aggSiblings,
        uint256 root
    ) external view returns (bool);
}
