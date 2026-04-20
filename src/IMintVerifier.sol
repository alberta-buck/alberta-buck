// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title IMintVerifier — Notes mint-circuit verifier interface.
/// @notice Phase 2 will plug in a Groth16/PLONK verifier whose Solidity is
///         auto-generated from the mint circuit; Phase 1 ships a stub.  The
///         interface fixes the public-input layout so the Notes contract is
///         already compatible with the eventual SNARK.
///
/// @dev    Public inputs (per alberta-buck-notes.org section "Mint Circuit"):
///           - totalFace:    sum of v_i over all minted notes
///           - commitments:  [cm_1, ..., cm_N], each appended to noteTree
///           - issuer:       msg.sender of the calling Notes.mint()
interface IMintVerifier {
    function verifyMint(
        bytes calldata proof,
        uint256 totalFace,
        uint256[] calldata commitments,
        address issuer
    ) external view returns (bool);
}
