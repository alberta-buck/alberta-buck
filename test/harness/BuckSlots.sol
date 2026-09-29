// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title BuckSlots -- every slot of Buck's that a test reaches by position,
///        named once.
///
/// @notice Tests seed state with `vm.store` where the public API would need
///         the PS-credential or Chaum-Pedersen machinery (a receipt fragment,
///         an allowance, a raw balance).  A slot reached by number moves
///         silently when the layout changes, so every such access goes
///         through here, and `test/BuckSlots.t.sol` pins each constant against
///         the real layout by writing through one side and reading through the
///         other.  Change Buck's layout, change this file, run that test.
library BuckSlots {
    uint256 internal constant STATE      = 0;   // mapping(address => AccountState) _state
    uint256 internal constant SUPPLY     = 1;   // uint256 _totalSupply
    uint256 internal constant ALLOWANCES = 2;   // mapping(address => mapping(address => uint256)) _allowances
    uint256 internal constant FRAGMENTS  = 4;   // mapping(address => mapping(address => bytes32)) _receiptFragments

    /// @notice `_state[a]`: the packed (balance, buckSeconds, timestamp, flags) word.
    function state(address a) internal pure returns (bytes32) {
        return keccak256(abi.encode(a, STATE));
    }

    function supply() internal pure returns (bytes32) {
        return bytes32(SUPPLY);
    }

    /// @notice `_allowances[owner][spender]`.
    function allowance(address owner, address spender) internal pure returns (bytes32) {
        return keccak256(abi.encode(spender, keccak256(abi.encode(owner, ALLOWANCES))));
    }

    /// @notice `_receiptFragments[from][to]`: the CP receipt `from` laid down
    ///         for `to` (it lets a private `from` send to `to`, and a private
    ///         `from` receive from `to`).
    function fragment(address from, address to) internal pure returns (bytes32) {
        return keccak256(abi.encode(to, keccak256(abi.encode(from, FRAGMENTS))));
    }
}
