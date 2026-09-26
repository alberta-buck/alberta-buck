// SPDX-License-Identifier: GPL-3.0-or-later
pragma circom 2.1.6;
include "../../../node_modules/circomlib/circuits/poseidon.circom";
include "../../../node_modules/circomlib/circuits/bitify.circom";

// REVIEW ONLY. A schematic ADDRESSED payment and certified-commitment showing.
// This is not Notes' encoding, PS issuance, encryption, revocation or a spend
// implementation. It isolates witness equality, hidden holder-secret knowledge,
// flavor/issuer constraints and context binding in one REAL proof relation.
// Tests choose the admitted root; certification of that root remains a premise.
template CredentialPayment(depth) {
    signal input identityRoot;
    signal input payment;
    signal input expectedFlavor;
    signal input expectedIssuer;
    signal input context;
    signal input m;
    signal input holderSecret;
    signal input salt;
    signal input siblings[depth];
    signal input bits[depth];
    signal input flavor;
    signal input value;
    signal input rho;
    signal input issuer;
    signal input recipient;
    signal input predicate;
    signal input account;
    signal input chainid;
    signal input registry;
    signal input nonce;

    component holder = Poseidon(1);
    holder.inputs[0] <== holderSecret;
    component leaf = Poseidon(4);
    leaf.inputs[0] <== 90201;
    leaf.inputs[1] <== m;
    leaf.inputs[2] <== holder.out;
    leaf.inputs[3] <== salt;
    component nodes[depth];
    signal levels[depth+1];
    levels[0] <== leaf.out;
    for (var i=0; i<depth; i++) {
        bits[i]*(bits[i]-1) === 0;
        nodes[i] = Poseidon(2);
        nodes[i].inputs[0] <== levels[i] + bits[i]*(siblings[i]-levels[i]);
        nodes[i].inputs[1] <== siblings[i] + bits[i]*(levels[i]-siblings[i]);
        levels[i+1] <== nodes[i].out;
    }
    levels[depth] === identityRoot;

    component note = Poseidon(7);
    note.inputs[0] <== 90202;
    note.inputs[1] <== flavor;
    note.inputs[2] <== value;
    note.inputs[3] <== rho;
    note.inputs[4] <== issuer;
    note.inputs[5] <== recipient;
    note.inputs[6] <== predicate;
    note.out === payment;
    flavor === expectedFlavor;
    issuer === expectedIssuer;
    recipient === m;
    predicate === 0;
    (flavor-1)*(flavor-2) === 0;
    component range = Num2Bits(128);
    range.in <== value;

    component ctx = Poseidon(5);
    ctx.inputs[0] <== 90203;
    ctx.inputs[1] <== account;
    ctx.inputs[2] <== chainid;
    ctx.inputs[3] <== registry;
    ctx.inputs[4] <== nonce;
    ctx.out === context;
}
component main {public [identityRoot, payment, expectedFlavor, expectedIssuer, context]} = CredentialPayment(10);
