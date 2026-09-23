pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "./leaf_tags.circom";

// Identity-membership circuit -- the native Poseidon-Merkle half of the unified
// Notes membership gate (the A2 deposit coupling and the B1 depositor binding
// both require "the counterparty Identity M is a registered identity").
//
// Proves that a registered Identity point M = (Mx, My) is a member of the
// registry-Identity accumulator under a public root, WITHOUT revealing M:
//
//     leaf = Poseidon(4)(TAG, Mx, My, salt)    // == identity_leaf_salted(M, salt)
//     fold leaf up the authentication path      // == IdentityTree.verify_path
//     identityRoot === computed root
//
// The leaf and node hashing match alberta_buck.wallet.unilateral_a2.IdentityTree
// byte-for-byte (circomlib Poseidon == the wallet's poseidon over BN254; Mx,My
// are the affine coordinates reduced mod the scalar field, as identity_leaf does).
// The switcher bit convention matches IdentityTree.verify_path: pathIndices[i]=0
// puts the running node on the LEFT (Poseidon(cur, sib)), =1 on the RIGHT.
//
// What this pins: the membership itself, in zero knowledge.  What remains (the
// one non-native G1 relation) is the tie of (Mx,My) to the point committed in
// the on-chain EIP-196 sigma -- P_I = M + b*H for A2, or E_dep_for_iss's
// plaintext for B1.  See alberta-buck-notes.org and alberta-buck-notes-flow.org (the identity-axis / one-gadget unification and accumulator).

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

template IdentityMembership(depth) {
    signal input identityRoot;            // public: registry-Identity accumulator root
    signal input Mx;                      // private: M.x mod field
    signal input My;                      // private: M.y mod field
    signal input salt;                    // private: this holder's blinding for THIS subtree
    signal input pathElements[depth];     // private: sibling hashes
    signal input pathIndices[depth];      // private: path bits

    // leaf = Poseidon(4)(TAG, Mx, My, salt) == identity_leaf_salted(M, salt).
    //
    // The leaf of a PRIVATE subtree.  An unsalted Poseidon(2)(Mx, My) would be
    // a deterministic function of the identity, so any party holding a set of
    // identity scalars -- a registry holds every scalar it ever certified --
    // could decide membership of the published subtree by recomputing leaves.
    // The salt is the holder's, derived from a wallet secret and never from
    // the identity, so an authority that learns one salt cannot derive
    // another (accumulator specification, sections 3, 4 and 7).
    //
    // PUBLIC subtrees -- a regulator's insurers, whose membership they
    // advertise -- keep the unsalted leaf and need no circuit at all: their
    // paths verify as plain Poseidon Merkle proofs.
    component leafH = Poseidon(4);
    leafH.inputs[0] <== LEAF_TAG_IDENTITY_SALTED();
    leafH.inputs[1] <== Mx;
    leafH.inputs[2] <== My;
    leafH.inputs[3] <== salt;

    component mp = MerkleProof(depth);
    mp.leaf <== leafH.out;
    for (var i = 0; i < depth; i++) {
        mp.pathElements[i] <== pathElements[i];
        mp.pathIndices[i]  <== pathIndices[i];
    }

    // Bind the public root to the folded path.
    identityRoot === mp.root;
}

// 32 levels: the leaf's identity-registry subtree (12, KYC_SUBTREE_DEPTH) and
// then the aggregator (20, AGGREGATOR_DEPTH), whose root is the on-chain
// `identityRoot`.  Every level folds with the same Poseidon-2, so the two
// paths are one path here (accumulator specification, section 11.1).  Named
// elsewhere as:
//   Solidity  IdentityRegistry.MEMBERSHIP_PATH_DEPTH
//   Python    alberta_buck.registry.merkle_service.MEMBERSHIP_PATH_DEPTH
//
// Left as a literal on purpose: the depth is baked into the r1cs, so any
// change here is a new circuit, and should read as one.
component main { public [ identityRoot ] } = IdentityMembership(32);
