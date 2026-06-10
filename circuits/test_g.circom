pragma circom 2.1.6;
include "./ec/bn254_g_scalarmul.circom";
template TestG() {
    signal input s[4];
    signal output out[2][4];
    component sg = ScalarMulG();
    sg.b <== s;
    out <== sg.out;
}
component main = TestG();
