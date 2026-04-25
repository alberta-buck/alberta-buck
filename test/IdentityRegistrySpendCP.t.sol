// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {BN254} from "../src/BN254.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";

/// @title IdentityRegistrySpendCP.t.sol — Phase 8 V2 A-spend CP-DLEQ verifier parity tests.
/// @notice Replays JSON vectors emitted by alberta_buck.wallet.cli through
///         IdentityRegistry.verifySpendCP, matching the threat model the Python
///         test suite covers (honest spend, identity re-issuance, tampered
///         transcript, cross-chain/recipient replay).
contract IdentityRegistrySpendCPTest is Test {

    IdentityRegistry internal reg;
    address internal constant GOV    = address(0xA0);
    address internal constant ISSUER = address(0x1551E1);

    address internal alice;
    address internal recipient;

    string internal vj;

    function setUp() public {
        // Wallet transcripts use chainid = 1.
        vm.chainId(1);
        vj  = vm.readFile("test/vectors/identity.json");
        reg = new IdentityRegistry(GOV);

        alice     = address(uint160(_u(".alice.registrant")));
        recipient = address(uint160(_u(".spend_cp.recipient")));

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

        _registerAlice();
    }

    // ---- helpers -----------------------------------------------------------

    function _u(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vj, key);
    }

    function _g1(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point(_u(string.concat(key, ".x")), _u(string.concat(key, ".y")));
    }

    function _ct(string memory key) internal view returns (IdentityRegistry.ElGamalCT memory c) {
        c.R = _g1(string.concat(key, ".R"));
        c.C = _g1(string.concat(key, ".C"));
    }

    function _registerAlice() internal {
        BN254.G1Point memory pk = _g1(".alice.elgamal_kp.pk");
        IdentityRegistry.ElGamalCT memory E = _ct(".alice.ciphertext");
        IdentityRegistry.PSSig memory sig;
        sig.sigma_1 = _g1(".alice.ps_sig_rerand.sigma_1");
        sig.sigma_2 = _g1(".alice.ps_sig_rerand.sigma_2");
        IdentityRegistry.RegistrationProof memory p;
        p.e    = _u(".alice.registration_proof.e");
        p.s_m  = _u(".alice.registration_proof.s_m");
        p.s_r  = _u(".alice.registration_proof.s_r");
        p.A_ps = _g1(".alice.registration_proof.A_ps");
        p.T_C  = _g1(".alice.registration_proof.T_C");
        p.T_R  = _g1(".alice.registration_proof.T_R");
        vm.prank(alice);
        reg.register(ISSUER, pk, E, sig, p);
    }

    function _eN() internal view returns (IdentityRegistry.ElGamalCT memory) {
        return _ct(".spend_cp.E_n");
    }

    function _proof() internal view returns (IdentityRegistry.SpendCPProof memory pi) {
        pi.e  = _u(".spend_cp.proof.e");
        pi.s  = _u(".spend_cp.proof.s");
        pi.T1 = _g1(".spend_cp.proof.T1");
        pi.T2 = _g1(".spend_cp.proof.T2");
    }

    // ---- positive ----------------------------------------------------------

    function test_verifySpendCP_validProof() public view {
        assertTrue(reg.verifySpendCP(alice, recipient, _eN(), _proof()));
    }

    // ---- negative: identity / registration -------------------------------

    function test_verifySpendCP_unregisteredSpender() public view {
        // address(0xDEAD) was never registered -> short-circuit false.
        assertFalse(reg.verifySpendCP(address(0xDEAD), recipient, _eN(), _proof()));
    }

    // ---- negative: tampered proof fields -------------------------------

    function test_verifySpendCP_tampered_e() public view {
        IdentityRegistry.SpendCPProof memory pi = _proof();
        pi.e = addmod(pi.e, 1, BN254.R);
        assertFalse(reg.verifySpendCP(alice, recipient, _eN(), pi));
    }

    function test_verifySpendCP_tampered_s() public view {
        IdentityRegistry.SpendCPProof memory pi = _proof();
        pi.s = addmod(pi.s, 1, BN254.R);
        assertFalse(reg.verifySpendCP(alice, recipient, _eN(), pi));
    }

    function test_verifySpendCP_tampered_T1() public view {
        IdentityRegistry.SpendCPProof memory pi = _proof();
        pi.T1 = BN254.add(pi.T1, BN254.g1());        // perturb by +G
        assertFalse(reg.verifySpendCP(alice, recipient, _eN(), pi));
    }

    function test_verifySpendCP_tampered_T2() public view {
        IdentityRegistry.SpendCPProof memory pi = _proof();
        pi.T2 = BN254.add(pi.T2, BN254.g1());        // perturb by +G
        assertFalse(reg.verifySpendCP(alice, recipient, _eN(), pi));
    }

    // ---- negative: tampered ciphertext ----------------------------------

    function test_verifySpendCP_tampered_C() public view {
        IdentityRegistry.ElGamalCT memory eN = _eN();
        eN.C = BN254.add(eN.C, BN254.g1());          // perturb by +G
        assertFalse(reg.verifySpendCP(alice, recipient, eN, _proof()));
    }

    function test_verifySpendCP_tampered_R() public view {
        IdentityRegistry.ElGamalCT memory eN = _eN();
        eN.R = BN254.add(eN.R, BN254.g1());          // perturb by +G
        assertFalse(reg.verifySpendCP(alice, recipient, eN, _proof()));
    }

    // ---- negative: replay across recipient / chain ----------------------

    function test_verifySpendCP_wrongRecipient() public view {
        // FS binds recipient -- a proof for `recipient` cannot verify against
        // anyone else.
        assertFalse(reg.verifySpendCP(alice, address(0xCAFE), _eN(), _proof()));
    }

    function test_verifySpendCP_wrongChainid() public {
        // Switch chain mid-test; FS recompute on chain 2 yields a different
        // challenge so the proof must reject.
        vm.chainId(2);
        assertFalse(reg.verifySpendCP(alice, recipient, _eN(), _proof()));
    }
}
