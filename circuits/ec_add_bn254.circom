// Standalone G1 point addition for BN254 — extracted from circom-lib.
// Does NOT include get.circom — safe for circom 2.2.2 compilation.
pragma circom 2.1.6;

include "../lib/circom-lib/circuits/bigInt/bigIntOverflow.circom";
include "../lib/circom-lib/circuits/bigInt/bigIntFunc.circom";

/// λ = (y2 - y1) / (x2 - x1)
/// x3 = λ * λ - x1 - x2
/// y3 = λ * (x1 - x3) - y1
template EllipticCurveAddOptimised(CHUNK_SIZE, CHUNK_NUMBER, A, B, P) {
    signal input in1[2][CHUNK_NUMBER];
    signal input in2[2][CHUNK_NUMBER];
    signal input dummy;

    signal output out[2][CHUNK_NUMBER];

    dummy * dummy === 0;

    // x2 - x1
    component sub = BigSubModOverflow(CHUNK_SIZE, CHUNK_NUMBER);
    sub.in1 <== in2[0];
    sub.in2 <== in1[0];
    sub.modulus <== P;
    sub.dummy <== dummy;

    // y2 - y1
    component sub2 = BigSubModOverflow(CHUNK_SIZE, CHUNK_NUMBER);
    sub2.in1 <== in2[1];
    sub2.in2 <== in1[1];
    sub2.modulus <== P;
    sub2.dummy <== dummy;

    // (x2 - x1) ** -1
    component modInv = BigModInvOverflow(CHUNK_SIZE, CHUNK_NUMBER, CHUNK_NUMBER);
    modInv.in <== sub.out;
    modInv.modulus <== P;
    modInv.dummy <== dummy;

    // (y2 - y1) * 1 / (x2 - x1)
    component mult = BigMultOverflow(CHUNK_SIZE, CHUNK_NUMBER);
    mult.in[0] <== sub2.out;
    mult.in[1] <== modInv.out;
    mult.dummy <== dummy;

    // (y2 - y1) * 1 / (x2 - x1) % P ==> lambda
    component mod = BigModOverflow(CHUNK_SIZE, CHUNK_NUMBER * 2 - 1, CHUNK_NUMBER, 2);
    mod.base <== mult.out;
    mod.modulus <== P;
    mod.dummy <== dummy;

    // lambda * lambda
    component mult2 = BigMultOptimisedOverflow(CHUNK_SIZE, CHUNK_NUMBER);
    mult2.in[0] <== mod.mod;
    mult2.in[1] <== mod.mod;
    mult2.dummy <== dummy;

    // P - in1
    component sub3 = BigSubModOverflow(CHUNK_SIZE, CHUNK_NUMBER);
    sub3.in1 <== P;
    sub3.in2 <== in1[0];
    sub3.modulus <== P;
    sub3.dummy <== dummy;

    // P - in2
    component sub4 = BigSubModOverflow(CHUNK_SIZE, CHUNK_NUMBER);
    sub4.in1 <== P;
    sub4.in2 <== in2[0];
    sub4.modulus <== P;
    sub4.dummy <== dummy;

    // 2 * P - in1 - in2
    component add = BigAddOverflow(CHUNK_SIZE, CHUNK_NUMBER);
    add.in[0] <== sub3.out;
    add.in[1] <== sub4.out;
    add.dummy <== dummy;

    // lambda * lambda + 2 * P - in1 - in2
    component add2 = BigAddNonEqualOverflow(CHUNK_SIZE, CHUNK_NUMBER * 2 - 1, CHUNK_NUMBER);
    add2.in1 <== mult2.out;
    add2.in2 <== add.out;
    add2.dummy <== dummy;

    // (lambda^2 + 2*P - in1 - in2) % P ==> x3
    component mod2 = BigModOverflow(CHUNK_SIZE, CHUNK_NUMBER * 2 - 1, CHUNK_NUMBER, 2);
    mod2.base <== add2.out;
    mod2.modulus <== P;
    mod2.dummy <== dummy;

    out[0] <== mod2.mod;

    // x1 - x3
    component sub5 = BigSubModOverflow(CHUNK_SIZE, CHUNK_NUMBER);
    sub5.in1 <== in1[0];
    sub5.in2 <== out[0];
    sub5.modulus <== P;
    sub5.dummy <== dummy;

    // lambda * (x1 - x3)
    component mult3 = BigMultNonEqualOverflow(CHUNK_SIZE, CHUNK_NUMBER * 2 - 1, CHUNK_NUMBER);
    mult3.in1 <== mult.out;
    mult3.in2 <== sub5.out;
    mult3.dummy <== dummy;

    // P - y1
    component sub6 = BigSubModOverflow(CHUNK_SIZE, CHUNK_NUMBER);
    sub6.in1 <== P;
    sub6.in2 <== in1[1];
    sub6.modulus <== P;
    sub6.dummy <== dummy;

    // lambda * (x1 - x3) + P - y1
    component add3 = BigAddNonEqualOverflow(CHUNK_SIZE, CHUNK_NUMBER * 3 - 2, CHUNK_NUMBER);
    add3.in1 <== mult3.out;
    add3.in2 <== sub6.out;
    add3.dummy <== dummy;

    // (lambda*(x1-x3) + P - y1) % P ==> y3
    component mod3 = BigModOverflow(CHUNK_SIZE, CHUNK_NUMBER * 3 - 2, CHUNK_NUMBER, 3);
    mod3.base <== add3.out;
    mod3.modulus <== P;
    mod3.dummy <== dummy;

    out[1] <== mod3.mod;
}
