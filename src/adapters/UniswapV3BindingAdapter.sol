// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import { ContractBindingAdapter } from "./ContractBindingAdapter.sol";

interface IUniswapV3BindingFactory {
    function owner() external view returns (address);
    function getPool(address tokenA, address tokenB, uint24 fee) external view returns (address pool);
    function createPool(address tokenA, address tokenB, uint24 fee) external returns (address pool);
}

/// @title Uniswap V3 pool binding adapter.
/// @notice Binds only canonical pools from one immutable V3 factory, and only
///         to the registered identity that factory names as owner.
contract UniswapV3BindingAdapter is ContractBindingAdapter {
    IUniswapV3BindingFactory internal immutable _factory;

    constructor(address registry_, address factory_) ContractBindingAdapter(registry_) {
        require(factory_ != address(0), "factory=0");
        require(factory_.code.length > 0, "factory not a contract");
        _factory = IUniswapV3BindingFactory(factory_);
    }

    function provenance() public view override returns (address) {
        return address(_factory);
    }

    function bindingAuthority() public view override returns (address) {
        return _factory.owner();
    }

    /// @notice Create a new canonical pool and bind it in the same transaction.
    function createPoolAndBind(address tokenA, address tokenB, uint24 fee) external returns (address pool) {
        address authority = _requireRegisteredAuthority();
        require(
            _factory.getPool(tokenA, tokenB, fee) == address(0) && _factory.getPool(tokenB, tokenA, fee) == address(0),
            "pool already exists"
        );
        pool = _factory.createPool(tokenA, tokenB, fee);
        _requireCanonicalPool(tokenA, tokenB, fee, pool);
        _bindPublicCarrying(pool, authority);
    }

    /// @notice Bind an already-created canonical pool under factory governance.
    function bindExistingPool(address tokenA, address tokenB, uint24 fee) external returns (address pool) {
        address authority = _requireRegisteredAuthority();
        pool = _factory.getPool(tokenA, tokenB, fee);
        _requireCanonicalPool(tokenA, tokenB, fee, pool);
        _bindPublicCarrying(pool, authority);
    }

    function _requireCanonicalPool(address tokenA, address tokenB, uint24 fee, address pool) internal view {
        require(pool != address(0) && pool.code.length > 0, "pool not deployed");
        require(
            _factory.getPool(tokenA, tokenB, fee) == pool && _factory.getPool(tokenB, tokenA, fee) == pool,
            "noncanonical pool"
        );
    }
}
