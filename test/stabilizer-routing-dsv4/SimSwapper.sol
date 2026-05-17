// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IUniswapV3Pool} from "@uniswap/v3-core/contracts/interfaces/IUniswapV3Pool.sol";

interface IUniswapV3SwapCallback {
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external;
}

/// @title SimSwapper — minimal V3 multi-hop swap executor.
///
/// Encoded path:  abi.encode(address[] pools, bool zeroForOne0, bool zeroForOne1, ...)
/// Swaps `amountIn` of the first pool's input token through each hop in sequence,
/// sending the final output to `recipient`.
contract SimSwapper is IUniswapV3SwapCallback {
    address internal _currentPayer;
    address internal _currentTokenIn;
    uint256 internal _currentAmountIn;

    /// @notice Execute a multi-hop swap.  Caller must have approved this contract
    ///         for `amountIn` of the first hop's input token.
    /// @param path  abi.encode(address[] pools, bool[] zeroForOne)
    /// @param amountIn  input token amount (native decimals)
    /// @param amountOutMin  minimum output (slippage guard)
    /// @param recipient  receives output tokens
    function swap(
        bytes calldata path,
        uint256 amountIn,
        uint256 amountOutMin,
        address recipient
    ) external returns (uint256 amountOut) {
        (address[] memory pools, bool[] memory zeroForOne) = abi.decode(path, (address[], bool[]));
        require(pools.length == zeroForOne.length, "len");

        _currentPayer = msg.sender;
        _currentTokenIn = zeroForOne[0]
            ? IUniswapV3Pool(pools[0]).token0()
            : IUniswapV3Pool(pools[0]).token1();
        _currentAmountIn = amountIn;

        // Pull input from caller.
        IERC20(_currentTokenIn).transferFrom(msg.sender, address(this), amountIn);

        for (uint256 i = 0; i < pools.length; i++) {
            bool zfo = zeroForOne[i];
            address nextRecipient = (i + 1 < pools.length)
                ? address(this)
                : recipient;
            (int256 delta0, int256 delta1) = IUniswapV3Pool(pools[i]).swap(
                nextRecipient,
                zfo,
                int256(_currentAmountIn),
                zfo ? 4295128740 : 1461446703485210103287273052203988822378723970341,  // sqrt limit
                abi.encode(msg.sender)
            );
            // Output is the positive delta.
            amountOut = uint256(zfo ? -delta1 : -delta0);
            if (i + 1 < pools.length) {
                _currentTokenIn = zfo
                    ? IUniswapV3Pool(pools[i]).token1()
                    : IUniswapV3Pool(pools[i]).token0();
                _currentAmountIn = amountOut;
            }
        }
        require(amountOut >= amountOutMin, "slip");
    }

    /// @dev V3 swap callback — called by each pool during the swap.
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata) external override {
        require(amount0Delta > 0 || amount1Delta > 0, "bad delta");
        // Pay the pool what it needs.
        (address token, uint256 amount) = amount0Delta > 0
            ? (IUniswapV3Pool(msg.sender).token0(), uint256(amount0Delta))
            : (IUniswapV3Pool(msg.sender).token1(), uint256(amount1Delta));
        IERC20(token).transfer(msg.sender, amount);
    }
}
