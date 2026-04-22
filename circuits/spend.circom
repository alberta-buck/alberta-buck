pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/bitify.circom";
include "../node_modules/circomlib/circuits/switcher.circom";

// Spend circuit -- Phase 7 B-spend (bearer) shape for BUCK Notes.
//
// Proves that the prover holds a Poseidon-5 opening of *some* commitment
// included in the on-chain Merkle accumulator under a recent root, and that
// the public `face` matches the witness `v` and the public `nullifier` is
// the prescribed Poseidon-3 derivation from the witness.
//
// The bearer/B-spend semantics are: knowledge of the opening *is* the spend
// authorization.  The current circuit does not constrain `flavor`, so it
// will accept any commitment opening regardless of A1/A2/B1 -- distinguishing
// A-flavor (which requires in-circuit ElGamal decryption + Chaum-Pedersen
// equality against the registered identity ciphertext) from B-flavor is
// deferred to a follow-up `spend_a.circom`.  Until then, A-flavor notes are
// effectively bearer-spendable; the on-chain wallet conventions and the
// pool's identity-bound transfer surface still apply.
//
// Public:  noteRoot, nullifier, face, recipient, chainId
// Private: flavor, v, rho, idHash, predicate,
//          pathElements[depth], pathIndices[depth]
//
// Constraints:
//   (R)   face in [0, 2^128) and v in [0, 2^128); face == v
//   (C)   cm = Poseidon([flavor, v, rho, idHash, predicate])
//   (M)   walking (cm, siblings, indices) up `depth` Poseidon-2 hashes
//         yields noteRoot
//   (N)   nullifier = Poseidon([rho, idHash, NULLIFIER_TAG])
//   (B)   ghost binding for recipient and chainId so they appear in at
//         least one R1CS row; Groth16's IC[] commitment then makes them
//         non-malleable from the mempool's perspective
//
// The face range bound is the same 2^128 cap mint uses (Open Question 1):
// without it a witness with v near r could spoof the public face.

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
        // pathIndices[i] is a single bit
        pathIndices[i] * (pathIndices[i] - 1) === 0;
        switchers[i] = Switcher();
        switchers[i].sel <== pathIndices[i];
        switchers[i].L   <== levels[i];
        switchers[i].R   <== pathElements[i];
        hashers[i] = Poseidon(2);
        hashers[i].inputs[0] <== switchers[i].outL;
        hashers[i].inputs[1] <== switchers[i].outR;
        levels[i + 1] <== hashers[i].out;
    }
    root <== levels[depth];
}

template Spend(depth) {
    // ---- public inputs ----
    signal input noteRoot;
    signal input nullifier;
    signal input face;
    signal input recipient;
    signal input chainId;

    // ---- private witness ----
    signal input flavor;
    signal input v;
    signal input rho;
    signal input idHash;
    signal input predicate;
    signal input pathElements[depth];
    signal input pathIndices[depth];

    // (R) Range-bound public face and witness v to 128 bits, and bind them.
    component faceRange = Num2Bits(128);
    faceRange.in <== face;
    component vRange    = Num2Bits(128);
    vRange.in    <== v;
    face === v;

    // (C) Recompute the leaf commitment from the witness opening.
    component cm = Poseidon(5);
    cm.inputs[0] <== flavor;
    cm.inputs[1] <== v;
    cm.inputs[2] <== rho;
    cm.inputs[3] <== idHash;
    cm.inputs[4] <== predicate;

    // (M) Merkle membership: cm + siblings -> noteRoot.
    component mp = MerkleProof(depth);
    mp.leaf <== cm.out;
    for (var i = 0; i < depth; i++) {
        mp.pathElements[i] <== pathElements[i];
        mp.pathIndices[i]  <== pathIndices[i];
    }
    mp.root === noteRoot;

    // (N) Nullifier = Poseidon-3 of (rho, idHash, tag).  rho is the bearer
    //     unforgeability secret -- without it the contract has no way to
    //     check that the spender actually holds the note opening, since
    //     idHash and predicate are derivable from the recipient's identity.
    //     The constant tag keeps nullifier preimages disjoint from the
    //     Poseidon-5 commitment preimages so a rho/idHash collision between
    //     a commitment and a nullifier is structurally impossible.
    component nf = Poseidon(3);
    nf.inputs[0] <== rho;
    nf.inputs[1] <== idHash;
    nf.inputs[2] <== 4242;
    nullifier === nf.out;

    // (B) Ghost-bind recipient and chainId so they participate in R1CS
    //     constraints (otherwise circom's optimizer may drop unreferenced
    //     public signals, which would silently widen the spend surface).
    //     Groth16's verifier folds public inputs into the pairing check via
    //     IC[]; one quadratic row per signal is enough to anchor them.
    signal recipBound;  recipBound <== recipient * recipient;
    signal chainBound;  chainBound <== chainId   * chainId;
}

// Tree depth pinned to 20 == Notes.TREE_DEPTH.  Changing this re-templates
// the circuit and requires regenerating the verifier + redeploying Notes.
component main { public [ noteRoot, nullifier, face, recipient, chainId ] } = Spend(20);
