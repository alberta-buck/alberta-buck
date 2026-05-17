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

    address internal alice;
    address internal bob;

    string internal vj;

    function setUp() public {
        // The wallet's transcripts use chainid = 1.
        vm.chainId(1);
        vj = vm.readFile("test/vectors/identity.json");
        reg = new IdentityRegistry(GOV);

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
        p.A_ps = _g1(string.concat(base, ".A_ps"));
        p.T_C  = _g1(string.concat(base, ".T_C"));
        p.T_R  = _g1(string.concat(base, ".T_R"));
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

    // ---- bindContract ------------------------------------------------------

    function test_bindContract_rejectsEOA() public {
        // Alice is an EOA -- her address has no code, so bindContract refuses.
        vm.expectRevert(bytes("target not a deployed contract"));
        reg.bindContract(
            alice,
            BN254.g1(),
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()}),
            true,
            true
        );
    }

    function test_bindContract_succeedsForDeployedContract_public() public {
        address pool = address(0xDECAF);
        vm.etch(pool, hex"60006000fd");

        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});
        reg.bindContract(pool, BN254.g1(), E, true, true);

        assertTrue(reg.isVerified(pool),         "pool now verified");
        assertTrue(reg.isPublicIdentity(pool),   "pool is Public Identity");
        assertTrue(reg.isCarrying(pool),         "pool is Carrying");
        assertEq(reg.binderOf(pool), address(this), "binder is the test contract");
        BN254.G1Point memory storedPk = reg.pkOf(pool);
        assertTrue(BN254.eq(storedPk, BN254.g1()), "stored pk matches");
    }

    function test_bindContract_succeedsForDeployedContract_encrypted() public {
        // BUCK-aware contracts may bind under an encrypted Identity (operator
        // controls the off-chain sk that decrypts approve receipts).
        address vault = address(0xBADD);
        vm.etch(vault, hex"60006000fd");

        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});
        reg.bindContract(vault, BN254.g1(), E, false, false);

        assertTrue(reg.isVerified(vault),          "vault now verified");
        assertFalse(reg.isPublicIdentity(vault),   "vault is encrypted Identity");
        assertFalse(reg.isCarrying(vault),         "vault is Non-Carrying (user wallet)");
    }

    function test_bindContract_firstBinderWins() public {
        address pool = address(0xDECAF);
        vm.etch(pool, hex"60006000fd");

        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});
        reg.bindContract(pool, BN254.g1(), E, true, true);

        // Second bind reverts -- first binder owns the slot.
        vm.expectRevert(bytes("already bound"));
        reg.bindContract(pool, BN254.g1(), E, true, true);
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

    function test_verifyApprove_rejects_tampered_e() public {
        _registerAlice();
        _registerBob();
        IdentityRegistry.ElGamalCT memory E_b = _ct(".approve.E_for_bob");
        IdentityRegistry.CPProof memory bad = _cpProof();
        bad.e = (bad.e + 1) % BN254.R;
        assertFalse(reg.verifyApprove(alice, bob, E_b, bad));
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

    function _planAt(address target) internal {
        vm.etch(target, hex"60006000fd");
    }

    function test_setIsCarrying_onlyBinder() public {
        address pool = address(0xDECAF);
        _planAt(pool);
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});

        // The test contract is the binder.
        reg.bindContract(pool, BN254.g1(), E, true, true);

        // Random caller cannot flip the flag.
        vm.prank(alice);
        vm.expectRevert(bytes("not binder"));
        reg.setIsCarrying(pool, false);

        // Binder can.
        reg.setIsCarrying(pool, false);
        assertFalse(reg.isCarrying(pool));
        reg.setIsCarrying(pool, true);
        assertTrue(reg.isCarrying(pool));
    }

    function test_markApproved_onlyBuck() public {
        address pool = address(0xDECAF);
        _planAt(pool);
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});
        reg.bindContract(pool, BN254.g1(), E, true, true);

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
        _planAt(pool);
        IdentityRegistry.ElGamalCT memory E =
            IdentityRegistry.ElGamalCT({R: BN254.g1(), C: BN254.g1()});
        reg.bindContract(pool, BN254.g1(), E, true, true);

        // Buck-side approval freezes the flag.
        address fakeBuck = address(0xB0CC);
        vm.prank(GOV);
        reg.setBuck(fakeBuck);
        vm.prank(fakeBuck);
        reg.markApproved(pool);

        // Binder can no longer change isCarrying.
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

    // ---- verifySpendCP (A-spend CP-DLEQ, Phase 8 V2) ----------------------

    function _spendCPProof() internal view returns (IdentityRegistry.SpendCPProof memory p) {
        p.e  = _u(".spend_cp.proof.e");
        p.s  = _u(".spend_cp.proof.s");
        p.T1 = _g1(".spend_cp.proof.T1");
        p.T2 = _g1(".spend_cp.proof.T2");
    }

    function test_verifySpendCP_validProof() public {
        _registerAlice();
        IdentityRegistry.ElGamalCT memory E_n = _ct(".spend_cp.E_n");
        assertTrue(reg.verifySpendCP(alice, bob, E_n, _spendCPProof()));
    }

    function test_verifySpendCP_rejectsUnregisteredSpender() public {
        IdentityRegistry.ElGamalCT memory E_n = _ct(".spend_cp.E_n");
        assertFalse(reg.verifySpendCP(alice, bob, E_n, _spendCPProof()));
    }

    function test_verifySpendCP_rejectsWrongRecipient() public {
        _registerAlice();
        IdentityRegistry.ElGamalCT memory E_n = _ct(".spend_cp.E_n");
        // The proof is Fiat-Shamir bound to `recipient` (= bob in the vector).
        // Passing a different recipient must fail.
        assertFalse(reg.verifySpendCP(alice, address(0xCAFE), E_n, _spendCPProof()));
    }

    function test_verifySpendCP_rejectsWrongChainId() public {
        _registerAlice();
        vm.chainId(2);
        IdentityRegistry.ElGamalCT memory E_n = _ct(".spend_cp.E_n");
        assertFalse(reg.verifySpendCP(alice, bob, E_n, _spendCPProof()));
    }

    function test_verifySpendCP_rejectsTamperedE() public {
        _registerAlice();
        IdentityRegistry.ElGamalCT memory E_n = _ct(".spend_cp.E_n");
        IdentityRegistry.SpendCPProof memory bad = _spendCPProof();
        bad.e = (bad.e + 1) % BN254.R;
        assertFalse(reg.verifySpendCP(alice, bob, E_n, bad));
    }

    function test_verifySpendCP_rejectsWrongCiphertext() public {
        _registerAlice();
        // Use Alice's own registered ciphertext as E_n — it is a valid
        // curve point but the proof was generated against the spend_cp.E_n
        // ciphertext, so the algebraic checks fail.
        IdentityRegistry.ElGamalCT memory wrongE = _ct(".alice.ciphertext");
        assertFalse(reg.verifySpendCP(alice, bob, wrongE, _spendCPProof()));
    }
}
