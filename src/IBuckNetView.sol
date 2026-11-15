// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @notice Buck's net view: what an account's BUCK is worth in full-value BUCK.
/// @dev Buck's ERC-20 view (balanceOf, Transfer amounts, a Uniswap pool's
///      reserves and quotes) is NOMINAL. An ordinary account's nominal BUCK is
///      full value; a Carrying account's carries its accrued demurrage, so its
///      net is less. Convert with lib/CarryRatio.sol. IBuckCashView extends this.
interface IBuckNetView {
    /// @return gross the account's nominal BUCK and net its full value, both
    ///         raw BUCK x 1e36 (BuckCash's fractional scale).
    function accountBalancesScaled(address account) external view returns (uint256 gross, uint256 net);
    /// @return the account's full-value BUCK, raw.
    function netBalanceOf(address account) external view returns (uint256);
}
