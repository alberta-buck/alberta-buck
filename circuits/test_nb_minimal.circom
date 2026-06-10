pragma circom 2.1.6;
include "../node_modules/circomlib/circuits/poseidon.circom";
template TestNBMin() {
    signal input nullifier;
    signal input rho;
    signal input idHash;
    signal input eNote[4];
    signal input eIss0[4];
    signal input R0x_limb[4];
    signal input R0y_limb[4];

    // (1) nullifier check
    component nf = Poseidon(3);
    nf.inputs[0] <== rho;
    nf.inputs[1] <== idHash;
    nf.inputs[2] <== 4242;
    nullifier === nf.out;

    // (2) idHash check
    component idH = Poseidon(8);
    idH.inputs[0] <== eNote[0]; idH.inputs[1] <== eNote[1];
    idH.inputs[2] <== eNote[2]; idH.inputs[3] <== eNote[3];
    idH.inputs[4] <== eIss0[0]; idH.inputs[5] <== eIss0[1];
    idH.inputs[6] <== eIss0[2]; idH.inputs[7] <== eIss0[3];
    idHash === idH.out;

    // Cross-constraint: R0x_limb -> eIss0[0]
    signal R0x_single;
    R0x_single <== R0x_limb[0] + R0x_limb[1] * (1 << 64)
                + R0x_limb[2] * (1 << 128) + R0x_limb[3] * (1 << 192);
    R0x_single === eIss0[0];
}
component main { public [ nullifier ] } = TestNBMin();
