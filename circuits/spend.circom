pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/bitify.circom";
include "../node_modules/circomlib/circuits/switcher.circom";

// Spend circuit -- Phase 7 spend shape for BUCK Notes.
//
// Proves that the prover holds a Poseidon-5 opening of *some* commitment
// included in the on-chain Merkle accumulator under a recent root, and that
// the public `face` matches the witness `v` and the public `nullifier` is
// the prescribed Poseidon-3 derivation from the witness.
//
// Flavor is a PUBLIC input equal to the committed Poseidon-5 word.  Each
// Notes.spendCoupled* entry point supplies its mode as a constant
// (A1=1, A2=2, B1=3), so an A-flavor opening cannot verify through the
// B1 path even with a well-formed membership proof.  The bearer/B-spend
// semantics remain: knowledge of the opening *is* the spend authorization
// once the flavor matches.  Addressed (A1/A2) recipient binding is still
// the deposit-coupling sigma + note-binding SNARK, not this circuit.
//
// Public:  noteRoot, nullifier, face, recipient, chainId, flavor,
//          issuanceCommitment
// Private: v, rho, idHash, predicate,
//          pathElements[depth], pathIndices[depth]
//
// Constraints:
//   (R)   face in [0, 2^128) and v in [0, 2^128); face == v
//   (F)   flavor in {1,2,3} (A1/A2/B1); the same signal is public and
//         the first Poseidon-5 word, so entry-point mode === committed flavor
//   (P)   predicate === 0 (no supported spend predicates yet)
//   (C)   cm = Poseidon([flavor, v, rho, idHash, predicate])
//   (I)   issuanceCommitment = cm for B1, else 0.  Notes records each
//         public-mint cm under the authenticated mint issuer, so the B1 spend
//         can bind its caller-supplied issuer to this exact committed note.
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
    signal input flavor;          // A1=1, A2=2, B1=3; bound to the commitment
    signal input issuanceCommitment; // cm for B1; zero for addressed flavors

    // ---- private witness ----
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

    // (F) flavor in {1,2,3}.  Public `flavor` is also the Poseidon-5 word,
    //     so Notes.spendCoupledB1(3) cannot verify an A1/A2 opening.
    signal flavorPair;
    flavorPair <== (flavor - 1) * (flavor - 2);
    flavorPair * (flavor - 3) === 0;

    // (P) No spend predicate is implemented; reject a nonzero committed word
    //     rather than treating it as an unconstrained private input.
    predicate === 0;

    // (C) Recompute the leaf commitment from the witness opening.
    component cm = Poseidon(5);
    cm.inputs[0] <== flavor;
    cm.inputs[1] <== v;
    cm.inputs[2] <== rho;
    cm.inputs[3] <== idHash;
    cm.inputs[4] <== predicate;

    // (I) Reveal the exact mint-authenticated commitment only for the bearer
    //     flavor.  The flavor constraint above makes b1Selector exactly 0 for
    //     A1/A2 and 1 for B1; division by two is expressed as multiplication
    //     to avoid relying on a host-language field constant.
    signal b1Selector;
    // 1/2 in the BN254 scalar field.
    b1Selector <== (flavor - 1) * (flavor - 2)
        * 10944121435919637611123202872628637544274182200208017171849102093287904247809;
    issuanceCommitment === b1Selector * cm.out;

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
component main { public [ noteRoot, nullifier, face, recipient, chainId, flavor, issuanceCommitment ] } = Spend(20);
