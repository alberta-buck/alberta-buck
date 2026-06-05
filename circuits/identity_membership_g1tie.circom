// Identity membership circuit with G1 tie — Phase C.
// Uses circom-lib EllipticCurveAddOptimised for the G1 point addition.
// Prover supplies T = b*H; circuit verifies P_I = M + T.
//
// Public:  identityRoot, PI_x[4], PI_y[4]  (4-limb F_q)
// Private: Mx[4], My[4], Mx_mod, My_mod, Tx[4], Ty[4], Merkle path

pragma circom 2.1.6;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "../lib/circom-lib/circuits/ec/curve.circom";

// BN254 curve params
function P0() { return 4332616871279656263; }
function P1() { return 10917124144477883021; }
function P2() { return 13281191951274694749; }
function P3() { return 3486998266802970665; }

// ---- Merkle proof -----------------------------------------------------------

template MerkleProof(depth) {
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
        switchers[i] = Switcher(); switchers[i].sel <== pathIndices[i];
        switchers[i].L <== levels[i]; switchers[i].R <== pathElements[i];
        hashers[i] = Poseidon(2);
        hashers[i].inputs[0] <== switchers[i].outL;
        hashers[i].inputs[1] <== switchers[i].outR;
        levels[i + 1] <== hashers[i].out;
    }
    root <== levels[depth];
}

// ---- Main -------------------------------------------------------------------

template IdentityMembershipG1Tie(depth) {
    // ===== PUBLIC ===========================================================
    signal input identityRoot;
    signal input PI_x[4];     // P_I point (4-limb F_q)
    signal input PI_y[4];

    // ===== PRIVATE: M =======================================================
    signal input Mx[4];       // M.x (4-limb F_q)
    signal input My[4];
    signal input Mx_mod;      // M.x reduced mod F_R (for Poseidon leaf)
    signal input My_mod;

    // ===== PRIVATE: T = b*H (prover-supplied) ===============================
    signal input Tx[4];
    signal input Ty[4];

    // ===== PRIVATE: Merkle path =============================================
    signal input pathElements[depth];
    signal input pathIndices[depth];

    // ===== CONSTRAINT 1: M ∈ identityRoot (NATIVE) ==========================
    signal Mx_rec;
    Mx_rec <== Mx[0] + Mx[1]*(1<<64) + Mx[2]*(1<<128) + Mx[3]*(1<<192);
    Mx_rec === Mx_mod;

    signal My_rec;
    My_rec <== My[0] + My[1]*(1<<64) + My[2]*(1<<128) + My[3]*(1<<192);
    My_rec === My_mod;

    component leafH = Poseidon(2);
    leafH.inputs[0] <== Mx_mod; leafH.inputs[1] <== My_mod;

    component mp = MerkleProof(depth);
    mp.leaf <== leafH.out;
    for (var i = 0; i < depth; i++) {
        mp.pathElements[i] <== pathElements[i];
        mp.pathIndices[i]  <== pathIndices[i];
    }
    mp.root === identityRoot;

    // ===== CONSTRAINT 2: P_I = M + T (circom-lib point addition) ==============
    var A[4] = [0, 0, 0, 0];
    var B[4] = [3, 0, 0, 0];
    var P[4] = [P0(), P1(), P2(), P3()];

    component add = EllipticCurveAddOptimised(64, 4, A, B, P);
    // add.in1 = M
    add.in1[0][0] <== Mx[0]; add.in1[0][1] <== Mx[1]; add.in1[0][2] <== Mx[2]; add.in1[0][3] <== Mx[3];
    add.in1[1][0] <== My[0]; add.in1[1][1] <== My[1]; add.in1[1][2] <== My[2]; add.in1[1][3] <== My[3];
    // add.in2 = T
    add.in2[0][0] <== Tx[0]; add.in2[0][1] <== Tx[1]; add.in2[0][2] <== Tx[2]; add.in2[0][3] <== Tx[3];
    add.in2[1][0] <== Ty[0]; add.in2[1][1] <== Ty[1]; add.in2[1][2] <== Ty[2]; add.in2[1][3] <== Ty[3];

    signal dummy;
    dummy <== 0;
    dummy * dummy === 0;
    add.dummy <== dummy;

    // Verify add.out == P_I
    add.out[0][0] === PI_x[0]; add.out[0][1] === PI_x[1]; add.out[0][2] === PI_x[2]; add.out[0][3] === PI_x[3];
    add.out[1][0] === PI_y[0]; add.out[1][1] === PI_y[1]; add.out[1][2] === PI_y[2]; add.out[1][3] === PI_y[3];
}

component main { public [ identityRoot, PI_x, PI_y ] } = IdentityMembershipG1Tie(10);
