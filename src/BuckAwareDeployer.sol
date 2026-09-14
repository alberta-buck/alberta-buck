// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {BN254}            from "./BN254.sol";
import {IdentityRegistry} from "./IdentityRegistry.sol";

/// @title BuckAwareDeployer -- atomic deploy + IdentityRegistry.bindContract.
/// @notice Operators that own BUCK-holding contracts (AMM pools, custodial
///         vaults, market makers, etc.) use this helper to atomically deploy
///         a contract and bind an Identity to its address in a single tx.
///
///         Atomicity removes the front-run window: an adversary cannot insert
///         a competing `bindContract` between the operator's deployment and
///         their bind because both happen inside one external call.
///
///         Certification is the registry's: the 5-arg bindContract path is
///         the already-certified-operator exception (msg.sender must be
///         registered and (pk, E) must match), so a helper contract cannot
///         use it.  The credential overloads forward a PS signature +
///         registration NIZK Fiat-Shamir-bound to the deployed address
///         (CREATE2-predictable).  Use a high-entropy salt or call this
///         helper from a tx the operator submits.
contract BuckAwareDeployer {

    IdentityRegistry public immutable registry;

    event Deployed(
        address indexed deployed,
        address indexed deployer,
        bool isPublicIdentity
    );

    constructor(address _registry) {
        require(_registry != address(0), "registry=0");
        registry = IdentityRegistry(_registry);
    }

    /// @notice Call `factory.<factoryCall>` (which must return a single
    ///         ABI-encoded address -- matches `IUniswapV2Factory.createPair`,
    ///         OpenZeppelin Clones, etc.) and bind the deployed contract
    ///         under a PS credential Fiat-Shamir-bound to that address.
    function deployAndBind(
        address factory,
        bytes calldata factoryCall,
        address issuer,
        BN254.G1Point calldata pk,
        IdentityRegistry.ElGamalCT calldata E,
        IdentityRegistry.PSSig calldata sigma,
        IdentityRegistry.RegistrationProof calldata proof,
        IdentityRegistry.ContractBindingProof calldata bindingProof,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external returns (address deployed) {
        deployed = _factoryDeploy(factory, factoryCall);
        registry.bindContract(
            deployed, issuer, pk, E, sigma, proof, bindingProof,
            isPublicIdentity_, isCarrying_
        );
        emit Deployed(deployed, msg.sender, isPublicIdentity_);
    }

    /// @notice Factory deploy + already-certified-operator bind.  The registry
    ///         5-arg path requires msg.sender (this helper) to be a registered
    ///         account whose stored (pk, E) match; a typical helper is not.
    ///         Prefer the credential overload.  Kept so fixture harnesses that
    ///         override 5-arg bindContract still exercise atomic deploy+bind.
    function deployAndBind(
        address factory,
        bytes calldata factoryCall,
        BN254.G1Point calldata pk,
        IdentityRegistry.ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external returns (address deployed) {
        deployed = _factoryDeploy(factory, factoryCall);
        registry.bindContract(deployed, pk, E, isPublicIdentity_, isCarrying_);
        emit Deployed(deployed, msg.sender, isPublicIdentity_);
    }

    /// @notice CREATE2-deploy raw `initCode` under `salt` and bind the result
    ///         under a PS credential Fiat-Shamir-bound to the predicted address.
    ///         Predicted address is keccak256(0xff || this || salt || keccak256(initCode)).
    function deployCreate2AndBind(
        bytes32 salt,
        bytes calldata initCode,
        address issuer,
        BN254.G1Point calldata pk,
        IdentityRegistry.ElGamalCT calldata E,
        IdentityRegistry.PSSig calldata sigma,
        IdentityRegistry.RegistrationProof calldata proof,
        IdentityRegistry.ContractBindingProof calldata bindingProof,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external returns (address deployed) {
        bytes memory ic = initCode;
        assembly {
            deployed := create2(0, add(ic, 32), mload(ic), salt)
        }
        require(deployed != address(0), "create2 failed");
        registry.bindContract(
            deployed, issuer, pk, E, sigma, proof, bindingProof,
            isPublicIdentity_, isCarrying_
        );
        emit Deployed(deployed, msg.sender, isPublicIdentity_);
    }

    /// @notice CREATE2 + already-certified-operator bind.  See 5-arg deployAndBind.
    function deployCreate2AndBind(
        bytes32 salt,
        bytes calldata initCode,
        BN254.G1Point calldata pk,
        IdentityRegistry.ElGamalCT calldata E,
        bool isPublicIdentity_,
        bool isCarrying_
    ) external returns (address deployed) {
        bytes memory ic = initCode;
        assembly {
            deployed := create2(0, add(ic, 32), mload(ic), salt)
        }
        require(deployed != address(0), "create2 failed");
        registry.bindContract(deployed, pk, E, isPublicIdentity_, isCarrying_);
        emit Deployed(deployed, msg.sender, isPublicIdentity_);
    }

    function _factoryDeploy(address factory, bytes calldata factoryCall)
        internal returns (address deployed)
    {
        (bool ok, bytes memory ret) = factory.call(factoryCall);
        require(ok, "factory call failed");
        require(ret.length >= 32, "factory return too short");
        deployed = abi.decode(ret, (address));
        require(deployed != address(0), "factory returned zero");
    }

    /// @notice Compute the CREATE2 address that `deployCreate2AndBind` would
    ///         produce for the given salt + initCode.
    function predictCreate2Address(bytes32 salt, bytes32 initCodeHash)
        external view returns (address)
    {
        return address(uint160(uint256(keccak256(
            abi.encodePacked(bytes1(0xff), address(this), salt, initCodeHash)
        ))));
    }
}
