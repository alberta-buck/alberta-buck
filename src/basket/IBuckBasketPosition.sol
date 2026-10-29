// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice Create an equity-basket receipt or add to an existing receipt.
interface IBuckBasketPosition {
    /// @notice Deposit an asset and create a new receipt.
    function deposit(address asset, uint256 amount, uint256 minShares) external returns (uint256 id);

    /// @notice Add the same asset to an existing receipt owned by the caller.
    /// @param id Existing receipt ID.
    /// @param asset The asset named by the receipt's original deposit.
    /// @param amount Nominal asset amount transferred from the caller.
    /// @param minReceived Minimum amount credited to basket equity, in asset units.
    /// @param minShares Minimum shares added to the receipt.
    /// @return positionId The same receipt ID.
    /// @return received Asset amount credited to basket equity. For BUCK this
    ///         is net of carried fees and any change to legacy relief claims.
    /// @return sharesAdded Additional shares in the existing receipt.
    function increasePosition(uint256 id, address asset, uint256 amount, uint256 minReceived, uint256 minShares)
        external returns (uint256 positionId, uint256 received, uint256 sharesAdded);

    /// @notice Redeem an exact, nonzero number of shares from an owned receipt.
    /// @return positionId The same ID if shares remain, or zero if it was burned.
    /// @return paid BUCK paid; in-kind TOKEN transfers are emitted separately.
    ///         A recipient of fee-on-transfer TOKEN must measure its own receipt.
    /// @return sharesRetired Shares actually removed, including the treasury cut.
    /// @return sharesRemaining Shares still held in the receipt after the exit.
    function redeemShares(uint256 id, uint256 shares, uint256 minBuck)
        external returns (uint256 positionId, uint256 paid, uint256 sharesRetired, uint256 sharesRemaining);
}
