// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice Minimal ERC20 with configurable decimals and an open-mint helper.
///         Used as a stand-in for real tokens (USDT, USDC, XAUT, etc.) in
///         local Uniswap V3 pool tests.
contract MockERC20 is ERC20 {
    uint8 private immutable _decimals;

    constructor(string memory name_, string memory symbol_, uint8 dec) ERC20(name_, symbol_) {
        _decimals = dec;
    }

    function decimals() public view override returns (uint8) {
        return _decimals;
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}
