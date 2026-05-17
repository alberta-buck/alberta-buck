// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BuckAwareDeployer} from "../src/BuckAwareDeployer.sol";

/// @dev Minimal factory that deploys a trivial contract and returns its address.
///      Mirrors the shape of IUniswapV2Factory.createPair / OZ Clones.clone.
contract ToyFactory {
    function deploy() external returns (address) {
        return address(new ToyContract());
    }
    function deployReverting() external pure returns (address) {
        revert("nope");
    }
    function deployReturningZero() external returns (address) {
        // return a zero address (simulating factory that returned 0x0)
        return address(0);
    }
    function deployReturningShort() external returns (address) {
        assembly { mstore(0, 0x01) return(0, 1) }
    }
}

contract ToyContract {}

/// @title BuckAwareDeployerTest — atomic deploy + bind.
contract BuckAwareDeployerTest is Test {

    IdentityRegistry   internal reg;
    BuckAwareDeployer  internal deployer;
    ToyFactory         internal factory;

    address internal constant GOV = address(0xA0);

    function setUp() public {
        reg      = new IdentityRegistry(GOV);
        deployer = new BuckAwareDeployer(address(reg));
        factory  = new ToyFactory();
    }

    // ---- constructor ---------------------------------------------------------

    function test_constructor_rejectsZeroRegistry() public {
        vm.expectRevert("registry=0");
        new BuckAwareDeployer(address(0));
    }

    function test_constructor_setsRegistry() public view {
        assertEq(address(deployer.registry()), address(reg));
    }

    // ---- deployAndBind -------------------------------------------------------

    function test_deployAndBind_succeeds() public {
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        address deployed = deployer.deployAndBind(
            address(factory),
            abi.encodeWithSelector(ToyFactory.deploy.selector),
            pk, E,
            true,   // isPublicIdentity
            true    // isCarrying
        );

        assertTrue(deployed != address(0));
        assertTrue(reg.isVerified(deployed),         "contract now verified");
        assertTrue(reg.isPublicIdentity(deployed),   "contract is public");
        assertTrue(reg.isCarrying(deployed),         "contract is carrying");
        assertGt(deployed.code.length, 0,            "deployed has code");
    }

    function test_deployAndBind_revertsOnFactoryRevert() public {
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        vm.expectRevert("factory call failed");
        deployer.deployAndBind(
            address(factory),
            abi.encodeWithSelector(ToyFactory.deployReverting.selector),
            pk, E, true, true
        );
    }

    function test_deployAndBind_rejectsZeroDeployed() public {
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        vm.expectRevert("factory returned zero");
        deployer.deployAndBind(
            address(factory),
            abi.encodeWithSelector(ToyFactory.deployReturningZero.selector),
            pk, E, true, true
        );
    }

    function test_deployAndBind_rejectsShortReturn() public {
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        vm.expectRevert("factory return too short");
        deployer.deployAndBind(
            address(factory),
            abi.encodeWithSelector(ToyFactory.deployReturningShort.selector),
            pk, E, true, true
        );
    }

    function test_deployAndBind_emitsDeployedEvent() public {
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        // Check deployer (indexed topic 2) and isPublicIdentity (data).
        // Don't check deployed address (nonce-dependent).
        vm.expectEmit(false, true, false, true);
        emit BuckAwareDeployer.Deployed(address(0), address(this), true);
        deployer.deployAndBind(
            address(factory),
            abi.encodeWithSelector(ToyFactory.deploy.selector),
            pk, E, true, true
        );
    }

    function test_bindContract_firstBinderWinsAfterDeploy() public {
        // Deploy a contract first, then bind it through the deployer.
        // A subsequent direct bind attempt must revert.
        address deployed = address(new ToyContract());

        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        // First bind succeeds.
        vm.prank(address(deployer));
        reg.bindContract(deployed, pk, E, true, true);

        // Second bind reverts — already bound.
        vm.expectRevert("already bound");
        vm.prank(address(deployer));
        reg.bindContract(deployed, pk, E, false, false);
    }

    function test_deployAndBind_encryptedIdentity() public {
        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        address deployed = deployer.deployAndBind(
            address(factory),
            abi.encodeWithSelector(ToyFactory.deploy.selector),
            pk, E,
            false,  // isPublicIdentity
            false   // isCarrying (user-controlled multisig/AA wallet)
        );

        assertTrue(reg.isVerified(deployed));
        assertFalse(reg.isPublicIdentity(deployed));
        assertFalse(reg.isCarrying(deployed));
    }

    // ---- deployCreate2AndBind ------------------------------------------------

    function test_deployCreate2AndBind_succeeds() public {
        bytes memory initCode = type(ToyContract).creationCode;
        bytes32 salt = bytes32(uint256(0x42));

        address predicted = deployer.predictCreate2Address(salt, keccak256(initCode));

        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        address deployed = deployer.deployCreate2AndBind(
            salt, initCode, pk, E, true, true
        );

        assertEq(deployed, predicted, "CREATE2 address matches prediction");
        assertTrue(reg.isVerified(deployed));
        assertGt(deployed.code.length, 0);
    }

    function test_deployCreate2AndBind_revertsOnSecondDeploy() public {
        bytes memory initCode = type(ToyContract).creationCode;
        bytes32 salt = bytes32(uint256(0x42));

        BN254.G1Point memory pk = BN254.g1();
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        deployer.deployCreate2AndBind(salt, initCode, pk, E, true, true);

        // Second CREATE2 with same salt+initCode fails (address already exists).
        vm.expectRevert("create2 failed");
        deployer.deployCreate2AndBind(salt, initCode, pk, E, true, true);
    }

    // ---- predictCreate2Address -----------------------------------------------

    function test_predictCreate2Address_matchesComputation() public view {
        bytes32 salt   = bytes32(uint256(0xDEAD));
        bytes32 codeHash = keccak256(type(ToyContract).creationCode);

        address predicted = deployer.predictCreate2Address(salt, codeHash);

        // Verify by recomputing the CREATE2 address.
        address expected = address(uint160(uint256(keccak256(
            abi.encodePacked(bytes1(0xff), address(deployer), salt, codeHash)
        ))));
        assertEq(predicted, expected);
    }
}
