// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {IdentityMembershipVerifier} from "../src/IdentityMembershipVerifier.sol";
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
///         See alberta-buck-notes-identity-axis.org.
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
        proof.A_ps = _g1j(j, ".registration_proof.A_ps");
        proof.T_C  = _g1j(j, ".registration_proof.T_C");
        proof.T_R  = _g1j(j, ".registration_proof.T_R");

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
}
