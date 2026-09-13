// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityMembershipVerifier} from "../src/IdentityMembershipVerifier.sol";
import {PoseidonT3Bytecode} from "../src/PoseidonT3Bytecode.sol";
import {IPoseidonT3} from "../src/IPoseidonT3.sol";
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

        reg = new IdentityRegistry(GOV);
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
        IdentityRegistry.PSSig memory sigma = IdentityRegistry.PSSig(
            _g1j(j, ".ps_sig_rerand.sigma_1"),
            _g1j(j, ".ps_sig_rerand.sigma_2")
        );
        IdentityRegistry.RegistrationProof memory proof;
        proof.e    = _u(j, ".registration_proof.e");
        proof.s_m  = _u(j, ".registration_proof.s_m");
        proof.s_r  = _u(j, ".registration_proof.s_r");
        proof.s_sk = _u(j, ".registration_proof.s_sk");
        proof.A_ps = _g1j(j, ".registration_proof.A_ps");
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

    /// @notice The identity root can be set by governance from an
    ///         off-chain-computed Merkle root (the registry's aggregator).
    function test_governance_setsIdentityRoot() public {
        vm.prank(GOV);
        reg.setIdentityRoot(expectedIdentityRoot);
        assertEq(reg.identityRoot(), expectedIdentityRoot);
    }

    function test_identityRoot_onlyGovernance() public {
        vm.expectRevert(bytes("not governance"));
        reg.setIdentityRoot(expectedIdentityRoot);
    }

    function test_identityRoot_rejectsZero() public {
        vm.prank(GOV);
        vm.expectRevert(bytes("root=0"));
        reg.setIdentityRoot(0);
    }

    /// @notice Full flow: register both parties, set the identity root,
    ///         verify the membership proof from the vectors against the
    ///         on-chain root using the IdentityMembershipVerifier.
    function test_fullRegistrationPlusIdentityRoot() public {
        _registerFromVectors(rjAlice, alice);
        _registerFromVectors(rjBob, bob);

        // Governance posts the off-chain-computed root.
        vm.prank(GOV);
        reg.setIdentityRoot(expectedIdentityRoot);

        assertEq(reg.identityRoot(), expectedIdentityRoot);
        assertTrue(reg.isVerified(alice));
        assertTrue(reg.isVerified(bob));
    }

    // ---- incremental accumulator (Phase B) ----------------------------------

    function test_incrementalAccumulator_singleLeaf() public {
        // Deploy Poseidon and wire it.
        address poseidonAddr = PoseidonT3Bytecode.deploy();
        vm.prank(GOV);
        reg.setIdentityPoseidon(poseidonAddr);
        assertEq(reg.identityPoseidon(), poseidonAddr);

        // Read alice's identity leaf from vectors.
        uint256 aliceLeaf = _u(rjAlice, ".leaf");

        // Register alice with the incremental leaf.
        _registerFromVectorsWithLeaf(rjAlice, alice, aliceLeaf);

        uint256 root = reg.identityRoot();
        assertTrue(root != 0, "root should be non-zero after insertion");
        assertEq(reg.identityNextLeafIndex(), 1);

        // Verify with Poseidon directly: single leaf tree, depth 10.
        uint256[2] memory pair;
        pair[0] = aliceLeaf;
        pair[1] = reg.IDENTITY_ZEROS(0);                       // ZERO_0 = 0
        uint256 cur = IPoseidonT3(poseidonAddr).poseidon(pair); // level 0
        for (uint8 d = 1; d < reg.IDENTITY_TREE_DEPTH(); d++) {
            pair[0] = cur;
            pair[1] = reg.IDENTITY_ZEROS(d);
            cur = IPoseidonT3(poseidonAddr).poseidon(pair);
        }
        assertEq(root, cur,
                "on-chain incremental root must match direct Poseidon computation");
    }

    function test_incrementalAccumulator_twoLeavesMatchesAggregator() public {
        address poseidonAddr = PoseidonT3Bytecode.deploy();
        vm.prank(GOV);
        reg.setIdentityPoseidon(poseidonAddr);

        uint256 aliceLeaf = _u(rjAlice, ".leaf");
        uint256 bobLeaf   = _u(rjBob, ".leaf");

        _registerFromVectorsWithLeaf(rjAlice, alice, aliceLeaf);
        _registerFromVectorsWithLeaf(rjBob,   bob,   bobLeaf);

        uint256 root = reg.identityRoot();
        assertEq(reg.identityNextLeafIndex(), 2);

        // The two-leaf on-chain root must match the Python registry sub_root
        // (which is a depth=10 IdentityMerkleTree with both leaves inserted).
        uint256 expectedSubRoot = vm.parseJsonUint(rjRoot, ".registry_sub_root");
        assertEq(root, expectedSubRoot,
                "two-leaf incremental root must match Python registry sub_root");
    }

    function test_incrementalAccumulator_governanceCanStillSet() public {
        // Governance-set root still works even with Poseidon wired.
        address poseidonAddr = PoseidonT3Bytecode.deploy();
        vm.prank(GOV);
        reg.setIdentityPoseidon(poseidonAddr);

        vm.prank(GOV);
        reg.setIdentityRoot(expectedIdentityRoot);
        assertEq(reg.identityRoot(), expectedIdentityRoot);
    }

    function test_incrementalAccumulator_zeroLeafSkipsUpdate() public {
        address poseidonAddr = PoseidonT3Bytecode.deploy();
        vm.prank(GOV);
        reg.setIdentityPoseidon(poseidonAddr);

        // Register WITHOUT a leaf (5-arg overload) — root stays 0.
        _registerFromVectors(rjAlice, alice);
        assertEq(reg.identityRoot(), 0,
                "5-arg register must not update identity root");
        assertEq(reg.identityNextLeafIndex(), 0,
                "leaf count must not advance with 5-arg register");
    }

    // ---- helpers with leaf -------------------------------------------------

    function _registerFromVectorsWithLeaf(
        string memory j, address who, uint256 leaf
    ) internal {
        BN254.G1Point memory pk = _g1j(j, ".elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = IdentityRegistry.ElGamalCT(
            _g1j(j, ".ciphertext.R"),
            _g1j(j, ".ciphertext.C")
        );
        IdentityRegistry.PSSig memory sigma = IdentityRegistry.PSSig(
            _g1j(j, ".ps_sig_rerand.sigma_1"),
            _g1j(j, ".ps_sig_rerand.sigma_2")
        );
        IdentityRegistry.RegistrationProof memory proof;
        proof.e    = _u(j, ".registration_proof.e");
        proof.s_m  = _u(j, ".registration_proof.s_m");
        proof.s_r  = _u(j, ".registration_proof.s_r");
        proof.s_sk = _u(j, ".registration_proof.s_sk");
        proof.A_ps = _g1j(j, ".registration_proof.A_ps");
        proof.T_C  = _g1j(j, ".registration_proof.T_C");
        proof.T_R  = _g1j(j, ".registration_proof.T_R");
        proof.T_key = _g1j(j, ".registration_proof.T_key");

        vm.prank(who);
        reg.register(ISSUER_ADDR, pk, E, sigma, proof, leaf);
    }
}
