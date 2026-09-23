// The folded deposit gate, A1 layout — one witness, one circuit, four relations.
//
// Reading a note and being an Identity are facts about two DIFFERENT secrets:
// an addressed note is keyed to the recipient's receiving key pk_recv = k*G,
// while authority belongs to the Identity M_rec = m_rec*G, because an identity
// scalar is a read capability the design discloses to every counterparty and so
// cannot also be a decryption key (doc/review/notes-receiving-key.org).
//
// A gate that proved those two facts SIDE BY SIDE would state nothing about
// their owner.  A thief holding a stolen payload -- and so the k inside it --
// supplies the reading half with the stolen key and the Identity half with its
// OWN registered Identity.  Both halves are true.  Neither joins them, and the
// note becomes spendable by the wrong person
// (scripts/review/deposit_gate_split.py demonstrates it as a passing test).
//
// This is review finding 5 in a second place: an equality inferred from two
// proofs that merely share a public point.  The remedy is the same one -- state
// the tie instead of assuming it -- so the four relations share one witness:
//
//   (1) k decrypts the note's ciphertext to the point the gate commits
//   (2) the deposit account's registered credential decrypts, under its own
//       key, to the Identity M_rec
//   (3) a registered accumulator leaf commits the pair (m_rec, k) under the
//       holder's own salt
//   (4) that leaf's path folds to the posted identity root
//
// Relation (3) is the step a single-secret design got for free, and it is the
// one the thief cannot satisfy: no registered leaf pairs ITS Identity with the
// key it stole, and it cannot compute the victim's leaf without the victim's
// salt, which is derived from a wallet secret and never from the identity.
//
// THE THREE FINDING-5 DEFECTS ARE ABSENT BY CONSTRUCTION, NOT BY PATCH.
//
//  * No free tie point.  The old split needed P_I = M + b*H so that a sigma
//    and a membership SNARK could share a hidden point, and the blind T = b*H
//    was WITNESSED rather than proven -- so a prover could choose any T and
//    satisfy P_I = M + T for any M.  Folding removes the reason for P_I at
//    all: there is no second proof to share a point with, so there is no
//    point, no blind, and no ScalarMulH.
//  * No unchecked limbs.  Every witnessed 4-limb scalar here is consumed by
//    ScalarMulG, which range-checks each limb with Num2Bits(64).  The
//    intermediate products (t*k, sk*r_E) are native signals rather than
//    witnessed limbs, so there is nothing left to alias.
//  * No incomplete addition.  Sums of points are folded into sums of SCALARS
//    before multiplication -- eEnc.C = (m_rec + t*k)*G rather than
//    m_rec*G + (t*k)*G -- so the circuit performs no elliptic-curve addition
//    and cannot be driven into a doubling or identity case.
//
// THE LEAF COMMITS THE SCALARS, NOT THE POINTS, AND THAT IS WHY THIS FITS.
//
// Committing (M_rec, pk_recv) as coordinates would force two more fixed-base
// multiplications -- 943,792 constraints, 22% of the circuit -- purely to
// re-derive points whose preimages the prover already holds.  With the leaf
// over (m_rec, k) the circuit hashes the very private signals its other
// relations use, which is one Poseidon and a tighter tie: the Identity in the
// credential relation and the Identity in the leaf are the SAME signal, not
// two values connected by a derivation whose output limbs the scalar-mul
// gadget does not range-check.  BN254's G1 group order equals this circuit's
// native field, so a scalar is a field element outright -- no limbs, no
// reduction, no aliasing question.  See registry/tree.py::receiving_leaf.
//
// Soundness of the native-field products: BN254's G1 group order equals this
// circuit's native field, so arithmetic on scalars in the native field IS
// scalar arithmetic.  (The same argument note_binding_a1.circom already makes.)
//
// A1 layout.  eRec's plaintext is the recipient Identity, so the point the gate
// commits in relation (1) IS M_rec and relation (1) closes on the same scalar
// relation (2) and (3) use.  A2, whose ciphertext decrypts to the ISSUER
// Identity, needs one more relation -- the membership of that decrypted point
// -- and is a separate circuit for that reason.

pragma circom 2.1.6;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "./ec/bn254_g_scalarmul.circom";
include "./ec/get_bn254.circom";
include "./leaf_tags.circom";

template MerkleProofFold(depth) {
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

// Recompose a canonical 4x64-bit limb array into one native field element.
// The limbs are range-checked wherever they enter a ScalarMulG, so this is an
// exact integer recomposition reduced mod the native field -- which is what
// point_to_words(P)[i] % F_R yields on the wallet side.
template Recompose4() {
    signal input  limbs[4];
    signal output out;
    out <== limbs[0] + limbs[1] * (1 << 64)
          + limbs[2] * (1 << 128) + limbs[3] * (1 << 192);
}

template DepositFoldA1(depth) {
    // ===== PUBLIC ============================================================
    signal input nullifier;                 // the spent note's nullifier
    signal input v;                         // the note face (the spend's face)
    signal input identityRoot;              // the posted aggregator root
    signal input eEncRx[4];  signal input eEncRy[4];   // the spend's ciphertext
    signal input eEncCx[4];  signal input eEncCy[4];
    signal input pkDepX[4];  signal input pkDepY[4];   // registered account key
    signal input eDepRx[4];  signal input eDepRy[4];   // registered credential
    signal input eDepCx[4];  signal input eDepCy[4];

    // ===== PRIVATE ===========================================================
    signal input rho;                       // note randomness
    signal input idHash;                    // Poseidon8(eNote, mIss, sigR, sigS)
    signal input eNote[4];                  // eNote coords (mod F_R)
    signal input mIss;                      // public issuer identity word
    signal input sigR[2];                   // issuer Schnorr nonce coords
    signal input sigS;                      // issuer Schnorr response word
    signal input rn[4];                     // eNote randomness
    signal input m_rec[4];                  // the Identity scalar
    signal input k_recv[4];                 // the RECEIVING secret
    signal input u[4];                      // v + rn*k_recv      (witnessed)
    signal input t[4];                      // eEnc total randomness
    signal input w[4];                      // m_rec + t*k_recv   (witnessed)
    signal input sk_dep[4];                 // the account key
    signal input r_E[4];                    // the account's registration nonce
    signal input cd[4];                     // m_rec + sk_dep*r_E (witnessed)
    signal input salt;                      // the holder's leaf salt
    signal input pathElements[depth];
    signal input pathIndices[depth];

    // ===== (0) the nullifier and the note's idHash ============================
    component nf = Poseidon(3);
    nf.inputs[0] <== rho;
    nf.inputs[1] <== idHash;
    nf.inputs[2] <== 4242;
    nullifier === nf.out;

    component idH = Poseidon(8);
    idH.inputs[0] <== eNote[0];  idH.inputs[1] <== eNote[1];
    idH.inputs[2] <== eNote[2];  idH.inputs[3] <== eNote[3];
    idH.inputs[4] <== mIss;
    idH.inputs[5] <== sigR[0];   idH.inputs[6] <== sigR[1];
    idH.inputs[7] <== sigS;
    idHash === idH.out;

    // ===== native-field scalar arithmetic ====================================
    component rnV = Recompose4();  rnV.limbs <== rn;
    component mV  = Recompose4();  mV.limbs  <== m_rec;
    component kV  = Recompose4();  kV.limbs  <== k_recv;
    component tV  = Recompose4();  tV.limbs  <== t;
    component skV = Recompose4();  skV.limbs <== sk_dep;
    component rEV = Recompose4();  rEV.limbs <== r_E;
    component uV  = Recompose4();  uV.limbs  <== u;
    component wV  = Recompose4();  wV.limbs  <== w;
    component cdV = Recompose4();  cdV.limbs <== cd;

    // The products are native signals, never witnessed limbs, so there is no
    // limb of theirs left to range-check or alias.
    signal rnk;  rnk  <== rnV.out * kV.out;      // rn * k
    signal tk;   tk   <== tV.out  * kV.out;      // t  * k
    signal skr;  skr  <== skV.out * rEV.out;     // sk_dep * r_E

    uV.out  === v        + rnk;                  // u  = v     + rn*k
    wV.out  === mV.out   + tk;                   // w  = m_rec + t*k
    cdV.out === mV.out   + skr;                  // cd = m_rec + sk_dep*r_E

    // ===== the seven fixed-base multiples ====================================
    // Seven, not nine: the leaf commits scalars, so no multiple of G is needed
    // to reach it.  Every witnessed limb array below enters a ScalarMulG, which
    // range-checks each limb with Num2Bits(64) -- so no limb anywhere in this
    // circuit is left unchecked.
    component rnG = ScalarMulG();  rnG.b <== rn;      // eNote.R
    component uG  = ScalarMulG();  uG.b  <== u;       // eNote.C
    component tG  = ScalarMulG();  tG.b  <== t;       // eEnc.R
    component wG  = ScalarMulG();  wG.b  <== w;       // eEnc.C  (relation 1)
    component skG = ScalarMulG();  skG.b <== sk_dep;  // pk_dep  (relation 2)
    component rEG = ScalarMulG();  rEG.b <== r_E;     // E_dep.R (relation 2)
    component cdG = ScalarMulG();  cdG.b <== cd;      // E_dep.C (relation 2)

    // ===== the note tie: eNote is keyed to this mailbox ======================
    component eNoteRx = Recompose4();  eNoteRx.limbs <== rnG.out[0];
    component eNoteRy = Recompose4();  eNoteRy.limbs <== rnG.out[1];
    eNoteRx.out === eNote[0];
    eNoteRy.out === eNote[1];

    component eNoteCx = Recompose4();  eNoteCx.limbs <== uG.out[0];
    component eNoteCy = Recompose4();  eNoteCy.limbs <== uG.out[1];
    eNoteCx.out === eNote[2];
    eNoteCy.out === eNote[3];

    // ===== (1) k decrypts the spend's ciphertext to M_rec ====================
    for (var i = 0; i < 4; i++) {
        tG.out[0][i] === eEncRx[i];
        tG.out[1][i] === eEncRy[i];
        wG.out[0][i] === eEncCx[i];
        wG.out[1][i] === eEncCy[i];
    }

    // ===== (2) the account credential decrypts to M_rec =====================
    for (var i = 0; i < 4; i++) {
        skG.out[0][i] === pkDepX[i];
        skG.out[1][i] === pkDepY[i];
        rEG.out[0][i] === eDepRx[i];
        rEG.out[1][i] === eDepRy[i];
        cdG.out[0][i] === eDepCx[i];
        cdG.out[1][i] === eDepCy[i];
    }

    // ===== (3) a registered leaf commits the pair (m_rec, k_recv) ===========
    // The same signals relations (1) and (2) consume, hashed directly: the tie
    // is an identity of signals rather than an inference across derivations.
    component leafH = Poseidon(4);
    leafH.inputs[0] <== LEAF_TAG_RECEIVING();
    leafH.inputs[1] <== mV.out;
    leafH.inputs[2] <== kV.out;
    leafH.inputs[3] <== salt;

    // ===== (4) the leaf's path folds to the posted root ======================
    component mp = MerkleProofFold(depth);
    mp.leaf <== leafH.out;
    for (var i = 0; i < depth; i++) {
        mp.pathElements[i] <== pathElements[i];
        mp.pathIndices[i]  <== pathIndices[i];
    }
    identityRoot === mp.root;
}

// The path is THIRTY-TWO levels: the leaf's identity-registry subtree (12,
// KYC_SUBTREE_DEPTH) and then the aggregator (20, AGGREGATOR_DEPTH), whose
// leaves are subtree roots and whose root is the posted identityRoot
// (accumulator specification, section 11.1).  Every level folds with the same
// Poseidon-2, so the two paths are one path here, and the subtree's slot in
// the aggregator stays a private input -- which authority a member belongs to
// is exactly what aggregation hides.  Python: MEMBERSHIP_PATH_DEPTH.
//
// The depth is baked into the r1cs, and so into the trusted setup.  The
// aggregator is twenty because authorities are a population rather than a
// roster (2**10 = 1024 subtrees is the wrong order of magnitude); the extra
// levels cost about 240 constraints each against this circuit's millions.
component main { public [
    nullifier, v, identityRoot,
    eEncRx, eEncRy, eEncCx, eEncCy,
    pkDepX, pkDepY,
    eDepRx, eDepRy, eDepCx, eDepCy
] } = DepositFoldA1(32);
