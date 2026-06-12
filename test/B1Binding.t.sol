// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BN254} from "../src/BN254.sol";

/// @notice Cross-artifact parity for the B1 depositor binding (the dual of the
///         A2 issuer binding): IdentityRegistry.verifyDepositorBinding accepts
///         the canonical proof emitted by alberta_buck.wallet.b1_binding
///         (scripts/gen_b1_binding_vectors.py -> test/vectors/b1_binding.json).
///         The full flow (bearer spend, the issuer scanning SpentB and decrypting
///         the depositor's Identity into an issuer-unilateral receipt) is
///         demonstrated in alberta_buck/test/test_b1_binding.py; this suite pins
///         the EVM gate.  See alberta-buck-notes-identity-axis.org.
contract B1BindingTest is Test {
    IdentityRegistry internal reg;
    address internal constant GOV = address(0xA0);
    string  internal vj;
    address internal depositor;
    address internal issuer;

    function setUp() public {
        vm.chainId(1);
        vj  = vm.readFile("test/vectors/b1_binding.json");
        reg = new IdentityRegistry(GOV);

        // Both parties are registered: the depositor's payout account and the
        // public bearer issuer (whose pk the binding re-encrypts toward).
        depositor = address(uint160(_u(".depositor.addr")));
        issuer    = address(uint160(_u(".issuer.addr")));
        vm.etch(depositor, hex"60006000fd");
        vm.etch(issuer,    hex"60006000fd");
        reg.bindContract(depositor, _g1(".depositor.pk"), _ct(".depositor.E"),    false, false);
        reg.bindContract(issuer,    _g1(".issuer.pk"),    _ct(".issuer.E_reg"),   true,  false);
    }

    // ---- vector helpers -----------------------------------------------------

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
    function _eDepForIss() internal view returns (IdentityRegistry.ElGamalCT memory) {
        return _ct(".eDepForIss");
    }
    function _proof() internal view returns (IdentityRegistry.DepositorBindingProof memory p) {
        p.e     = _u(".depositor_binding.e");
        p.s_m   = _u(".depositor_binding.s_m");
        p.s_s   = _u(".depositor_binding.s_s");
        p.s_r   = _u(".depositor_binding.s_r");
        p.s_b   = _u(".depositor_binding.s_b");
        p.A2    = _g1(".depositor_binding.A2");
        p.A4    = _g1(".depositor_binding.A4");
        p.B1    = _g1(".depositor_binding.B1");
        p.B2    = _g1(".depositor_binding.B2");
        p.A_p   = _g1(".depositor_binding.A_p");
        p.P_dep = _g1(".depositor_binding.P_dep");
    }

    // ---- completeness -------------------------------------------------------

    function test_vector_validDepositorBinding_verifies() public {
        assertTrue(reg.verifyDepositorBinding(depositor, issuer, _eDepForIss(), _proof()),
                   "python-reference B1 depositor binding must verify on-chain");
    }

    // ---- soundness ----------------------------------------------------------

    function test_vector_tamperedResponse_rejected() public {
        IdentityRegistry.DepositorBindingProof memory p = _proof();
        p.s_m = addmod(p.s_m, 1, BN254.R);          // breaks E2/F2
        assertFalse(reg.verifyDepositorBinding(depositor, issuer, _eDepForIss(), p));
    }

    function test_vector_substitutedCiphertext_rejected() public {
        // A ciphertext over a different identity breaks F2 (coupled to E2 via m_dep)
        // -- the depositor cannot hide/frame.
        IdentityRegistry.ElGamalCT memory bad = _eDepForIss();
        bad.C = BN254.add(bad.C, BN254.g1());
        assertFalse(reg.verifyDepositorBinding(depositor, issuer, bad, _proof()));
    }

    function test_vector_wrongIssuer_rejected() public {
        // Verifying against a different (registered) issuer key breaks F2 + FS.
        address other = address(uint160(0xBEEF));
        vm.etch(other, hex"60006000fd");
        reg.bindContract(other, _g1(".depositor.pk"), _ct(".depositor.E"), true, false);
        assertFalse(reg.verifyDepositorBinding(depositor, other, _eDepForIss(), _proof()));
    }

    function test_vector_wrongChainid_rejected() public {
        vm.chainId(2);
        assertFalse(reg.verifyDepositorBinding(depositor, issuer, _eDepForIss(), _proof()));
    }

    function test_vector_unregisteredDepositor_rejected() public {
        address other = address(uint160(0xdead));
        assertFalse(reg.verifyDepositorBinding(other, issuer, _eDepForIss(), _proof()));
    }
}
