// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {PoseidonT3Bytecode} from "../src/PoseidonT3Bytecode.sol";
import {BN254} from "../src/BN254.sol";

/// @notice Identity-axis V2 test: reads registry-generated vectors
///         (test/vectors/registry/) and exercises the identity root
///         accumulator alongside the existing PS-credential registration.
///         Demonstrates the Python registry -> Forge test pipeline.
///
///         The identity root is maintained off-chain by the registry
///         (alberta_buck.registry.CentralMerkleService) and posted on chain
///         once per batch of registrations.  For the test, we set it directly
///         from the vector file.  In production, governance or a batched
///         update function posts the root.
///
///         See alberta-buck-notes.org ("Mutual Decryptability") and alberta-buck-notes-flow.org "The Identity-M Spend Path" (one-gadget / accumulator).
contract IdentityRegistryV2Test is Test {
    IdentityRegistry internal reg;
    address internal constant GOV = address(0xA0);
    address internal constant ISSUER_ADDR = address(0x1551E1);

    string internal rj;   // registry_vectors.json
    string internal rjRoot; // identity_root.json
    string internal rjAlice; // identity_alice.json
    string internal rjBob;   // identity_bob.json

    address internal alice;
    address internal bob;
    uint256 internal expectedIdentityRoot;

    function setUp() public {
        vm.chainId(1);

        // Load registry-generated vectors.
        rj      = vm.readFile("test/vectors/registry/registry_vectors.json");
        rjRoot  = vm.readFile("test/vectors/registry/identity_root.json");
        rjAlice = vm.readFile("test/vectors/registry/identity_alice.json");
        rjBob   = vm.readFile("test/vectors/registry/identity_bob.json");

        expectedIdentityRoot = vm.parseJsonUint(rjRoot, ".identityRoot");

        address registryAddr = address(uint160(_u(rj, ".registry.address")));
        deployCodeTo(
            "IdentityRegistry.sol:IdentityRegistry",
            abi.encode(GOV),
            registryAddr
        );
        reg = IdentityRegistry(registryAddr);
        _trustIssuer();

        // Compute deterministic addresses that match the Python vectors'
        // registrant fields.  The existing vectors use 0 for registrant
        // (filled by caller), so we assign known addresses.
        alice = address(uint160(0x1000000000000000000000000000000000000000));
        bob   = address(uint160(0x1000000000000000000000000000000000000001));
        vm.etch(alice, hex"60006000fd");
        vm.etch(bob,   hex"60006000fd");
    }

    // ---- helpers (mirror existing IdentityRegistry.t.sol patterns) --------

    function _u(string memory j, string memory key) internal pure returns (uint256) {
        return vm.parseJsonUint(j, key);
    }

    function _g1j(string memory j, string memory key)
        internal pure returns (BN254.G1Point memory)
    {
        return BN254.G1Point(
            _u(j, string.concat(key, ".x")),
            _u(j, string.concat(key, ".y"))
        );
    }

    function _trustIssuer() internal {
        // Reconstruct PS issuer public key from the vector file.
        IdentityRegistry.PSPubKey memory ipk;
        ipk.X = BN254.G2Point(
            [_u(rj, ".ps_issuer.pk_X.x[0]"), _u(rj, ".ps_issuer.pk_X.x[1]")],
            [_u(rj, ".ps_issuer.pk_X.y[0]"), _u(rj, ".ps_issuer.pk_X.y[1]")]
        );
        ipk.Y = BN254.G2Point(
            [_u(rj, ".ps_issuer.pk_Y.x[0]"), _u(rj, ".ps_issuer.pk_Y.x[1]")],
            [_u(rj, ".ps_issuer.pk_Y.y[0]"), _u(rj, ".ps_issuer.pk_Y.y[1]")]
        );
        ipk.Y1 = _g1j(rj, ".ps_issuer.pk_Y1");
        vm.prank(GOV);
        reg.trustIssuer(ISSUER_ADDR, ipk);
        assertTrue(reg.isTrustedIssuer(ISSUER_ADDR));
    }

    function _registerFromVectors(string memory j, address who) internal {
        BN254.G1Point memory pk = _g1j(j, ".elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT(
            _g1j(j, ".ciphertext.R"),
            _g1j(j, ".ciphertext.C")
        );
        IdentityRegistry.PSPresentation memory sigma = IdentityRegistry.PSPresentation(
            _g1j(j, ".ps_presentation.A"),
            _g1j(j, ".ps_presentation.B")
        );
        IdentityRegistry.RegistrationProof memory proof;
        proof.e    = _u(j, ".registration_proof.e");
        proof.s_m  = _u(j, ".registration_proof.s_m");
        proof.s_r  = _u(j, ".registration_proof.s_r");
        proof.s_sk = _u(j, ".registration_proof.s_sk");
        proof.s_b = _u(j, ".registration_proof.s_b");
        proof.C1 = _g1j(j, ".registration_proof.C1");
        proof.T_C  = _g1j(j, ".registration_proof.T_C");
        proof.T_R  = _g1j(j, ".registration_proof.T_R");
        proof.T_key = _g1j(j, ".registration_proof.T_key");

        vm.prank(who);
        reg.register(ISSUER_ADDR, pk, E, sigma, proof);
    }

    // ---- identity root ----------------------------------------------------

    function test_vector_identityRoot_parses() public view {
        assertTrue(expectedIdentityRoot != 0, "identity root must be non-zero");
        assertTrue(expectedIdentityRoot < BN254.R, "identity root in field");
    }

    function test_vector_aliceRegisters() public {
        _registerFromVectors(rjAlice, alice);
        assertTrue(reg.isVerified(alice));
        assertEq(reg.issuerOf(alice), ISSUER_ADDR);

        // Verify stored pk matches the vector.
        BN254.G1Point memory storedPk = reg.pkOf(alice);
        BN254.G1Point memory expectedPk = _g1j(rjAlice, ".elgamal_kp.pk");
        assertTrue(BN254.eq(storedPk, expectedPk), "stored pk mismatch");
    }

    function test_vector_bobRegisters() public {
        _registerFromVectors(rjBob, bob);
        assertTrue(reg.isVerified(bob));
        assertEq(reg.issuerOf(bob), ISSUER_ADDR);
    }

    function test_vector_bothRegister() public {
        _registerFromVectors(rjAlice, alice);
        _registerFromVectors(rjBob, bob);
        assertTrue(reg.isVerified(alice));
        assertTrue(reg.isVerified(bob));
    }

    // ---- identity root accumulator (V2) ------------------------------------

    address internal constant ROOT_AUTH = address(0xA07);
    address internal constant AGG       = address(0xA99);

    function _appointAggregator() internal {
        vm.prank(GOV);
        reg.setRootAuthority(ROOT_AUTH);
        vm.prank(ROOT_AUTH);
        reg.setAggregator(AGG);
    }

    /// @notice The aggregator posts the off-chain-computed aggregator root.
    function test_aggregator_postsIdentityRoot() public {
        _appointAggregator();
        vm.prank(AGG);
        reg.postIdentityRoot(expectedIdentityRoot, keccak256("leaf list"));
        assertEq(reg.identityRoot(), expectedIdentityRoot);
        assertEq(reg.rootSequence(), 1);
        assertEq(reg.rootPostedAt(expectedIdentityRoot), block.timestamp);
    }

    function test_identityRoot_onlyAggregator() public {
        _appointAggregator();
        vm.expectRevert(bytes("not aggregator"));
        reg.postIdentityRoot(expectedIdentityRoot, bytes32(0));
        vm.prank(GOV);
        vm.expectRevert(bytes("not aggregator"));
        reg.postIdentityRoot(expectedIdentityRoot, bytes32(0));
    }

    function test_identityRoot_rejectsZero() public {
        _appointAggregator();
        vm.prank(AGG);
        vm.expectRevert(bytes("bad root"));
        reg.postIdentityRoot(0, bytes32(0));
    }

    /// @notice Full flow: register both parties, post the identity root.
    function test_fullRegistrationPlusIdentityRoot() public {
        _registerFromVectors(rjAlice, alice);
        _registerFromVectors(rjBob, bob);

        _appointAggregator();
        vm.prank(AGG);
        reg.postIdentityRoot(expectedIdentityRoot, bytes32(0));

        assertEq(reg.identityRoot(), expectedIdentityRoot);
        assertTrue(reg.isVerified(alice));
        assertTrue(reg.isVerified(bob));
    }

    // ---- admission is never a registry call ---------------------------------

    function test_callerLeaf_singleLeaf_refused() public {
        // A caller-supplied identityLeaf is refused, permanently: even Alice's
        // honest leaf, because admission is the certifier's, not the caller's.
        address poseidonAddr = PoseidonT3Bytecode.deploy();
        vm.prank(GOV);
        reg.setIdentityPoseidon(poseidonAddr);
        assertEq(reg.identityPoseidon(), poseidonAddr);

        uint256 aliceLeaf = _u(rjAlice, ".leaf");
        _expectUncheckedLeaf(rjAlice, alice, aliceLeaf);
        assertEq(reg.identityRoot(), 0);
        assertEq(reg.rootSequence(), 0);
        assertFalse(reg.isVerified(alice));
    }

    function test_callerLeaf_twoLeaves_refused() public {
        address poseidonAddr = PoseidonT3Bytecode.deploy();
        vm.prank(GOV);
        reg.setIdentityPoseidon(poseidonAddr);

        _expectUncheckedLeaf(rjAlice, alice, _u(rjAlice, ".leaf"));
        _expectUncheckedLeaf(rjBob,   bob,   _u(rjBob, ".leaf"));
        assertEq(reg.identityRoot(), 0);
        assertEq(reg.rootSequence(), 0);
    }

    function test_aggregatorPostsWithPoseidonWired() public {
        address poseidonAddr = PoseidonT3Bytecode.deploy();
        vm.prank(GOV);
        reg.setIdentityPoseidon(poseidonAddr);

        _appointAggregator();
        vm.prank(AGG);
        reg.postIdentityRoot(expectedIdentityRoot, bytes32(0));
        assertEq(reg.identityRoot(), expectedIdentityRoot);
    }

    function test_registerPostsNothing() public {
        address poseidonAddr = PoseidonT3Bytecode.deploy();
        vm.prank(GOV);
        reg.setIdentityPoseidon(poseidonAddr);

        // Register WITHOUT a leaf (5-arg overload) -- nothing is posted.
        _registerFromVectors(rjAlice, alice);
        assertEq(reg.identityRoot(), 0,
                "register must not post a root");
        assertEq(reg.rootSequence(), 0,
                "register must not post a root");
    }

    // ---- helpers with leaf -------------------------------------------------

    function _expectUncheckedLeaf(
        string memory j, address who, uint256 leaf
    ) internal {
        BN254.G1Point memory pk = _g1j(j, ".elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT(
            _g1j(j, ".ciphertext.R"),
            _g1j(j, ".ciphertext.C")
        );
        IdentityRegistry.PSPresentation memory sigma = IdentityRegistry.PSPresentation(
            _g1j(j, ".ps_presentation.A"),
            _g1j(j, ".ps_presentation.B")
        );
        IdentityRegistry.RegistrationProof memory proof;
        proof.e    = _u(j, ".registration_proof.e");
        proof.s_m  = _u(j, ".registration_proof.s_m");
        proof.s_r  = _u(j, ".registration_proof.s_r");
        proof.s_sk = _u(j, ".registration_proof.s_sk");
        proof.s_b = _u(j, ".registration_proof.s_b");
        proof.C1 = _g1j(j, ".registration_proof.C1");
        proof.T_C  = _g1j(j, ".registration_proof.T_C");
        proof.T_R  = _g1j(j, ".registration_proof.T_R");
        proof.T_key = _g1j(j, ".registration_proof.T_key");

        vm.prank(who);
        vm.expectRevert(bytes("unchecked identity leaf"));
        reg.register(ISSUER_ADDR, pk, E, sigma, proof, leaf);
    }
}
