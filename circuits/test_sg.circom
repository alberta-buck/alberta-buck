pragma circom 2.1.6;
include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "./ec/bn254_h_scalarmul.circom";
include "./ec/bn254_g_scalarmul.circom";
include "./ec/get_bn254.circom";
include "../lib/circom-lib/circuits/ec/curve.circom";
template TestSG() {
    signal input s[4];
    signal output out[2][4];
    component sg = ScalarMulG();
    sg.b <== s;
    out <== sg.out;
}
component main = TestSG();
