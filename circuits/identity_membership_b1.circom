// B1's P-bound identity membership -- the repaired successor to
// identity_membership_g1tie.circom.
//
// B1 is the one flavour that needs no folded gate.  Its two facts -- "my payout
// account is bound to this Identity" and "this ciphertext encrypts that same
// Identity for the issuer" -- rest on ONE secret, m_dep, so the shared
// Fiat-Shamir nonce of the depositor-binding sigma is a genuine tie.  (The
// A-flavours had to fold because their two facts rest on two different secrets,
// the Identity and the receiving key, and no nonce and no generator can join
// those.)
//
// What B1 does need is for the sigma and THIS proof to agree about the public
// point P_dep they share.  This circuit proves
//
//   (1) T = b * H_PEDERSEN                 -- PROVEN, not witnessed
//   (2) P_dep = M + T                      -- with the addition's precondition enforced
//   (3) leaf = Poseidon(M.x, M.y, salt)    -- the salted private-subtree leaf
//   (4) the leaf's path folds to identityRoot
//
// and all three of review finding 5's defects are repaired here rather than
// inherited:
//
//  * THE FREE TIE POINT.  g1tie took T as a witnessed input and never proved
//    it was any multiple of H, so a prover could choose T and satisfy
//    P_dep = M + T for any M it liked.  Here T is computed by ScalarMulHP from
//    the blind b, so it is a multiple of H_PEDERSEN by construction.
//  * THE KNOWN GENERATOR LOGARITHM.  Proving T = b*H is not enough on its own
//    if log_G(H) is public: a depositor holding any registered identity scalar
//    m' -- which counterparties hold by design -- would shift b by
//    (m_dep - m')/h and make this proof speak about m' while the sigma speaks
//    about its own m_dep, so an UNREGISTERED depositor spends.  H_PEDERSEN is
//    hashed to the curve, so there is no such h to shift by.
//  * UNCHECKED LIMBS AND INCOMPLETE ADDITION.  M's limbs are witnessed, so they
//    are range-checked here with Num2Bits(64).  circom-lib offers no complete
//    addition, so the one addition asserts that its addends' x-coordinates
//    differ, which excludes the doubling and the negation case together;
//    comparing them reduced mod the native field suffices, because equal true
//    coordinates would be congruent.
//
// The leaf is the salted one: a private subtree's membership must not be a
// deterministic function of the identity, or a registry holding every scalar it
// certified could decide membership at will.  g1tie hashed Poseidon(2) over the
// coordinates alone, which is the shape this replaces.

pragma circom 2.1.6;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "./ec/bn254_hp_scalarmul.circom";
include "./ec/get_bn254.circom";
include "../lib/circom-lib/circuits/ec/curve.circom";

template MerkleProofB1(depth) {
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

template Recompose4B1() {
    signal input  limbs[4];
    signal output out;
    out <== limbs[0] + limbs[1] * (1 << 64)
          + limbs[2] * (1 << 128) + limbs[3] * (1 << 192);
}

template IdentityMembershipB1(depth) {
    // ===== PUBLIC ============================================================
    signal input identityRoot;
    signal input PI_x[4];                   // P_dep, shared with the sigma
    signal input PI_y[4];

    // ===== PRIVATE ===========================================================
    signal input Mx[4];                     // the depositor Identity point
    signal input My[4];
    signal input b[4];                      // the blind, so T is PROVEN
    signal input salt;                      // the holder's leaf salt
    signal input pathElements[depth];
    signal input pathIndices[depth];

    var A[4] = [0, 0, 0, 0];
    var B[4] = [3, 0, 0, 0];
    var P[4] = [BN254_MOD_Q0(), BN254_MOD_Q1(), BN254_MOD_Q2(), BN254_MOD_Q3()];
    signal dummy;
    dummy <== 0;
    dummy * dummy === 0;

    // ===== M's limbs are witnessed, so range-check them ======================
    component mxBits[4];
    component myBits[4];
    for (var i = 0; i < 4; i++) {
        mxBits[i] = Num2Bits(64);  mxBits[i].in <== Mx[i];
        myBits[i] = Num2Bits(64);  myBits[i].in <== My[i];
    }
    component MxV = Recompose4B1();  MxV.limbs <== Mx;
    component MyV = Recompose4B1();  MyV.limbs <== My;

    // ===== (1) T = b * H_PEDERSEN, computed rather than trusted ==============
    component bH = ScalarMulHP();
    bH.b <== b;
    component TxV = Recompose4B1();  TxV.limbs <== bH.out[0];

    // ===== (2) P_dep = M + T, with the incomplete addition's precondition ====
    // x(M) != x(T) rules out both excluded cases at once: doubling needs the
    // x-coordinates equal, and so does adding a point to its own negation.
    signal dx;
    signal dxInv;
    dx <== MxV.out - TxV.out;
    dxInv <-- 1 / dx;
    dx * dxInv === 1;

    component add = EllipticCurveAddOptimised(64, 4, A, B, P);
    add.in1[0] <== Mx;
    add.in1[1] <== My;
    add.in2 <== bH.out;
    add.dummy <== dummy;

    for (var i = 0; i < 4; i++) {
        add.out[0][i] === PI_x[i];
        add.out[1][i] === PI_y[i];
    }

    // ===== (3)+(4) the salted leaf, and its path =============================
    component leafH = Poseidon(3);
    leafH.inputs[0] <== MxV.out;
    leafH.inputs[1] <== MyV.out;
    leafH.inputs[2] <== salt;

    component mp = MerkleProofB1(depth);
    mp.leaf <== leafH.out;
    for (var i = 0; i < depth; i++) {
        mp.pathElements[i] <== pathElements[i];
        mp.pathIndices[i]  <== pathIndices[i];
    }
    identityRoot === mp.root;
}

component main { public [ identityRoot, PI_x, PI_y ] } = IdentityMembershipB1(20);
