// Note<->eEnc addressed tie, A1 payload layout — INoteBindingVerifier
// (verifyNoteBindingA1) circuit.
//
// Proves that the deposit-coupling ciphertext eEnc supplied at an A1 spend
// is keyed to the SAME recipient Identity M_rec the spent note's eNote was
// encrypted under — the A1 counterpart of note_binding.circom (whose
// idHash opens the A2 layout Poseidon8(eNote, eIss)).
//
// Public:  nullifier, v, eEncRx[4], eEncRy[4], eEncCx[4], eEncCy[4],
//          piX[4], piY[4]                                       (26 signals)
// Private: rho, idHash, eNote[4], mIss, sigR[2], sigS,
//          rn[4], m_rec[4], u[4], t[4], tm[4], b[4]             (27 signals)
//
// DESIGN — everything is a known multiple of G.
//
// An A1 note commits, in its idHash, the value ciphertext
//   eNote = (rn*G, v*G + rn*M_rec)
// alongside the PUBLIC issuer's identity material (m_issuer, sigma).  The
// spend-side coupling ciphertext is a re-randomized encryption of the
// recipient identity under itself,
//   eEnc = (t*G, M_rec + t*M_rec)        (t = r' + s, the total randomness)
// and the coupling sigma's committed point is P_I = M_rec + b*H.  With
// M_rec = m_rec*G every curve point above is a *fixed-base* multiple of G,
// so the circuit needs no variable-base scalar multiplication and no
// witnessed point limbs: it recomputes each point from witnessed scalars
// and equates coordinates (mod F_R against the Poseidon words for eNote;
// full 4x64-limb equality against the public eEnc / P_I).
//
// WHY v IS PUBLIC.  ElGamal ciphertexts are not key-committing: a prover
// knowing rn (the note randomness travels with the opening) could open
// eNote.C = V + rn*m'*G for ANY m' by absorbing the difference into a free
// plaintext V.  Pinning the plaintext to v*G with v a PUBLIC input — which
// Notes.spendCoupledA1 sets to the spend's `face`, itself bound to the
// note's committed value by the spend SNARK over the same nullifier —
// makes m_rec = (dlog(eNote.C) - v)/rn unique: only the identity the note
// was addressed to can satisfy the relation.
//
// Constraints:
//   (1) nullifier = Poseidon3(rho, idHash, 4242)
//   (2) idHash    = Poseidon8(eNote, mIss, sigR, sigS)   (the A1 layout)
//   (3) eNote.R = rn*G                  (mod-F_R words match eNote[0..1])
//   (4) eNote.C = u*G,  u = v + rn*m_rec   (words match eNote[2..3])
//   (5) eEnc.R  = t*G
//   (6) eEnc.C  = m_rec*G + tm*G,  tm = t*m_rec
//   (7) P_I     = m_rec*G + b*H
//
// (1) reuses the nullifier the spend SNARK already attests, tying the proof
// to the SPECIFIC spent note without revealing idHash; (6)+(7) share m_rec /
// P_I with IdentityRegistry.verifyDepositCoupling, so the coupling and the
// tie cannot be answered with different identities.  The scalar products
// u = v + rn*m_rec and tm = t*m_rec are witnessed in 4-limb form and
// constrained against the native-field products (sound because BN254's G1
// group order equals the circuit's native field).
//
// CIRCUIT DESIGN NOTE — circomlib vs circom-lib.  As note_binding.circom:
// Poseidon + Switcher from circomlib, EC primitives from circom-lib (via
// bn254_h_scalarmul, get_bn254, curve); circomlib's bitify/comparators are
// NOT included to avoid conflicts with circom-lib.

pragma circom 2.1.6;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "./ec/bn254_h_scalarmul.circom";
include "./ec/bn254_g_scalarmul.circom";
include "./ec/get_bn254.circom";
include "../lib/circom-lib/circuits/ec/curve.circom";


template NoteBindingA1() {
    // ===== PUBLIC INPUTS (26 signals) ========================================
    signal input nullifier;
    signal input v;                             // note face (= spend's public face)
    signal input eEncRx[4];  signal input eEncRy[4];
    signal input eEncCx[4];  signal input eEncCy[4];
    signal input piX[4];     signal input piY[4];

    // ===== PRIVATE WITNESSES =================================================
    signal input rho;                           // note randomness (mod F_R)
    signal input idHash;                        // Poseidon8(eNote, mIss, sigR, sigS)
    signal input eNote[4];                      // eNote coords (mod F_R)
    signal input mIss;                          // public issuer identity scalar word
    signal input sigR[2];                       // issuer Schnorr nonce coords (mod F_R)
    signal input sigS;                          // issuer Schnorr response word
    signal input rn[4];                         // eNote randomness (4 limbs)
    signal input m_rec[4];                      // recipient identity scalar (4 limbs)
    signal input u[4];                          // v + rn * m_rec (4 limbs, witnessed)
    signal input t[4];                          // eEnc total randomness (4 limbs)
    signal input tm[4];                         // t * m_rec (4 limbs, witnessed)
    signal input b[4];                          // P_I blind (4 limbs)

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

    // ===== (2) idHash = Poseidon8(eNote, mIss, sigR, sigS) — A1 layout =======
    component idH = Poseidon(8);
    idH.inputs[0] <== eNote[0];  idH.inputs[1] <== eNote[1];
    idH.inputs[2] <== eNote[2];  idH.inputs[3] <== eNote[3];
    idH.inputs[4] <== mIss;
    idH.inputs[5] <== sigR[0];   idH.inputs[6] <== sigR[1];
    idH.inputs[7] <== sigS;
    idHash === idH.out;

    // ---- Witnessed scalar products: u = v + rn*m_rec, tm = t*m_rec ---------
    signal rn_val;
    rn_val <== rn[0] + rn[1] * (1 << 64) + rn[2] * (1 << 128) + rn[3] * (1 << 192);
    signal m_rec_val;
    m_rec_val <== m_rec[0] + m_rec[1] * (1 << 64)
               + m_rec[2] * (1 << 128) + m_rec[3] * (1 << 192);
    signal t_val;
    t_val <== t[0] + t[1] * (1 << 64) + t[2] * (1 << 128) + t[3] * (1 << 192);

    signal rnm_val;
    rnm_val <== rn_val * m_rec_val;
    signal u_check;
    u_check <== u[0] + u[1] * (1 << 64) + u[2] * (1 << 128) + u[3] * (1 << 192);
    u_check === v + rnm_val;

    signal tm_check;
    tm_check <== tm[0] + tm[1] * (1 << 64) + tm[2] * (1 << 128) + tm[3] * (1 << 192);
    tm_check === t_val * m_rec_val;

    // ===== (3) eNote.R = rn*G ================================================
    // The scalar-mul output is the canonical 4-limb point; its mod-F_R
    // recomposition must equal the Poseidon words committed in idHash.
    component rnG = ScalarMulG();
    rnG.b <== rn;

    signal eNoteRx_single;
    eNoteRx_single <== rnG.out[0][0] + rnG.out[0][1] * (1 << 64)
                    + rnG.out[0][2] * (1 << 128) + rnG.out[0][3] * (1 << 192);
    eNoteRx_single === eNote[0];

    signal eNoteRy_single;
    eNoteRy_single <== rnG.out[1][0] + rnG.out[1][1] * (1 << 64)
                    + rnG.out[1][2] * (1 << 128) + rnG.out[1][3] * (1 << 192);
    eNoteRy_single === eNote[1];

    // ===== (4) eNote.C = u*G  (u = v + rn*m_rec, constrained above) ==========
    component uG = ScalarMulG();
    uG.b <== u;

    signal eNoteCx_single;
    eNoteCx_single <== uG.out[0][0] + uG.out[0][1] * (1 << 64)
                    + uG.out[0][2] * (1 << 128) + uG.out[0][3] * (1 << 192);
    eNoteCx_single === eNote[2];

    signal eNoteCy_single;
    eNoteCy_single <== uG.out[1][0] + uG.out[1][1] * (1 << 64)
                    + uG.out[1][2] * (1 << 128) + uG.out[1][3] * (1 << 192);
    eNoteCy_single === eNote[3];

    // ===== (5) eEnc.R = t*G ==================================================
    component tG = ScalarMulG();
    tG.b <== t;

    tG.out[0][0] === eEncRx[0]; tG.out[0][1] === eEncRx[1];
    tG.out[0][2] === eEncRx[2]; tG.out[0][3] === eEncRx[3];
    tG.out[1][0] === eEncRy[0]; tG.out[1][1] === eEncRy[1];
    tG.out[1][2] === eEncRy[2]; tG.out[1][3] === eEncRy[3];

    // ===== (6) eEnc.C = m_rec*G + tm*G  (tm = t*m_rec, constrained above) ====
    component mG = ScalarMulG();
    mG.b <== m_rec;

    component tmG = ScalarMulG();
    tmG.b <== tm;

    component addC = EllipticCurveAddOptimised(64, 4, A, B, P);
    addC.in1 <== mG.out;
    addC.in2 <== tmG.out;
    addC.dummy <== dummy;

    addC.out[0][0] === eEncCx[0]; addC.out[0][1] === eEncCx[1];
    addC.out[0][2] === eEncCx[2]; addC.out[0][3] === eEncCx[3];
    addC.out[1][0] === eEncCy[0]; addC.out[1][1] === eEncCy[1];
    addC.out[1][2] === eEncCy[2]; addC.out[1][3] === eEncCy[3];

    // ===== (7) P_I = m_rec*G + b*H ===========================================
    component bH = ScalarMulH();
    bH.b <== b;

    component addP = EllipticCurveAddOptimised(64, 4, A, B, P);
    addP.in1 <== mG.out;
    addP.in2 <== bH.out;
    addP.dummy <== dummy;

    addP.out[0][0] === piX[0]; addP.out[0][1] === piX[1];
    addP.out[0][2] === piX[2]; addP.out[0][3] === piX[3];
    addP.out[1][0] === piY[0]; addP.out[1][1] === piY[1];
    addP.out[1][2] === piY[2]; addP.out[1][3] === piY[3];
}

component main { public [
    nullifier, v,
    eEncRx, eEncRy, eEncCx, eEncCy,
    piX, piY
] } = NoteBindingA1();
