// SPDX-License-Identifier: GPL-3.0-or-later
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {stdJson} from "forge-std/StdJson.sol";
import {BN254} from "../src/BN254.sol";

/// @title BN254.t.sol — sanity + parity checks against the Python wallet vectors.
/// @notice Loads test/vectors/identity.json (emitted by alberta_buck.wallet.cli)
///         and confirms every primitive matches the wallet's outputs.
contract BN254Test is Test {
    using stdJson for string;

    string internal vectorsJson;

    function setUp() public {
        vectorsJson = vm.readFile("test/vectors/identity.json");
    }

    // ---- helpers -----------------------------------------------------------

    function _g1At(string memory key) internal view returns (BN254.G1Point memory) {
        return BN254.G1Point({
            X: vm.parseJsonUint(vectorsJson, string.concat(key, ".x")),
            Y: vm.parseJsonUint(vectorsJson, string.concat(key, ".y"))
        });
    }

    function _scalarAt(string memory key) internal view returns (uint256) {
        return vm.parseJsonUint(vectorsJson, key);
    }

    // ---- primitive correctness --------------------------------------------

    function test_constants_match_python() public pure {
        // P and R from py_ecc.bn128 — same hex literals the Python encodes.
        assertEq(
            BN254.P,
            21888242871839275222246405745257275088696311157297823662689037894645226208583
        );
        assertEq(
            BN254.R,
            21888242871839275222246405745257275088548364400416034343698204186575808495617
        );
    }

    function test_g1_generator_doubles_correctly() public view {
        BN254.G1Point memory G = BN254.g1();
        BN254.G1Point memory G2 = BN254.add(G, G);
        BN254.G1Point memory G2viaMul = BN254.mul(G, 2);
        assertTrue(BN254.eq(G2, G2viaMul), "2G via add != 2G via mul");
    }

    function test_g1_neg_roundtrip() public view {
        BN254.G1Point memory G = BN254.g1();
        BN254.G1Point memory zero = BN254.add(G, BN254.neg(G));
        assertTrue(BN254.isInfinity(zero), "G + (-G) != O");
    }

    function test_g1_scalar_mul_distributive() public view {
        BN254.G1Point memory G = BN254.g1();
        BN254.G1Point memory left  = BN254.add(BN254.mul(G, 7), BN254.mul(G, 5));
        BN254.G1Point memory right = BN254.mul(G, 12);
        assertTrue(BN254.eq(left, right), "7G + 5G != 12G");
    }

    // ---- pairing parity: PS verification using emitted Alice signature ------

    function test_pairing_verifies_alice_ps_signature() public view {
        // The RAW credential (wallet-private) still satisfies the PS relation;
        // the published presentation does not (see IdentityRegistry.t.sol).
        BN254.G1Point memory s1 = _g1At(".alice.ps_sig_raw.sigma_1");
        BN254.G1Point memory s2 = _g1At(".alice.ps_sig_raw.sigma_2");
        uint256 m = _scalarAt(".alice.m");

        // pk_X, pk_Y are G2 points stored as { x:[c0,c1], y:[c0,c1] } -- read each coord.
        BN254.G2Point memory X = BN254.G2Point({
            X: [
                vm.parseJsonUint(vectorsJson, ".issuer.pk_X.x[0]"),
                vm.parseJsonUint(vectorsJson, ".issuer.pk_X.x[1]")
            ],
            Y: [
                vm.parseJsonUint(vectorsJson, ".issuer.pk_X.y[0]"),
                vm.parseJsonUint(vectorsJson, ".issuer.pk_X.y[1]")
            ]
        });
        BN254.G2Point memory Y = BN254.G2Point({
            X: [
                vm.parseJsonUint(vectorsJson, ".issuer.pk_Y.x[0]"),
                vm.parseJsonUint(vectorsJson, ".issuer.pk_Y.x[1]")
            ],
            Y: [
                vm.parseJsonUint(vectorsJson, ".issuer.pk_Y.y[0]"),
                vm.parseJsonUint(vectorsJson, ".issuer.pk_Y.y[1]")
            ]
        });

        // PS check: e(sigma_1, X + m*Y) == e(sigma_2, g_2)
        // Restated as a pairing product = 1: e(sigma_1, X + m*Y) * e(-sigma_2, g_2) == 1
        // We can't add G2 points without precompile help, so we use the linearised
        // equivalent on the G1 side that the registration NIZK uses:
        //   e(m*sigma_1, Y) * e(sigma_1, X) * e(-sigma_2, g_2) == 1
        BN254.G1Point[] memory a = new BN254.G1Point[](3);
        BN254.G2Point[] memory b = new BN254.G2Point[](3);
        a[0] = BN254.mul(s1, m);
        b[0] = Y;
        a[1] = s1;
        b[1] = X;
        a[2] = BN254.neg(s2);
        b[2] = BN254.g2();
        assertTrue(BN254.pairingCheck(a, b), "PS pairing product != 1");
    }

    // ---- Fiat-Shamir parity ------------------------------------------------

    function test_fsChallenge_matches_alice_registration_e() public view {
        BN254.G1Point[] memory pts = new BN254.G1Point[](9);
        pts[0] = _g1At(".alice.ps_presentation.A");
        pts[1] = _g1At(".alice.ps_presentation.B");
        pts[2] = _g1At(".alice.ciphertext.R");
        pts[3] = _g1At(".alice.ciphertext.C");
        pts[4] = _g1At(".alice.elgamal_kp.pk");
        pts[5] = _g1At(".alice.registration_proof.C1");
        pts[6] = _g1At(".alice.registration_proof.T_C");
        pts[7] = _g1At(".alice.registration_proof.T_R");
        pts[8] = _g1At(".alice.registration_proof.T_key");

        uint256[] memory scl = new uint256[](4);
        scl[0] = _scalarAt(".alice.registrant");
        scl[1] = _scalarAt(".chainid");
        scl[2] = _scalarAt(".registry");
        scl[3] = uint256(keccak256(
            "AlbertaBuck/FiatShamir/IdentityRegistry/Register/v2"
        ));

        uint256 e = BN254.fsChallenge(pts, scl);
        assertEq(e, _scalarAt(".alice.registration_proof.e"), "registration FS mismatch");
    }

    function test_fsChallenge_matches_approve_e() public view {
        BN254.G1Point[] memory pts = new BN254.G1Point[](9);
        pts[0] = _g1At(".approve.E_alice.R");
        pts[1] = _g1At(".approve.E_alice.C");
        pts[2] = _g1At(".approve.E_for_bob.R");
        pts[3] = _g1At(".approve.E_for_bob.C");
        pts[4] = _g1At(".alice.elgamal_kp.pk");
        pts[5] = _g1At(".bob.elgamal_kp.pk");
        pts[6] = _g1At(".approve.cp_proof.T1");
        pts[7] = _g1At(".approve.cp_proof.T2");
        pts[8] = _g1At(".approve.cp_proof.T3");

        uint256[] memory scl = new uint256[](5);
        scl[0] = _scalarAt(".approve.sender");
        scl[1] = _scalarAt(".approve.spender");
        scl[2] = _scalarAt(".approve.chainid");
        scl[3] = _scalarAt(".approve.registry");
        scl[4] = uint256(keccak256(
            "AlbertaBuck/FiatShamir/IdentityRegistry/Approve/v2"
        ));

        uint256 e = BN254.fsChallenge(pts, scl);
        assertEq(e, _scalarAt(".approve.cp_proof.e"), "approve FS mismatch");
    }
}
