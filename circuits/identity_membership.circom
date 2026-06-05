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
// plaintext for B1.  See alberta-buck-notes-identity-axis.org.

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
    signal input pathElements[depth];     // private: sibling hashes
    signal input pathIndices[depth];      // private: path bits

    // leaf = Poseidon(2)(Mx, My) == identity_leaf(M)
    component leafH = Poseidon(2);
    leafH.inputs[0] <== Mx;
    leafH.inputs[1] <== My;

    component mp = MerkleProof(depth);
    mp.leaf <== leafH.out;
    for (var i = 0; i < depth; i++) {
        mp.pathElements[i] <== pathElements[i];
        mp.pathIndices[i]  <== pathIndices[i];
    }

    // Bind the public root to the folded path.
    identityRoot === mp.root;
}

// Depth pinned to 10 to match the reference IdentityTree(depth=10) used by the
// wallet vectors; production pins this to the registry accumulator's depth.
component main { public [ identityRoot ] } = IdentityMembership(10);
