// BN254 (alt_bn128) extension for circom-lib's get.circom.
// Provides EllipticCurveGetGenerator and EllipticCurveGetDummy for BN254.
// BN254 curve: y^2 = x^3 + 3, generator G = (1, 2).

pragma circom 2.1.6;

function BN254_MOD_Q0() { return 4332616871279656263; }
function BN254_MOD_Q1() { return 10917124144477883021; }
function BN254_MOD_Q2() { return 13281191951274694749; }
function BN254_MOD_Q3() { return 3486998266802970665; }

template EllipticCurveGetGeneratorBN254(CHUNK_SIZE, CHUNK_NUMBER, A, B, P) {
    assert(CHUNK_SIZE == 64 && CHUNK_NUMBER == 4);
    signal output gen[2][CHUNK_NUMBER];

    // BN254 generator G = (1, 2) in 4-limb form.
    gen[0][0] <== 1;
    gen[0][1] <== 0;
    gen[0][2] <== 0;
    gen[0][3] <== 0;
    gen[1][0] <== 2;
    gen[1][1] <== 0;
    gen[1][2] <== 0;
    gen[1][3] <== 0;
}

template EllipticCurveGetDummyBN254(CHUNK_SIZE, CHUNK_NUMBER, A, B, P) {
    assert(CHUNK_SIZE == 64 && CHUNK_NUMBER == 4);
    signal output dummyPoint[2][CHUNK_NUMBER];

    // G * 2^256 mod ORDER, verified on curve.
    dummyPoint[0][0] <== 2461376521386225594;
    dummyPoint[0][1] <== 1551624202502274584;
    dummyPoint[0][2] <== 18140410977652634304;
    dummyPoint[0][3] <== 2123611379616921749;
    dummyPoint[1][0] <== 17960298251312869361;
    dummyPoint[1][1] <== 3751284194431087161;
    dummyPoint[1][2] <== 5176772293933894022;
    dummyPoint[1][3] <== 2326139083189292223;
}
