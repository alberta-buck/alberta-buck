// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import {IdentityRegistry} from "../src/IdentityRegistry.sol";
import {BN254} from "../src/BN254.sol";

/// @notice Cross-artifact parity for the A2 issuer re-encryption binding: the
///         on-chain verifyIssuerReenc accepts the canonical proof emitted by
///         alberta_buck.wallet.issuer_reenc (via emit-vectors ->
///         test/vectors/identity.json), pinning the Solidity and Python
///         Okamoto sigma + Fiat-Shamir encodings together byte-for-byte.  Also
///         exercises the soundness rejections (Notes mutual-decryptability,
///         Phase 2; see alberta-buck-notes-decryptability.org).
contract IssuerReencVectorTest is Test {
    IdentityRegistry internal reg;
    address internal constant GOV = address(0xA0);
    string  internal vj;
    address internal issuer;

    function setUp() public {
        vm.chainId(1);                          // wallet transcripts use chainid = 1
        vj  = vm.readFile("test/vectors/identity.json");
        reg = new IdentityRegistry(GOV);

        // The A2 issuer is a registered *private* Identity (isPublicIdentity =
        // false); verifyIssuerReenc reads its (pk, E_addr) from storage.
        issuer = address(uint160(_u(".issuer_reenc.issuer")));
        vm.etch(issuer, hex"60006000fd");
        reg.bindContract(issuer, _g1(".issuer_reenc.pk_iss"),
                         _ct(".issuer_reenc.E_reg"), false, false);
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
        return _ct(".issuer_reenc.E_iss");
    }
    function _proof() internal view returns (IdentityRegistry.IssuerReencProof memory p) {
        p.e   = _u(".issuer_reenc.proof.e");
        p.s_r = _u(".issuer_reenc.proof.s_r");
        p.s_b = _u(".issuer_reenc.proof.s_b");
        p.s_s = _u(".issuer_reenc.proof.s_s");
        p.s_g = _u(".issuer_reenc.proof.s_g");
        p.A1  = _g1(".issuer_reenc.proof.A1");
        p.A2  = _g1(".issuer_reenc.proof.A2");
        p.A3  = _g1(".issuer_reenc.proof.A3");
        p.A4  = _g1(".issuer_reenc.proof.A4");
        p.A5  = _g1(".issuer_reenc.proof.A5");
        p.Q   = _g1(".issuer_reenc.proof.Q");
        p.U   = _g1(".issuer_reenc.proof.U");
        p.T   = _g1(".issuer_reenc.proof.T");
    }

    // ---- completeness -------------------------------------------------------

    function test_vector_validProof_verifies() public {
        assertTrue(reg.verifyIssuerReenc(issuer, _eIss(), _proof()),
                   "python-reference A2 binding must verify on-chain");
    }

    // ---- soundness ----------------------------------------------------------

    function test_vector_tamperedResponse_rejected() public {
        IdentityRegistry.IssuerReencProof memory p = _proof();
        p.s_r = addmod(p.s_r, 1, BN254.R);           // breaks L1/L2/L3
        assertFalse(reg.verifyIssuerReenc(issuer, _eIss(), p));
    }

    function test_vector_tamperedT_rejected() public {
        // Perturbing the published T (= r'*pk_rec) breaks L3 and L5 -- the
        // colluding-issuer attack (a leaf the recipient cannot decrypt to M).
        IdentityRegistry.IssuerReencProof memory p = _proof();
        p.T = BN254.add(p.T, BN254.g1());
        assertFalse(reg.verifyIssuerReenc(issuer, _eIss(), p));
    }

    function test_vector_tamperedEIss_rejected() public {
        // A different leaf ciphertext no longer matches the bound issuer M.
        IdentityRegistry.ElGamalCT memory bad = _eIss();
        bad.C = BN254.add(bad.C, BN254.g1());
        assertFalse(reg.verifyIssuerReenc(issuer, bad, _proof()));
    }

    function test_vector_wrongChainid_rejected() public {
        vm.chainId(2);                               // FS rebinds chainid
        assertFalse(reg.verifyIssuerReenc(issuer, _eIss(), _proof()));
    }

    function test_vector_unregisteredIssuer_rejected() public {
        address other = address(uint160(0xdead));
        assertFalse(reg.verifyIssuerReenc(other, _eIss(), _proof()));
    }
}
