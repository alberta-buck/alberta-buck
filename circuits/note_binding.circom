// Note<->eEnc re-encryption tie — INoteBindingVerifier circuit.
//
// Proves that the deposit-coupling ciphertext eEnc supplied at spend is a
// re-encryption, under the recipient Identity point M_rec, of the
// issuer/recipient ciphertext that the spent note committed in its idHash.
//
// Public:  nullifier, eEncRx[4], eEncRy[4], eEncCx[4], eEncCy[4],
//          piX[4], piY[4]                                          (25 signals)
// Private: rho, idHash, eNote[4], eIss0[4], s[4], m_rec[4],
//          sm[4], r[4], rm[4], b[4], MI[2][4]                      (38 signals)
//
// DESIGN — ElGamal structure exploitation.
//
// The committed ciphertext eIssCommitted = (R0, C0) is an ElGamal encryption:
//   R0 = r*G,  C0 = M_I + r*M_rec
// where M_I is the issuer identity and r is the encryption randomness.
// Decryption recovers M_I = C0 - m_rec*R0.
//
// Instead of computing m_rec*R0 (variable-base, ~5M constraints), we witness
// the decrypted M_I and the randomness r, and verify the ElGamal structure
// using only fixed-base scalar multiplications (r*G and (r·m_rec)*G).
// This reduces the constraint count from ~6.3M to ~2.8M.
//
// Constraints:
//   (1) nullifier = Poseidon3(rho, idHash, 4242)
//   (2) idHash     = Poseidon8(eNote, eIss0)
//   (3a) eEnc.R = R0 + s*G       (re-encryption of R)
//   (3b) eEnc.C = C0 + sm*G      (re-encryption of C, sm = s·m_rec)
//   (4a) R0 = r*G                 (ElGamal structure — randomness commitment)
//   (4b) C0 = M_I + rm*G          (ElGamal structure — rm = r·m_rec)
//   (4c) P_I = M_I + b*H          (committed/blinded identity)
//
// The scalar products sm = s·m_rec and rm = r·m_rec are witnessed in 4-limb
// form; the circuit constrains them against the native-field products.
//
// CIRCUIT DESIGN NOTE — circomlib vs circom-lib.  This circuit includes
// Poseidon + Switcher from circomlib, and EC primitives from circom-lib (via
// bn254_h_scalarmul, get_bn254, curve).  It does NOT include circomlib's
// bitify/comparators templates to avoid conflicts with circom-lib.

pragma circom 2.1.6;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "./ec/bn254_h_scalarmul.circom";
include "./ec/bn254_g_scalarmul.circom";
include "./ec/get_bn254.circom";
include "../lib/circom-lib/circuits/ec/curve.circom";


template NoteBinding() {
    // ===== PUBLIC INPUTS (25 signals) ========================================
    signal input nullifier;
    signal input eEncRx[4];  signal input eEncRy[4];
    signal input eEncCx[4];  signal input eEncCy[4];
    signal input piX[4];     signal input piY[4];

    // ===== PRIVATE WITNESSES =================================================
    signal input rho;                           // note randomness (mod F_R)
    signal input idHash;                        // Poseidon8(eNote, eIss0)
    signal input eNote[4];                      // eNote coords (mod F_R)
    signal input eIss0[4];                      // eIssCommitted coords (mod F_R)
    signal input s[4];                          // re-rand scalar (4 limbs)
    signal input m_rec[4];                      // identity scalar (4 limbs)
    signal input sm[4];                         // s * m_rec (4 limbs, witnessed)
    signal input r[4];                          // ElGamal randomness (4 limbs)
    signal input rm[4];                         // r * m_rec (4 limbs, witnessed)
    signal input b[4];                          // P_I blind (4 limbs)
    signal input MI[2][4];                      // decrypted identity M_I (4-limb)

    signal dummy;
    dummy <== 0;
    dummy * dummy === 0;

    var A[4] = [BN254_A0(), BN254_A1(), BN254_A2(), BN254_A3()];
    var B[4] = [BN254_B0(), BN254_B1(), BN254_B2(), BN254_B3()];
    var P[4] = [BN254_MOD_Q0(), BN254_MOD_Q1(), BN254_MOD_Q2(), BN254_MOD_Q3()];

    // ===== (1) nullifier = Poseidon3(rho, idHash, 4242) ======================
    component nf = Poseidon(3);
    nf.inputs[0] <== rho;
    nf.inputs[1] <== idHash;
    nf.inputs[2] <== 4242;
    nullifier === nf.out;

    // ===== (2) idHash = Poseidon8(eNote, eIss0) ==============================
    component idH = Poseidon(8);
    idH.inputs[0] <== eNote[0];  idH.inputs[1] <== eNote[1];
    idH.inputs[2] <== eNote[2];  idH.inputs[3] <== eNote[3];
    idH.inputs[4] <== eIss0[0];  idH.inputs[5] <== eIss0[1];
    idH.inputs[6] <== eIss0[2];  idH.inputs[7] <== eIss0[3];
    idHash === idH.out;

    // ---- Witnessed scalar products: constrain sm = s * m_rec, rm = r * m_rec
    signal s_val;
    s_val <== s[0] + s[1] * (1 << 64) + s[2] * (1 << 128) + s[3] * (1 << 192);
    signal r_val;
    r_val <== r[0] + r[1] * (1 << 64) + r[2] * (1 << 128) + r[3] * (1 << 192);
    signal m_rec_val;
    m_rec_val <== m_rec[0] + m_rec[1] * (1 << 64)
               + m_rec[2] * (1 << 128) + m_rec[3] * (1 << 192);

    signal sm_val;
    sm_val <== s_val * m_rec_val;
    signal sm_check;
    sm_check <== sm[0] + sm[1] * (1 << 64)
              + sm[2] * (1 << 128) + sm[3] * (1 << 192);
    sm_check === sm_val;

    signal rm_val;
    rm_val <== r_val * m_rec_val;
    signal rm_check;
    rm_check <== rm[0] + rm[1] * (1 << 64)
              + rm[2] * (1 << 128) + rm[3] * (1 << 192);
    rm_check === rm_val;

    // eIss0[4] carries (R0x, R0y, C0x, C0y) reduced mod F_R as Poseidon inputs.
    // We RECONSTRUCT R0 and C0 in 4-limb form from eIss0 (since the circuit
    // already constrains their relationship via R0_single===eIss0, etc.).
    // To avoid the prover having to supply R0/C0 as separate witnesses, we
    // derive them from eIss0's mod-F_R values.  The prover supplies R0_limb
    // as the FULL 4-limb values (which may exceed F_R), cross-constrained below.

    signal input R0_limb[2][4];                 // eIssCommitted.R (4-limb, full)
    signal input C0_limb[2][4];                 // eIssCommitted.C (4-limb, full)

    // Cross-constrain R0_limb / C0_limb (4-limb) ↔ eIss0 (mod-F_R).
    signal R0x_single;
    R0x_single <== R0_limb[0][0] + R0_limb[0][1] * (1 << 64)
                + R0_limb[0][2] * (1 << 128) + R0_limb[0][3] * (1 << 192);
    R0x_single === eIss0[0];

    signal R0y_single;
    R0y_single <== R0_limb[1][0] + R0_limb[1][1] * (1 << 64)
                + R0_limb[1][2] * (1 << 128) + R0_limb[1][3] * (1 << 192);
    R0y_single === eIss0[1];

    signal C0x_single;
    C0x_single <== C0_limb[0][0] + C0_limb[0][1] * (1 << 64)
                + C0_limb[0][2] * (1 << 128) + C0_limb[0][3] * (1 << 192);
    C0x_single === eIss0[2];

    signal C0y_single;
    C0y_single <== C0_limb[1][0] + C0_limb[1][1] * (1 << 64)
                + C0_limb[1][2] * (1 << 128) + C0_limb[1][3] * (1 << 192);
    C0y_single === eIss0[3];

    // ===== (3a) sG = s * G ===================================================
    component sG = ScalarMulG();
    sG.b <== s;

    // eEnc.R = R0_limb + sG
    component addR = EllipticCurveAddOptimised(64, 4, A, B, P);
    addR.in1 <== R0_limb;
    addR.in2 <== sG.out;
    addR.dummy <== dummy;

    addR.out[0][0] === eEncRx[0]; addR.out[0][1] === eEncRx[1];
    addR.out[0][2] === eEncRx[2]; addR.out[0][3] === eEncRx[3];
    addR.out[1][0] === eEncRy[0]; addR.out[1][1] === eEncRy[1];
    addR.out[1][2] === eEncRy[2]; addR.out[1][3] === eEncRy[3];

    // ===== (3b) smG = sm * G =================================================
    component smG = ScalarMulG();
    smG.b <== sm;

    // eEnc.C = C0_limb + smG
    component addC = EllipticCurveAddOptimised(64, 4, A, B, P);
    addC.in1 <== C0_limb;
    addC.in2 <== smG.out;
    addC.dummy <== dummy;

    addC.out[0][0] === eEncCx[0]; addC.out[0][1] === eEncCx[1];
    addC.out[0][2] === eEncCx[2]; addC.out[0][3] === eEncCx[3];
    addC.out[1][0] === eEncCy[0]; addC.out[1][1] === eEncCy[1];
    addC.out[1][2] === eEncCy[2]; addC.out[1][3] === eEncCy[3];

    // ===== (4a) R0_limb = r*G  (ElGamal structure) ===========================
    component rG = ScalarMulG();
    rG.b <== r;

    rG.out[0][0] === R0_limb[0][0]; rG.out[0][1] === R0_limb[0][1];
    rG.out[0][2] === R0_limb[0][2]; rG.out[0][3] === R0_limb[0][3];
    rG.out[1][0] === R0_limb[1][0]; rG.out[1][1] === R0_limb[1][1];
    rG.out[1][2] === R0_limb[1][2]; rG.out[1][3] === R0_limb[1][3];

    // ===== (4b) C0_limb = M_I + rm*G  (ElGamal structure) ====================
    component rmG = ScalarMulG();
    rmG.b <== rm;

    component add4b = EllipticCurveAddOptimised(64, 4, A, B, P);
    add4b.in1 <== MI;
    add4b.in2 <== rmG.out;
    add4b.dummy <== dummy;

    add4b.out[0][0] === C0_limb[0][0]; add4b.out[0][1] === C0_limb[0][1];
    add4b.out[0][2] === C0_limb[0][2]; add4b.out[0][3] === C0_limb[0][3];
    add4b.out[1][0] === C0_limb[1][0]; add4b.out[1][1] === C0_limb[1][1];
    add4b.out[1][2] === C0_limb[1][2]; add4b.out[1][3] === C0_limb[1][3];

    // ===== (4c) P_I = M_I + b*H ==============================================
    component bH = ScalarMulH();
    bH.b <== b;

    component add4c = EllipticCurveAddOptimised(64, 4, A, B, P);
    add4c.in1 <== MI;
    add4c.in2 <== bH.out;
    add4c.dummy <== dummy;

    add4c.out[0][0] === piX[0]; add4c.out[0][1] === piX[1];
    add4c.out[0][2] === piX[2]; add4c.out[0][3] === piX[3];
    add4c.out[1][0] === piY[0]; add4c.out[1][1] === piY[1];
    add4c.out[1][2] === piY[2]; add4c.out[1][3] === piY[3];
}

component main { public [
    nullifier,
    eEncRx, eEncRy, eEncCx, eEncCy,
    piX, piY
] } = NoteBinding();
