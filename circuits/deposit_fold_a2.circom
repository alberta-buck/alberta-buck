// The folded deposit gate, A2 layout -- one witness, six relations.
//
// The A1 sibling (deposit_fold_a1.circom) carries the argument for folding;
// read its header first.  This file records only what A2 needs beyond it, and
// every difference traces to ONE fact: A2's ciphertext decrypts to the
// ISSUER's Identity, a point the spender holds no scalar for.
//
//   (1) k decrypts the note's ciphertext to the issuer Identity M_I
//   (2) the deposit account's registered credential decrypts, under its own
//       key, to the recipient Identity m_rec
//   (3) a registered leaf commits the pair (m_rec, k) under the recipient's
//       own salt -- the tie, and the relation a single-secret design got free
//   (4) that leaf's path folds to the posted identity root
//   (5) a registered leaf commits M_I under the ISSUER's salt, and its path
//       folds to the same root
//   (6) the mint binding's T, committed in idHash, opens as rm*G + gamma*H
//       for the rm = r*k_recv of the note tie -- the key tie
//
// WHY (5) EXISTS.  A colluding issuer could key the note to a throwaway point
// instead of the recipient's mailbox.  The anti-framing binding still passes,
// because it only forces the ciphertext over the issuer's own registered
// Identity -- so what rules the bogus note out is that the point the recipient
// decrypts must be a REGISTERED Identity.  A1 needs no such relation: its
// plaintext is the recipient's own Identity, which relation (3) already
// commits.
//
// WHY (6) EXISTS.  The mint binding proves eIss carries the minter's own
// registered Identity to the key hidden in its Q -- but an ElGamal ciphertext
// does not bind its plaintext to one key.  A minter can pick that key so the
// SAME eIss opens, under the recipient's k, to a registered sock puppet; (5)
// holds, and the recipient would name the puppet.  The binding also proves
// T = r*pk_Q + gamma*H, and idHash commits T, so (6) forces
// r*(pk_Q - k*G) = (gamma' - gamma)*H: re-aiming the key needs the log of the
// difference of two Identities to H_PEDERSEN, which no one has
// (doc/review/notes-receiving-key.org, section 4.6).  gamma arrives wrapped in
// the delivery.
//
// WHERE THE ISSUER'S SALT COMES FROM.  Relation (5) is proven by the
// RECIPIENT about the ISSUER, so the recipient needs the issuer's leaf
// preimage.  It cannot be the issuer's receiving leaf -- that would mean
// handing over the issuer's mailbox key.  It is the issuer's ordinary salted
// identity leaf, a SECOND association of the same Identity, and the issuer
// ships that salt in the note payload.  Distinct salts make the two
// associations unlinkable (accumulator specification, section 8.3), so
// disclosing the one that names the issuer reveals nothing about the one that
// reads its mail.  Only the salt is shipped: paths go stale as the subtree
// grows, and the recipient rebuilds the path from the published subtree.
//
// TWO CONSEQUENCES FOR THE ARITHMETIC, both from M_I being an arbitrary point.
//
//  * A2 cannot fold its point sums into scalar sums.  A1 writes
//    eEnc.C = (m_rec + t*k)*G because it knows m_rec; A2 can only write
//    M_I + (t*k)*G.  So this circuit performs elliptic-curve addition, which
//    A1 avoids by construction.  (6) adds a third, of two fixed-base
//    multiples on generators whose relative log no one knows.
//  * circom-lib offers no COMPLETE addition -- EllipticCurveAdd dispatches to
//    the incomplete EllipticCurveAddOptimised, which is finding 5's third
//    defect.  A1 escaped it; A2 must enforce its precondition instead.  Each
//    addition here asserts that the two x-coordinates differ, which excludes
//    both excluded cases at once, since doubling needs x1 == x2 and adding a
//    negation needs it too.  Comparing the coordinates reduced mod the native
//    field is SUFFICIENT: equal true coordinates would be congruent, so a
//    proven incongruence proves inequality.
//
// M_I's limbs are witnessed rather than produced by a gadget, so they are
// range-checked here explicitly with Num2Bits(64) -- finding 5's second
// defect, which A1 avoids because every limb array it carries feeds a
// ScalarMulG that checks them.

pragma circom 2.1.6;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "./ec/bn254_g_scalarmul.circom";
include "./ec/bn254_hp_scalarmul.circom";
include "./ec/get_bn254.circom";
include "../lib/circom-lib/circuits/ec/curve.circom";
include "./leaf_tags.circom";
include "./note_tags.circom";

template MerkleProofFoldA2(depth) {
    signal input  leaf;
    signal input  pathElements[depth];
    signal input  pathIndices[depth];
    signal output root;

    component switchers[depth];
    component hashers[depth];
    signal levels[depth + 1];
    levels[0] <== leaf;
    for (var i = 0; i < depth; i++) {
        pathIndices[i] * (pathIndices[i] - 1) === 0;
        switchers[i] = Switcher();
        switchers[i].sel <== pathIndices[i];
        switchers[i].L   <== levels[i];
        switchers[i].R   <== pathElements[i];
        hashers[i] = Poseidon(2);
        hashers[i].inputs[0] <== switchers[i].outL;
        hashers[i].inputs[1] <== switchers[i].outR;
        levels[i + 1] <== hashers[i].out;
    }
    root <== levels[depth];
}

template Recompose4A2() {
    signal input  limbs[4];
    signal output out;
    out <== limbs[0] + limbs[1] * (1 << 64)
          + limbs[2] * (1 << 128) + limbs[3] * (1 << 192);
}

// Assert x1 != x2 over the native field, so an incomplete addition of two
// points with those x-coordinates cannot land on a doubling or an identity.
template DistinctX() {
    signal input x1;
    signal input x2;
    signal diff;
    signal inv;
    diff <== x1 - x2;
    inv <-- 1 / diff;          // fails to satisfy the next line when diff == 0
    diff * inv === 1;
}

template DepositFoldA2(depth) {
    // ===== PUBLIC ============================================================
    signal input nullifier;
    signal input identityRoot;
    signal input eEncRx[4];  signal input eEncRy[4];
    signal input eEncCx[4];  signal input eEncCy[4];
    signal input pkDepX[4];  signal input pkDepY[4];
    signal input eDepRx[4];  signal input eDepRy[4];
    signal input eDepCx[4];  signal input eDepCy[4];

    // ===== PRIVATE ===========================================================
    signal input rho;
    signal input idHash;                    // Poseidon11(T_ID, eNote, eIss0, T)
    signal input eNote[4];                  // eNote coords (mod F_R)
    signal input eIss0[4];                  // the note's eIss coords (mod F_R)
    signal input T[2];                      // the mint binding's T (mod F_R)
    signal input r[4];                      // the issuer's mint randomness r'
    signal input k_recv[4];                 // the RECEIVING secret
    signal input rm[4];                     // r * k_recv        (witnessed)
    signal input t[4];                      // r' + s, the total randomness
    signal input tk[4];                     // t * k_recv        (witnessed)
    signal input m_rec[4];                  // the recipient Identity scalar
    signal input sk_dep[4];                 // the account key
    signal input r_E[4];                    // the account's registration nonce
    signal input cd[4];                     // m_rec + sk_dep*r_E (witnessed)
    signal input gamma[4];                  // T's blind (shipped, wrapped)
    signal input MI[2][4];                  // the decrypted issuer Identity
    signal input salt;                      // the recipient's leaf salt
    signal input saltIss;                   // the ISSUER's leaf salt (shipped)
    signal input pathElements[depth];
    signal input pathIndices[depth];
    signal input issPathElements[depth];
    signal input issPathIndices[depth];

    var A[4] = [0, 0, 0, 0];
    var B[4] = [3, 0, 0, 0];
    var P[4] = [BN254_MOD_Q0(), BN254_MOD_Q1(), BN254_MOD_Q2(), BN254_MOD_Q3()];
    signal dummy;
    dummy <== 0;
    dummy * dummy === 0;

    // ===== (0) the nullifier and the note's idHash ===========================
    component nf = Poseidon(3);
    nf.inputs[0] <== NOTE_TAG_NULLIFIER();
    nf.inputs[1] <== rho;
    nf.inputs[2] <== idHash;
    nullifier === nf.out;

    component idH = Poseidon(11);
    idH.inputs[0] <== NOTE_TAG_ID_HASH();
    idH.inputs[1] <== eNote[0];  idH.inputs[2] <== eNote[1];
    idH.inputs[3] <== eNote[2];  idH.inputs[4] <== eNote[3];
    idH.inputs[5] <== eIss0[0];  idH.inputs[6] <== eIss0[1];
    idH.inputs[7] <== eIss0[2];  idH.inputs[8] <== eIss0[3];
    idH.inputs[9] <== T[0];      idH.inputs[10] <== T[1];
    idHash === idH.out;

    // ===== M_I's limbs are witnessed, so range-check them ====================
    component miBits[2][4];
    for (var c = 0; c < 2; c++) {
        for (var i = 0; i < 4; i++) {
            miBits[c][i] = Num2Bits(64);
            miBits[c][i].in <== MI[c][i];
        }
    }
    component MIx = Recompose4A2();  MIx.limbs <== MI[0];
    component MIy = Recompose4A2();  MIy.limbs <== MI[1];

    // ===== native-field scalar arithmetic ====================================
    component rV   = Recompose4A2();  rV.limbs   <== r;
    component kV   = Recompose4A2();  kV.limbs   <== k_recv;
    component tV   = Recompose4A2();  tV.limbs   <== t;
    component mV   = Recompose4A2();  mV.limbs   <== m_rec;
    component skV  = Recompose4A2();  skV.limbs  <== sk_dep;
    component rEV  = Recompose4A2();  rEV.limbs  <== r_E;
    component rmV  = Recompose4A2();  rmV.limbs  <== rm;
    component tkV  = Recompose4A2();  tkV.limbs  <== tk;
    component cdV  = Recompose4A2();  cdV.limbs  <== cd;

    signal skr;  skr <== skV.out * rEV.out;      // sk_dep * r_E
    rmV.out === rV.out * kV.out;                 // rm = r * k
    tkV.out === tV.out * kV.out;                 // tk = t * k
    cdV.out === mV.out + skr;                    // cd = m_rec + sk_dep*r_E

    // ===== the seven fixed-base multiples of G (and one of H, in (6)) ========
    component rG  = ScalarMulG();  rG.b  <== r;        // eIss.R
    component rmG = ScalarMulG();  rmG.b <== rm;       // eIss.C = M_I + rm*G
    component tG  = ScalarMulG();  tG.b  <== t;        // eEnc.R = t*G
    component tkG = ScalarMulG();  tkG.b <== tk;       // eEnc.C = M_I + tk*G
    component skG = ScalarMulG();  skG.b <== sk_dep;   // pk_dep
    component rEG = ScalarMulG();  rEG.b <== r_E;      // E_dep.R
    component cdG = ScalarMulG();  cdG.b <== cd;       // E_dep.C

    // ===== the note tie: eIss.R = r*G, committed in idHash ===================
    component iss0x = Recompose4A2();  iss0x.limbs <== rG.out[0];
    component iss0y = Recompose4A2();  iss0y.limbs <== rG.out[1];
    iss0x.out === eIss0[0];
    iss0y.out === eIss0[1];

    // ===== (1a) eIss.C = M_I + rm*G, committed in idHash =====================
    component rmx = Recompose4A2();  rmx.limbs <== rmG.out[0];
    component dxC0 = DistinctX();
    dxC0.x1 <== MIx.out;
    dxC0.x2 <== rmx.out;

    component addC0 = EllipticCurveAddOptimised(64, 4, A, B, P);
    addC0.in1 <== MI;
    addC0.in2 <== rmG.out;
    addC0.dummy <== dummy;

    component c0x = Recompose4A2();  c0x.limbs <== addC0.out[0];
    component c0y = Recompose4A2();  c0y.limbs <== addC0.out[1];
    c0x.out === eIss0[2];
    c0y.out === eIss0[3];

    // ===== (6) the key tie: T = rm*G + gamma*H, committed in idHash ==========
    component gH = ScalarMulHP();  gH.b <== gamma;
    component ghx = Recompose4A2();  ghx.limbs <== gH.out[0];
    component dxT = DistinctX();
    dxT.x1 <== rmx.out;
    dxT.x2 <== ghx.out;

    component addT = EllipticCurveAddOptimised(64, 4, A, B, P);
    addT.in1 <== rmG.out;
    addT.in2 <== gH.out;
    addT.dummy <== dummy;

    component tx = Recompose4A2();  tx.limbs <== addT.out[0];
    component ty = Recompose4A2();  ty.limbs <== addT.out[1];
    tx.out === T[0];
    ty.out === T[1];

    // ===== (1b) k decrypts the SPEND's ciphertext to the same M_I ============
    for (var i = 0; i < 4; i++) {
        tG.out[0][i] === eEncRx[i];
        tG.out[1][i] === eEncRy[i];
    }

    component tkx = Recompose4A2();  tkx.limbs <== tkG.out[0];
    component dxEnc = DistinctX();
    dxEnc.x1 <== MIx.out;
    dxEnc.x2 <== tkx.out;

    component addEnc = EllipticCurveAddOptimised(64, 4, A, B, P);
    addEnc.in1 <== MI;
    addEnc.in2 <== tkG.out;
    addEnc.dummy <== dummy;

    for (var i = 0; i < 4; i++) {
        addEnc.out[0][i] === eEncCx[i];
        addEnc.out[1][i] === eEncCy[i];
    }

    // ===== (2) the account credential decrypts to m_rec ======================
    for (var i = 0; i < 4; i++) {
        skG.out[0][i] === pkDepX[i];
        skG.out[1][i] === pkDepY[i];
        rEG.out[0][i] === eDepRx[i];
        rEG.out[1][i] === eDepRy[i];
        cdG.out[0][i] === eDepCx[i];
        cdG.out[1][i] === eDepCy[i];
    }

    // ===== (3)+(4) the recipient's leaf commits (m_rec, k) ===================
    component leafH = Poseidon(4);
    leafH.inputs[0] <== LEAF_TAG_RECEIVING();
    leafH.inputs[1] <== mV.out;
    leafH.inputs[2] <== kV.out;
    leafH.inputs[3] <== salt;

    component mp = MerkleProofFoldA2(depth);
    mp.leaf <== leafH.out;
    for (var i = 0; i < depth; i++) {
        mp.pathElements[i] <== pathElements[i];
        mp.pathIndices[i]  <== pathIndices[i];
    }
    identityRoot === mp.root;

    // ===== (5) the ISSUER's leaf commits M_I, under the shipped salt =========
    // identity_leaf_salted(M_I, saltIss): coordinates, because this is an
    // ordinary registry association rather than a receiving one.
    component issLeaf = Poseidon(4);
    issLeaf.inputs[0] <== LEAF_TAG_IDENTITY_SALTED();
    issLeaf.inputs[1] <== MIx.out;
    issLeaf.inputs[2] <== MIy.out;
    issLeaf.inputs[3] <== saltIss;

    component issMp = MerkleProofFoldA2(depth);
    issMp.leaf <== issLeaf.out;
    for (var i = 0; i < depth; i++) {
        issMp.pathElements[i] <== issPathElements[i];
        issMp.pathIndices[i]  <== issPathIndices[i];
    }
    identityRoot === issMp.root;
}

// Both paths are 32 levels, subtree then aggregator; see deposit_fold_a1's
// trailer.  The recipient's and the issuer's leaves may sit in different
// registries' subtrees, and neither slot is revealed.
component main { public [
    nullifier, identityRoot,
    eEncRx, eEncRy, eEncCx, eEncCy,
    pkDepX, pkDepY,
    eDepRx, eDepRy, eDepCx, eDepCy
] } = DepositFoldA2(32);
