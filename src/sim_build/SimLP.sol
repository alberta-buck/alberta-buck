// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

/// @title SimLP -- minimal Uniswap V3 liquidity + swap helper for the
///        externally-driven (anvil + web3.py) routing simulation.
///
/// The Python driver owns all numeric decisions (sqrtPrice, liquidity L,
/// tick bounds, swap amounts).  This contract only provides what an
/// external EOA cannot do directly: implement the V3 mint/swap callbacks
/// so it can seed full-range liquidity into the TOKEN/USDC pools and let
/// the synthetic market-maker drive a pool's spot to an exact target.
///
/// Callback bodies are the proven ones from test/fixtures/UniswapV3Fixture.sol
/// (pay owed/positive-delta tokens from this contract's own balance).
/// SimLP only ever custodies TOKEN and USDC -- never BUCK -- so it needs no
/// IdentityRegistry binding (the TOKEN/BUCK basket pools are LP'd by
/// BuckBasket.depositToken, which is its own callback).
contract SimLP {
    function mint(
        address pool,
        int24   tickLower,
        int24   tickUpper,
        uint128 liquidity,
        address token0,
        address token1
    ) external returns (uint256 amount0, uint256 amount1) {
        return IUniswapV3Pool(pool).mint(
            address(this), tickLower, tickUpper, liquidity,
            abi.encode(token0, token1)
        );
    }

    /// @notice Generic passthrough so this *public, identity-bound* helper
    ///         can itself act as the BUCK-backed LP: pledge an insured
    ///         asset (BuckCredit.activate) and mint BUCK (Buck.mint).  All
    ///         resulting BUCK transfers are public<->public (SimLP -> pool),
    ///         so no identity fakery is needed anywhere.
    function exec(address target, bytes calldata data)
        external returns (bytes memory)
    {
        (bool ok, bytes memory ret) = target.call(data);
        require(ok, "SimLP: exec failed");
        return ret;
    }

    function swap(
        address pool,
        address recipient,
        bool    zeroForOne,
        int256  amountSpecified,
        uint160 sqrtPriceLimitX96,
        address token0,
        address token1
    ) external returns (int256 amount0, int256 amount1) {
        return IUniswapV3Pool(pool).swap(
            recipient, zeroForOne, amountSpecified, sqrtPriceLimitX96,
            abi.encode(token0, token1)
        );
    }

    function uniswapV3MintCallback(
        uint256 amount0Owed,
        uint256 amount1Owed,
        bytes calldata data
    ) external {
        (address t0, address t1) = abi.decode(data, (address, address));
        if (amount0Owed > 0) IERC20(t0).transfer(msg.sender, amount0Owed);
        if (amount1Owed > 0) IERC20(t1).transfer(msg.sender, amount1Owed);
    }

    function uniswapV3SwapCallback(
        int256 amount0Delta,
        int256 amount1Delta,
        bytes calldata data
    ) external {
        (address t0, address t1) = abi.decode(data, (address, address));
        if (amount0Delta > 0) IERC20(t0).transfer(msg.sender, uint256(amount0Delta));
        if (amount1Delta > 0) IERC20(t1).transfer(msg.sender, uint256(amount1Delta));
    }
}

interface IUniswapV3Pool {
    function mint(address recipient, int24 tickLower, int24 tickUpper, uint128 amount, bytes calldata data)
        external returns (uint256 amount0, uint256 amount1);
    function swap(address recipient, bool zeroForOne, int256 amountSpecified, uint160 sqrtPriceLimitX96, bytes calldata data)
        external returns (int256 amount0, int256 amount1);
}

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
}
