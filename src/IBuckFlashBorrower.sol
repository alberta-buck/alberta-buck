// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice The callback of Buck.flashIssue: the borrower holds `amount` fresh BUCK for the length of this
///         call and must hold them again when it returns, when Buck debits exactly that amount.
interface IBuckFlashBorrower {
    function onBuckFlash(uint256 tokenId, uint256 amount, bytes calldata data) external returns (bytes memory);
}

/// @notice Buck's flash: fresh BUCK for one call, bounded by a facility's room to issue.
interface IBuckFlash {
    function flashIssue(uint256 tokenId, uint256 amount, bytes calldata data) external returns (bytes memory);
    function flashRoom(uint256 tokenId) external view returns (uint256);
}
