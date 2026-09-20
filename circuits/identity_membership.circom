pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/switcher.circom";

// Identity-membership circuit -- the native Poseidon-Merkle half of the unified
// Notes membership gate (the A2 deposit coupling and the B1 depositor binding
// both require "the counterparty Identity M is a registered identity").
//
// Proves that a registered Identity point M = (Mx, My) is a member of the
// registry-Identity accumulator under a public root, WITHOUT revealing M:
//
//     leaf = Poseidon(2)(Mx, My)               // == identity_leaf(M)
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

    // leaf = Poseidon(3)(Mx, My, salt) == identity_leaf_salted(M, salt).
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
    component leafH = Poseidon(3);
    leafH.inputs[0] <== Mx;
    leafH.inputs[1] <== My;
    leafH.inputs[2] <== salt;

    component mp = MerkleProof(depth);
    mp.leaf <== leafH.out;
    for (var i = 0; i < depth; i++) {
        mp.pathElements[i] <== pathElements[i];
        mp.pathIndices[i]  <== pathIndices[i];
    }

    // Bind the public root to the folded path.
    identityRoot === mp.root;
}

// Depth is the AGGREGATOR depth.  The accumulator specification raises it
// from 10 to 20, so that authorities are a population rather than a roster:
// clubs, community boards, congregations and delegated sub-regulators are all
// attribute authorities, and 2**10 = 1024 sub-trees is the wrong order of
// magnitude.  Ten extra levels cost ten Poseidon-2 hashes here and about two
// thousand constraints.
//
// It is still 10 because raising it re-folds every identity root, including
// the one committed in the Notes end-to-end fixtures, and regenerating those
// runs the note-binding prover.  The finding-5 circuit repairs force that
// same regeneration, so the depth rises with them rather than paying for it
// twice.  IdentityRegistry already carries ZERO_11..ZERO_20, so the contract
// side is ready.
// The 10 here is the AGGREGATOR depth -- the tree whose root is the
// on-chain `identityRoot`.  Named elsewhere as:
//   Solidity  IdentityRegistry.IDENTITY_TREE_DEPTH
//   Rust      buck_registry::tree::AGGREGATOR_DEPTH
//   Python    alberta_buck.registry.tree.AGGREGATOR_DEPTH
// It is NOT the registry sub-tree depth (12, KYC_SUBTREE_DEPTH); an
// organization's own tree is deeper and composes into this one.
//
// Left as a literal on purpose: a compile-time `var` would very likely
// produce identical R1CS, but "very likely" is not worth it here --
// any change to this file forces a fresh trusted setup, hence a new
// zkey, a new committed verifier and regenerated proof vectors, which
// the Makefile calls a MATCHED SET from a single run.
component main { public [ identityRoot ] } = IdentityMembership(10);
