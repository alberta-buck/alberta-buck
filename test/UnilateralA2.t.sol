// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BN254} from "../src/BN254.sol";

/// @notice Cross-artifact parity for the identity-targeted unilateral-A2 deposit
///         coupling: IdentityRegistry.verifyDepositCoupling accepts the canonical
///         proof emitted by alberta_buck.wallet.unilateral_a2
///         (scripts/gen_unilateral_a2_vectors.py -> test/vectors/unilateral_a2.json),
///         pinning the Solidity and Python Okamoto sigma + Fiat-Shamir encodings
///         byte-for-byte, and exercising the soundness rejections.  The full flow
///         (mint, off-chain delivery, unilateral receipt naming both identities,
///         collusion-resistance) is demonstrated in
///         alberta_buck/test/test_unilateral_a2.py; this suite pins the one piece
///         that lands on the EVM -- the deposit-eligibility gate.  See
///         alberta-buck-notes-unilateral.org.
contract UnilateralA2DepositTest is Test {
    IdentityRegistry internal reg;
    address internal constant GOV = address(0xA0);
    string  internal vj;
    address internal depositor;

    function setUp() public {
        vm.chainId(1);                          // wallet transcripts use chainid = 1
        vj  = vm.readFile("test/vectors/unilateral_a2.json");
        reg = new IdentityRegistry(GOV);

        // The depositor is *any* registered Fountain account bound to the
        // recipient's identity M_rec; verifyDepositCoupling reads its
        // (pk, E_addr) from storage.  The issuer (the note's source) is never
        // named on chain at deposit -- privacy from Mallory.
        depositor = address(uint160(_u(".depositor.addr")));
        vm.etch(depositor, hex"60006000fd");
        reg.bindContract(depositor, _g1(".depositor.pk"), _ct(".depositor.E"), false, false);
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
    function _eIss() internal view returns (IdentityRegistry.ElGamalCT memory) {
        return _ct(".eIss");
    }
    function _proof() internal view returns (IdentityRegistry.DepositCouplingProof memory p) {
        p.e   = _u(".deposit_coupling.e");
        p.s_m = _u(".deposit_coupling.s_m");
        p.s_s = _u(".deposit_coupling.s_s");
        p.s_b = _u(".deposit_coupling.s_b");
        p.A2  = _g1(".deposit_coupling.A2");
        p.A3  = _g1(".deposit_coupling.A3");
        p.A4  = _g1(".deposit_coupling.A4");
        p.P_I = _g1(".deposit_coupling.P_I");
    }

    // ---- completeness -------------------------------------------------------

    function test_vector_validDepositCoupling_verifies() public {
        assertTrue(reg.verifyDepositCoupling(depositor, _eIss(), _proof()),
                   "python-reference deposit coupling must verify on-chain");
    }

    // ---- soundness ----------------------------------------------------------

    function test_vector_tamperedResponse_rejected() public {
        // Perturbing s_m breaks E2/E3.
        IdentityRegistry.DepositCouplingProof memory p = _proof();
        p.s_m = addmod(p.s_m, 1, BN254.R);
        assertFalse(reg.verifyDepositCoupling(depositor, _eIss(), p));
    }

    function test_vector_tamperedPI_rejected() public {
        // Perturbing the committed issuer identity P_I breaks E3 -- the
        // collusion case where eIss is keyed to the wrong point.
        IdentityRegistry.DepositCouplingProof memory p = _proof();
        p.P_I = BN254.add(p.P_I, BN254.g1());
        assertFalse(reg.verifyDepositCoupling(depositor, _eIss(), p));
    }

    function test_vector_tamperedEIss_rejected() public {
        // A different leaf ciphertext no longer decrypts under m_rec to P_I.
        IdentityRegistry.ElGamalCT memory bad = _eIss();
        bad.C = BN254.add(bad.C, BN254.g1());
        assertFalse(reg.verifyDepositCoupling(depositor, bad, _proof()));
    }

    function test_vector_wrongChainid_rejected() public {
        vm.chainId(2);                               // FS rebinds chainid
        assertFalse(reg.verifyDepositCoupling(depositor, _eIss(), _proof()));
    }

    function test_vector_unregisteredDepositor_rejected() public {
        address other = address(uint160(0xdead));
        assertFalse(reg.verifyDepositCoupling(other, _eIss(), _proof()));
    }

    function test_vector_wrongDepositor_rejected() public {
        // A proof bound to this depositor must not verify for a different
        // registered account (its (pk, E_addr) differ).
        address other = address(uint160(0xC0FFEE));
        vm.etch(other, hex"60006000fd");
        // register `other` with a *different* key/credential (reuse issuer's record).
        reg.bindContract(other, _g1(".issuer.pk"), _ct(".issuer.E_reg"), false, false);
        assertFalse(reg.verifyDepositCoupling(other, _eIss(), _proof()));
    }
}
