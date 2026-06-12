pragma circom 2.1.6;
include "./ec/powers/bn254_g_pows.circom";
template Test() {
    signal input x;
    signal output y;
    var t[32][256][2][4] = getGPowStride8TableBN254(64, 4);
    y <== x;
}
component main = Test();
