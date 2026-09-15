// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import { IContractBindingAdapter } from "../IContractBindingAdapter.sol";
import { IdentityRegistry } from "../IdentityRegistry.sol";

/// @notice Shared guardrails for narrowly audited contract-binding adapters.
abstract contract ContractBindingAdapter is IContractBindingAdapter {
    IdentityRegistry internal immutable _registry;

    event AdapterBound(address indexed target, address indexed authority);

    constructor(address registry_) {
        require(registry_ != address(0), "registry=0");
        require(registry_.code.length > 0, "registry not a contract");
        _registry = IdentityRegistry(registry_);
    }

    function registry() public view override returns (address) {
        return address(_registry);
    }

    function provenance() public view virtual override returns (address);

    function bindingAuthority() public view virtual override returns (address);

    function _requireRegisteredAuthority() internal view returns (address authority) {
        authority = bindingAuthority();
        require(msg.sender == authority, "not binding authority");
        require(_registry.isVerified(authority), "authority not registered");
    }

    function _bindPublicCarrying(address target, address authority) internal {
        _registry.bindContractFromAdapter(target, authority, true, true);
        emit AdapterBound(target, authority);
    }
}
