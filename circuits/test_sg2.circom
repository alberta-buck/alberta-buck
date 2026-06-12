pragma circom 2.1.6;
include "./ec/bn254_h_scalarmul.circom";
include "./ec/get_bn254.circom";
include "../lib/circom-lib/circuits/ec/curve.circom";
template TestSG2() {
    signal input s[4];
    signal output out[2][4];
    signal dummy;
    dummy <== 0;
    dummy * dummy === 0;
    var A[4] = [BN254_A0(), BN254_A1(), BN254_A2(), BN254_A3()];
    var B[4] = [BN254_B0(), BN254_B1(), BN254_B2(), BN254_B3()];
    var P[4] = [BN254_MOD_Q0(), BN254_MOD_Q1(), BN254_MOD_Q2(), BN254_MOD_Q3()];
    component sg = EllipticCurveScalarGeneratorMultiplicationNonOptimised(64, 4, A, B, P);
    sg.scalar <== s;
    sg.dummy  <== dummy;
    out <== sg.out;
}
component main = TestSG2();
