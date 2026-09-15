// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import { ContractBindingAdapter } from "./ContractBindingAdapter.sol";

interface IUniswapV2BindingFactory {
    function feeToSetter() external view returns (address);
    function getPair(address tokenA, address tokenB) external view returns (address pair);
    function createPair(address tokenA, address tokenB) external returns (address pair);
}

/// @title Uniswap V2 pool binding adapter.
/// @notice Binds only canonical pairs from one immutable V2 factory, and only
///         to the registered identity that factory names as feeToSetter.
contract UniswapV2BindingAdapter is ContractBindingAdapter {
    IUniswapV2BindingFactory internal immutable _factory;

    constructor(address registry_, address factory_) ContractBindingAdapter(registry_) {
        require(factory_ != address(0), "factory=0");
        require(factory_.code.length > 0, "factory not a contract");
        _factory = IUniswapV2BindingFactory(factory_);
    }

    function provenance() public view override returns (address) {
        return address(_factory);
    }

    function bindingAuthority() public view override returns (address) {
        return _factory.feeToSetter();
    }

    /// @notice Create a new canonical pair and bind it in the same transaction.
    function createPairAndBind(address tokenA, address tokenB) external returns (address pair) {
        address authority = _requireRegisteredAuthority();
        require(
            _factory.getPair(tokenA, tokenB) == address(0) && _factory.getPair(tokenB, tokenA) == address(0),
            "pair already exists"
        );
        pair = _factory.createPair(tokenA, tokenB);
        _requireCanonicalPair(tokenA, tokenB, pair);
        _bindPublicCarrying(pair, authority);
    }

    /// @notice Bind an already-created canonical pair under factory governance.
    function bindExistingPair(address tokenA, address tokenB) external returns (address pair) {
        address authority = _requireRegisteredAuthority();
        pair = _factory.getPair(tokenA, tokenB);
        _requireCanonicalPair(tokenA, tokenB, pair);
        _bindPublicCarrying(pair, authority);
    }

    function _requireCanonicalPair(address tokenA, address tokenB, address pair) internal view {
        require(pair != address(0) && pair.code.length > 0, "pair not deployed");
        require(
            _factory.getPair(tokenA, tokenB) == pair && _factory.getPair(tokenB, tokenA) == pair, "noncanonical pair"
        );
    }
}
