// BN254 H-point scalar multiplication using circom-lib's EC primitives.
//
// Computes T = b * H where H is the NUMS generator from IdentityRegistry.
// Uses precomputed stride-8 powers table (bn254_h_pows.circom).
// Based on circom-lib's EllipticCurveScalarGeneratorMultiplicationOptimised.
//
// BN254 curve: y^2 = x^3 + 3  (A=0, B=3)
// Base field: ~2^254

pragma circom 2.1.6;

include "../../lib/circom-lib/circuits/bigInt/bigIntOverflow.circom";
include "../../lib/circom-lib/circuits/bigInt/bigIntFunc.circom";
include "../../lib/circom-lib/circuits/bitify/bitify.circom";
include "../../lib/circom-lib/circuits/bitify/comparators.circom";
include "../../lib/circom-lib/circuits/int/arithmetic.circom";
include "../../lib/circom-lib/circuits/ec/curve.circom";
include "./powers/bn254_h_pows.circom";
include "./get_bn254.circom";

// BN254 curve params inherited from get_bn254.circom
function BN254_A0() { return 0; } function BN254_A1() { return 0; }
function BN254_A2() { return 0; } function BN254_A3() { return 0; }
function BN254_B0() { return 3; } function BN254_B1() { return 0; }
function BN254_B2() { return 0; } function BN254_B3() { return 0; }

/// Computes b * H where H is the NUMS generator.
/// b is a 256-bit scalar in 4-limb representation.
/// Uses circom-lib's optimized stride-8 windowed multiplication.
template ScalarMulH() {
    signal input b[4];
    signal output out[2][4];

    var A[4] = [BN254_A0(), BN254_A1(), BN254_A2(), BN254_A3()];
    var B[4] = [BN254_B0(), BN254_B1(), BN254_B2(), BN254_B3()];
    var P[4] = [BN254_MOD_Q0(), BN254_MOD_Q1(), BN254_MOD_Q2(), BN254_MOD_Q3()];

    var STRIDE = 8;
    var parts = 32;  // 256 / 8

    var powers[parts][256][2][4] = getHPowStride8Table(64, 4);

    signal dummy;
    dummy <== 0;
    dummy * dummy === 0;

    // Decompose scalar into bits.
    component num2bits[4];
    for (var i = 0; i < 4; i++) {
        num2bits[i] = Num2Bits(64);
        num2bits[i].in <== b[i];
    }

    // Recombine into 8-bit windows.
    component bits2num[parts];
    for (var i = 0; i < parts; i++) {
        bits2num[i] = Bits2Num(STRIDE);
        for (var j = 0; j < STRIDE; j++) {
            bits2num[i].in[j] <== num2bits[(i * STRIDE + j) \ 64].out[(i * STRIDE + j) % 64];
        }
    }

    // Select precomputed point for each window.
    component equal[parts][256];
    signal resultCoord[parts][256][2][4];

    for (var i = 0; i < parts; i++) {
        for (var j = 0; j < 256; j++) {
            equal[i][j] = IsEqual();
            equal[i][j].in[0] <== j;
            equal[i][j].in[1] <== bits2num[i].out;

            for (var axis = 0; axis < 4; axis++) {
                resultCoord[i][j][0][axis] <== equal[i][j].out * powers[i][j][0][axis];
                resultCoord[i][j][1][axis] <== equal[i][j].out * powers[i][j][1][axis];
            }
        }
    }

    // Sum selected points per window.
    component sumElements[parts][2][4];
    for (var i = 0; i < parts; i++) {
        for (var ax = 0; ax < 2; ax++) {
            for (var axis = 0; axis < 4; axis++) {
                sumElements[i][ax][axis] = GetSumOfNElements(256);
                sumElements[i][ax][axis].dummy <== dummy;
                for (var j = 0; j < 256; j++) {
                    sumElements[i][ax][axis].in[j] <== resultCoord[i][j][ax][axis];
                }
            }
        }
    }

    // Handle zero windows (if the 8-bit value is 0, the sum is 0 → point at infinity).
    component isZero[parts];
    for (var i = 0; i < parts; i++) {
        isZero[i] = IsZero();
        isZero[i].in <== sumElements[i][0][0].out + sumElements[i][0][1].out
                     + sumElements[i][0][2].out + sumElements[i][0][3].out
                     + sumElements[i][1][0].out + sumElements[i][1][1].out
                     + sumElements[i][1][2].out + sumElements[i][1][3].out;
    }

    component getDummy = EllipticCurveGetDummyBN254(64, 4, A, B, P);

    signal dummyReplace[parts][2][4];
    for (var i = 0; i < parts; i++) {
        for (var ax = 0; ax < 2; ax++) {
            for (var axis = 0; axis < 4; axis++) {
                dummyReplace[i][ax][axis] <== isZero[i].out * getDummy.dummyPoint[ax][axis];
            }
        }
    }

    signal addPoints[parts][2][4];
    for (var i = 0; i < parts; i++) {
        for (var ax = 0; ax < 2; ax++) {
            for (var axis = 0; axis < 4; axis++) {
                addPoints[i][ax][axis] <== (1 - isZero[i].out) * sumElements[i][ax][axis].out
                                         + dummyReplace[i][ax][axis];
            }
        }
    }

    // Chain-add all 32 selected points.
    component adders[parts - 1];
    component isDummyLeft[parts - 1];
    component isDummyRight[parts - 1];

    signal resLeft[parts][2][4];
    signal resLeft2[parts][2][4];
    signal resRight[parts][2][4];
    signal resRight2[parts][2][4];
    signal result[parts][2][4];

    for (var i = 0; i < parts - 1; i++) {
        adders[i] = EllipticCurveAddOptimised(64, 4, A, B, P);
        adders[i].dummy <== dummy;

        isDummyLeft[i] = IsEqual();
        isDummyRight[i] = IsEqual();
        isDummyLeft[i].in[0] <== getDummy.dummyPoint[0][0];
        isDummyRight[i].in[0] <== getDummy.dummyPoint[0][0];

        if (i == 0) {
            isDummyLeft[i].in[1] <== addPoints[i][0][0];
            isDummyRight[i].in[1] <== addPoints[i + 1][0][0];
            adders[i].in1 <== addPoints[i];

            for (var j = 0; j < 3; j++) {
                adders[i].in2[0][j] <== addPoints[i + 1][0][j];
                adders[i].in2[1][j] <== addPoints[i + 1][1][j];
            }
            adders[i].in2[0][3] <== addPoints[i + 1][0][3] + isDummyRight[i].out * isDummyRight[i].out;
            adders[i].in2[1][3] <== addPoints[i + 1][1][3] + isDummyRight[i].out * isDummyRight[i].out;

            for (var ax = 0; ax < 2; ax++) {
                for (var j = 0; j < 4; j++) {
                    resRight[i][ax][j] <== (1 - isDummyRight[i].out) * adders[i].out[ax][j];
                    resRight2[i][ax][j] <== isDummyRight[i].out * addPoints[i][ax][j] + resRight[i][ax][j];
                    resLeft[i][ax][j] <== isDummyLeft[i].out * addPoints[i + 1][ax][j];
                    resLeft2[i][ax][j] <== (1 - isDummyLeft[i].out) * resRight2[i][ax][j] + resLeft[i][ax][j];
                    result[i][ax][j] <== resLeft2[i][ax][j];
                }
            }
        } else {
            isDummyLeft[i].in[1] <== result[i - 1][0][0];
            isDummyRight[i].in[1] <== addPoints[i + 1][0][0];
            adders[i].in1 <== result[i - 1];

            for (var j = 0; j < 3; j++) {
                adders[i].in2[0][j] <== addPoints[i + 1][0][j];
                adders[i].in2[1][j] <== addPoints[i + 1][1][j];
            }
            adders[i].in2[0][3] <== addPoints[i + 1][0][3] + isDummyRight[i].out * isDummyRight[i].out;
            adders[i].in2[1][3] <== addPoints[i + 1][1][3] + isDummyRight[i].out * isDummyRight[i].out;

            for (var ax = 0; ax < 2; ax++) {
                for (var j = 0; j < 4; j++) {
                    resRight[i][ax][j] <== (1 - isDummyRight[i].out) * adders[i].out[ax][j];
                    resRight2[i][ax][j] <== isDummyRight[i].out * result[i - 1][ax][j] + resRight[i][ax][j];
                    resLeft[i][ax][j] <== isDummyLeft[i].out * addPoints[i + 1][ax][j];
                    resLeft2[i][ax][j] <== (1 - isDummyLeft[i].out) * resRight2[i][ax][j] + resLeft[i][ax][j];
                    result[i][ax][j] <== resLeft2[i][ax][j];
                }
            }
        }
    }

    out <== result[parts - 2];
}
