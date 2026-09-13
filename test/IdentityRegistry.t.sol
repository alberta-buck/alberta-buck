// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";

/// @title IdentityRegistry.t.sol — register / verifyApprove parity tests.
/// @notice Replays the JSON vectors emitted by alberta_buck.wallet.cli through
///         the on-chain verifier.  Same chain id 1 the wallet uses.
contract IdentityRegistryTest is Test {

    IdentityRegistry internal reg;
    address internal constant GOV = address(0xA0);
    address internal constant ISSUER = address(0x1551E1);
    address internal constant REGISTRY_ADDR =
        0x1D1D1D1d1d1D1D1d1d1D1D1d1d1D1d1d1d1d1D1D;

    address internal alice;
    address internal bob;

    string internal vj;

    function setUp() public {
        // The wallet's transcripts use chainid = 1.
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");
        deployCodeTo(
            "IdentityRegistry.sol:IdentityRegistry",
            abi.encode(GOV),
            REGISTRY_ADDR
        );
        reg = IdentityRegistry(REGISTRY_ADDR);

        alice = address(uint160(_u(".alice.registrant")));
        bob   = address(uint160(_u(".bob.registrant")));

        // Trust the issuer using the issuer PSPubKey from the vectors.
        IdentityRegistry.PSPubKey memory ipk;
        ipk.X.X[0] = _u(".issuer.pk_X.x[0]");
        ipk.X.X[1] = _u(".issuer.pk_X.x[1]");
        ipk.X.Y[0] = _u(".issuer.pk_X.y[0]");
        ipk.X.Y[1] = _u(".issuer.pk_X.y[1]");
        ipk.Y.X[0] = _u(".issuer.pk_Y.x[0]");
        ipk.Y.X[1] = _u(".issuer.pk_Y.x[1]");
        ipk.Y.Y[0] = _u(".issuer.pk_Y.y[0]");
        ipk.Y.Y[1] = _u(".issuer.pk_Y.y[1]");

        vm.prank(GOV);
        reg.trustIssuer(ISSUER, ipk);
    }

    // ---- helpers -----------------------------------------------------------

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }

    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }

    function _ps(string memory who) internal view returns (IdentityRegistry.PSSig memory s) {
        s.sigma_1 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_1"));
        s.sigma_2 = _g1(string.concat(".", who, ".ps_sig_rerand.sigma_2"));
    }

    function _ct(string memory key) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(key, ".R"));
        c.C = _g1(string.concat(key, ".C"));
    }

    function _regProof(string memory who) internal view returns (IdentityRegistry.RegistrationProof memory p) {
        string memory base = string.concat(".", who, ".registration_proof");
        p.e    = _u(string.concat(base, ".e"));
        p.s_m  = _u(string.concat(base, ".s_m"));
        p.s_r  = _u(string.concat(base, ".s_r"));
        p.s_sk = _u(string.concat(base, ".s_sk"));
        p.A_ps = _g1(string.concat(base, ".A_ps"));
        p.T_C  = _g1(string.concat(base, ".T_C"));
        p.T_R  = _g1(string.concat(base, ".T_R"));
        p.T_key = _g1(string.concat(base, ".T_key"));
    }

    function _cpProof() internal view returns (IdentityRegistry.CPProof memory p) {
        p.e  = _u(".approve.cp_proof.e");
        p.s1 = _u(".approve.cp_proof.s1");
        p.s2 = _u(".approve.cp_proof.s2");
        p.T1 = _g1(".approve.cp_proof.T1");
        p.T2 = _g1(".approve.cp_proof.T2");
        p.T3 = _g1(".approve.cp_proof.T3");
    }

    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }

    function _registerBob() internal {
        BN254.G1Point memory pk = _g1(".bob.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".bob.ciphertext");
        vm.prank(bob);
        reg.register(ISSUER, pk, E, _ps("bob"), _regProof("bob"));
    }

    // ---- governance --------------------------------------------------------

    function test_constructor_setsGovernance() public view {
        assertEq(reg.governance(), GOV);
    }

    function test_constructor_rejectsZeroGovernance() public {
        vm.expectRevert(bytes("governance=0"));
        new IdentityRegistry(address(0));
    }

    function test_trustIssuer_onlyGovernance() public {
        IdentityRegistry.PSPubKey memory ipk;
        vm.expectRevert(bytes("not governance"));
        reg.trustIssuer(address(0xCAFE), ipk);
    }

    function test_revokeIssuer_clearsFlag() public {
        assertTrue(reg.isTrustedIssuer(ISSUER));
        vm.prank(GOV);
        reg.revokeIssuer(ISSUER);
        assertFalse(reg.isTrustedIssuer(ISSUER));
    }

    function test_transferGovernance() public {
        vm.prank(GOV);
        reg.transferGovernance(address(0xB0));
        assertEq(reg.governance(), address(0xB0));
    }

    // ---- registration ------------------------------------------------------

    function test_register_alice_succeeds() public {
        _registerAlice();
        assertTrue(reg.isVerified(alice));
        assertEq(reg.issuerOf(alice), ISSUER);

        BN254.G1Point memory storedPk = reg.pkOf(alice);
        BN254.G1Point memory expected = _g1(".alice.elgamal_kp.pk");
        assertTrue(BN254.eq(storedPk, expected), "stored pk mismatch");
    }

    function test_register_bob_succeeds() public {
        _registerBob();
        assertTrue(reg.isVerified(bob));
    }

    function test_register_rejects_double_registration() public {
        _registerAlice();
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        vm.expectRevert(bytes("already registered"));
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }

    function test_register_rejects_untrusted_issuer() public {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        vm.expectRevert(bytes("untrusted issuer"));
        reg.register(address(0xDEAD), pk, E, _ps("alice"), _regProof("alice"));
    }

    function test_register_rejects_replay_under_other_address() public {
        // Alice's proof was Fiat-Shamired against Alice's address.  Submitting
        // it from anyone else must fail at the FS step.
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(address(0xDEADBEEF));
        vm.expectRevert(bytes("bad FS challenge"));
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }

    function test_register_rejects_tampered_e() public {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        IdentityRegistry.RegistrationProof memory p = _regProof("alice");
        p.e = (p.e + 1) % BN254.R;
        vm.prank(alice);
        vm.expectRevert(bytes("bad FS challenge"));
        reg.register(ISSUER, pk, E, _ps("alice"), p);
    }

    function test_register_rejects_zero_sigma1() public {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        IdentityRegistry.PSSig memory bad = _ps("alice");
        bad.sigma_1 = BN254.zeroG1();
        vm.prank(alice);
        vm.expectRevert(bytes("sigma_1=O"));
        reg.register(ISSUER, pk, E, bad, _regProof("alice"));
    }

    function test_register_rejects_zero_pk() public {
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.prank(alice);
        vm.expectRevert(bytes("pk=O"));
        reg.register(ISSUER, BN254.zeroG1(), E, _ps("alice"), _regProof("alice"));
    }

    function test_register_rejects_zero_R() public {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        E.R = BN254.zeroG1();
        vm.prank(alice);
        vm.expectRevert(bytes("R=O"));
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }

    function test_register_rejects_noncanonical_scalar() public {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        IdentityRegistry.RegistrationProof memory p = _regProof("alice");
        p.s_sk = p.s_sk + BN254.R;
        vm.prank(alice);
        vm.expectRevert(bytes("bad scalar"));
        reg.register(ISSUER, pk, E, _ps("alice"), p);
    }

    function test_register_rejects_wrong_chainid() public {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        vm.chainId(2);
        vm.prank(alice);
        vm.expectRevert(bytes("bad FS challenge"));
        reg.register(ISSUER, pk, E, _ps("alice"), _regProof("alice"));
    }

    function test_register_rejects_otherRegistry() public {
        IdentityRegistry other = new IdentityRegistry(GOV);
        IdentityRegistry.PSPubKey memory ipk;
        ipk.X.X[0] = _u(".issuer.pk_X.x[0]");
        ipk.X.X[1] = _u(".issuer.pk_X.x[1]");
        ipk.X.Y[0] = _u(".issuer.pk_X.y[0]");
        ipk.X.Y[1] = _u(".issuer.pk_X.y[1]");
        ipk.Y.X[0] = _u(".issuer.pk_Y.x[0]");
        ipk.Y.X[1] = _u(".issuer.pk_Y.x[1]");
        ipk.Y.Y[0] = _u(".issuer.pk_Y.y[0]");
        ipk.Y.Y[1] = _u(".issuer.pk_Y.y[1]");
        vm.prank(GOV);
        other.trustIssuer(ISSUER, ipk);

        vm.prank(alice);
        vm.expectRevert(bytes("bad FS challenge"));
        other.register(
            ISSUER,
            _g1(".alice.elgamal_kp.pk"),
            _ct(".alice.ciphertext"),
            _ps("alice"),
            _regProof("alice")
        );
    }

    // ---- bindContract ------------------------------------------------------
    //
    // The 5-arg path is the already-certified-operator exception: the binder
    // must be registered and the supplied (pk, E) must equal that identity.
    // Alice's vector credential is the honest control.

    function _alicePk() internal view returns (BN254.G1Point memory) {
        return _g1(".alice.elgamal_kp.pk");
    }

    function _aliceE() internal view returns (IdentityRegistry.ElGamalCT memory) {
        return _ct(".alice.ciphertext");
    }

    function _planAt(address target) internal {
        vm.etch(target, hex"60006000fd");
    }

    function _bindPoolAsAlice(address pool, bool isPublic, bool isCarrying) internal {
        _registerAlice();
        _planAt(pool);
        vm.prank(alice);
        reg.bindContract(pool, _alicePk(), _aliceE(), isPublic, isCarrying);
    }

    function test_bindContract_rejectsEOA() public {
        // Alice is an EOA -- her address has no code, so bindContract refuses
        // before the binder-registered check.
        vm.expectRevert(bytes("target not a deployed contract"));
        reg.bindContract(
            alice,
            BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}),
            true,
            true
        );
    }

    function test_bindContract_rejectsUnregisteredBinder() public {
        address pool = address(0xDECAF);
        _planAt(pool);
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});
        vm.expectRevert(bytes("binder not registered"));
        reg.bindContract(pool, BN254.g1(), E, true, true);
    }

    function test_bindContract_rejectsMismatchedPkE() public {
        _registerAlice();
        address pool = address(0xDECAF);
        _planAt(pool);
        IdentityRegistry.ElGamalCT memory junk =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});
        vm.prank(alice);
        vm.expectRevert(bytes("uncertified identity"));
        reg.bindContract(pool, BN254.g1(), junk, true, true);
    }

    function test_bindContract_succeedsForDeployedContract_public() public {
        address pool = address(0xDECAF);
        _bindPoolAsAlice(pool, true, true);

        assertTrue(reg.isVerified(pool),         "pool now verified");
        assertTrue(reg.isPublicIdentity(pool),   "pool is Public Identity");
        assertTrue(reg.isCarrying(pool),         "pool is Carrying");
        assertEq(reg.binderOf(pool), alice,      "binder is alice");
        BN254.G1Point memory storedPk = reg.pkOf(pool);
        assertTrue(BN254.eq(storedPk, _alicePk()), "stored pk matches binder");
        assertEq(reg.issuerOf(pool), ISSUER,     "issuer copied from binder");
    }

    function test_bindContract_succeedsForDeployedContract_encrypted() public {
        address vault = address(0xBADD);
        _bindPoolAsAlice(vault, false, false);

        assertTrue(reg.isVerified(vault),          "vault now verified");
        assertFalse(reg.isPublicIdentity(vault),   "vault is encrypted Identity");
        assertFalse(reg.isCarrying(vault),         "vault is Non-Carrying (user wallet)");
        assertEq(reg.binderOf(vault), alice);
    }

    function test_bindContract_firstBinderWins() public {
        address pool = address(0xDECAF);
        _bindPoolAsAlice(pool, true, true);

        vm.prank(alice);
        vm.expectRevert(bytes("already bound"));
        reg.bindContract(pool, _alicePk(), _aliceE(), true, true);
    }

    function test_bindContract_rejectsUncheckedLeaf() public {
        _registerAlice();
        address pool = address(0xDECAF);
        _planAt(pool);
        vm.prank(alice);
        vm.expectRevert(bytes("unchecked identity leaf"));
        reg.bindContract(pool, _alicePk(), _aliceE(), true, true, 12345);
    }

    function test_bindContract_leafZeroIsCertifiedOperatorPath() public {
        _registerAlice();
        address pool = address(0xDECAF);
        _planAt(pool);
        vm.prank(alice);
        reg.bindContract(pool, _alicePk(), _aliceE(), true, true, 0);
        assertTrue(reg.isVerified(pool));
        assertEq(reg.identityRoot(), 0, "leaf=0 must not touch the accumulator");
    }

    function test_bindContract_rejectsRegisterProofBoundToBinder() public {
        // Alice's registration proof is Fiat-Shamired against Alice, not the
        // pool.  Replaying it on the credential bind overload must fail FS.
        _registerAlice();
        address pool = address(0xDECAF);
        _planAt(pool);
        vm.prank(alice);
        vm.expectRevert(bytes("bad FS challenge"));
        reg.bindContract(
            pool, ISSUER, _alicePk(), _aliceE(), _ps("alice"), _regProof("alice"),
            true, true
        );
    }

    function test_register_rejectsUncheckedLeaf() public {
        vm.prank(alice);
        vm.expectRevert(bytes("unchecked identity leaf"));
        reg.register(ISSUER, _alicePk(), _aliceE(), _ps("alice"), _regProof("alice"), 12345);
    }

    function test_register_leafZeroStillRegisters() public {
        vm.prank(alice);
        reg.register(ISSUER, _alicePk(), _aliceE(), _ps("alice"), _regProof("alice"), 0);
        assertTrue(reg.isVerified(alice));
        assertEq(reg.identityRoot(), 0);
    }

    // ---- verifyApprove -----------------------------------------------------

    function test_verifyApprove_validProof() public {
        _registerAlice();
        _registerBob();
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        assertTrue(reg.verifyApprove(alice, bob, E_b, _cpProof()));
    }

    function test_verifyApprove_rejects_unregistered_sender() public {
        _registerBob();
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        assertFalse(reg.verifyApprove(alice, bob, E_b, _cpProof()));
    }

    function test_verifyApprove_rejects_unregistered_spender() public {
        _registerAlice();
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        assertFalse(reg.verifyApprove(alice, bob, E_b, _cpProof()));
    }

    function test_verifyApprove_rejects_wrong_spender_in_transcript() public {
        _registerAlice();
        _registerBob();
        // Use a third registered party as the spender at call time (proof is
        // Fiat-Shamired over Bob, so FS must fail).
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        assertFalse(reg.verifyApprove(alice, address(0xCAFE), E_b, _cpProof()));
    }

    function test_verifyApprove_rejects_wrong_chainid() public {
        _registerAlice();
        _registerBob();
        vm.chainId(2);
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        assertFalse(reg.verifyApprove(alice, bob, E_b, _cpProof()));
    }

    function test_verifyApprove_rejects_otherRegistry() public {
        IdentityRegistry other = new IdentityRegistry(GOV);

        // Give the second registry identical public records.  With every
        // other transcript input held constant, only its address differs.
        vm.etch(alice, hex"60006000fd");
        vm.etch(bob, hex"60006000fd");
        other.bindContract(
            alice,
            _g1(".alice.elgamal_kp.pk"),
            _ct(".alice.ciphertext"),
            false,
            false
        );
        other.bindContract(
            bob,
            _g1(".bob.elgamal_kp.pk"),
            _ct(".bob.ciphertext"),
            false,
            false
        );

        assertFalse(other.verifyApprove(
            alice, bob, _ct(".approve.E_for_bob"), _cpProof()
        ));
    }

    function test_verifyApprove_rejects_tampered_e() public {
        _registerAlice();
        _registerBob();
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        IdentityRegistry.CPProof memory bad = _cpProof();
        bad.e = (bad.e + 1) % BN254.R;
        assertFalse(reg.verifyApprove(alice, bob, E_b, bad));
    }

    function test_verifyApprove_rejects_noncanonical_scalar() public {
        _registerAlice();
        _registerBob();
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        IdentityRegistry.CPProof memory bad = _cpProof();
        bad.s1 = bad.s1 + BN254.R;
        assertFalse(reg.verifyApprove(alice, bob, E_b, bad));
    }

    function test_verifyApprove_rejects_zero_R() public {
        _registerAlice();
        _registerBob();
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        E_b.R = BN254.zeroG1();
        assertFalse(reg.verifyApprove(alice, bob, E_b, _cpProof()));
    }

    // ---- setBuck -----------------------------------------------------------

    function test_setBuck_governance_oneTime() public {
        address fakeBuck  = address(0xB0CC);
        address fakeBuck2 = address(0xB0DD);

        // Non-governance caller is rejected.
        vm.expectRevert(bytes("not governance"));
        reg.setBuck(fakeBuck);

        // Governance succeeds; buck is set.
        vm.prank(GOV);
        reg.setBuck(fakeBuck);
        assertEq(reg.buck(), fakeBuck);

        // Second call (even from governance) reverts.
        vm.expectRevert(bytes("buck already set"));
        vm.prank(GOV);
        reg.setBuck(fakeBuck2);

        // Zero buck rejected on a fresh registry.
        IdentityRegistry fresh = new IdentityRegistry(GOV);
        vm.expectRevert(bytes("buck=0"));
        vm.prank(GOV);
        fresh.setBuck(address(0));
    }

    // ---- setIsCarrying / markApproved freeze -------------------------------

    function test_setIsCarrying_onlyBinder() public {
        address pool = address(0xDECAF);
        _bindPoolAsAlice(pool, true, true);

        // Random caller cannot flip the flag.
        vm.expectRevert(bytes("not binder"));
        reg.setIsCarrying(pool, false);

        // Binder can.
        vm.prank(alice);
        reg.setIsCarrying(pool, false);
        assertFalse(reg.isCarrying(pool));
        vm.prank(alice);
        reg.setIsCarrying(pool, true);
        assertTrue(reg.isCarrying(pool));
    }

    function test_markApproved_onlyBuck() public {
        address pool = address(0xDECAF);
        _bindPoolAsAlice(pool, true, true);

        // Without a buck set, no caller can markApproved.
        vm.expectRevert(bytes("only Buck"));
        reg.markApproved(pool);

        // After setBuck, only that address can call.
        address fakeBuck = address(0xB0CC);
        vm.prank(GOV);
        reg.setBuck(fakeBuck);

        vm.prank(alice);
        vm.expectRevert(bytes("only Buck"));
        reg.markApproved(pool);

        // Buck succeeds.
        vm.prank(fakeBuck);
        reg.markApproved(pool);
        assertTrue(reg.carryingFrozen(pool));

        // Idempotent (no revert on re-call).
        vm.prank(fakeBuck);
        reg.markApproved(pool);
    }

    function test_setIsCarrying_revertsAfterFreeze() public {
        address pool = address(0xDECAF);
        _bindPoolAsAlice(pool, true, true);

        // Buck-side approval freezes the flag.
        address fakeBuck = address(0xB0CC);
        vm.prank(GOV);
        reg.setBuck(fakeBuck);
        vm.prank(fakeBuck);
        reg.markApproved(pool);

        // Binder can no longer change isCarrying.
        vm.prank(alice);
        vm.expectRevert(bytes("carrying frozen by approval"));
        reg.setIsCarrying(pool, false);

        // The original value is preserved.
        assertTrue(reg.isCarrying(pool));
    }

    function test_setIsCarrying_targetingEOA_reverts() public {
        // EOAs have binderOf == address(0), so setIsCarrying reverts for any
        // caller (no one can match address(0) as msg.sender from a real tx).
        _registerAlice();
        vm.expectRevert(bytes("not binder"));
        reg.setIsCarrying(alice, true);
    }
}
