pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/bitify.circom";
include "../node_modules/circomlib/circuits/switcher.circom";

// A-flavor spend circuit (Phase 8) for BUCK Notes.
//
// Structurally identical to spend.circom (B-spend), with three A-specific
// differentiators:
//
//   (F-A) flavor must be A1 (=1) or A2 (=2): (flavor-1)(flavor-2) === 0.
//         The B-spend circuit leaves flavor unconstrained, which means a
//         spender can pass any flavor including A1/A2.  Without this gate
//         here the same A-flavor opening would be acceptable to either the
//         A or B verifier and the on-chain dispatch by tag would be the
//         only flavor distinction; with it, A-spend cryptographically
//         excludes B-flavor openings.
//   (N)   nullifier tag = 4243 (vs 4242 for B), so the nullifier preimage
//         space is disjoint from the B-spend's; the on-chain `nullifiers`
//         mapping can be shared without flavor-collision risk.
//   (I)   V2 only: idHash === Poseidon-8(eNoteR.x, eNoteR.y, eNoteC.x,
//         eNoteC.y, issuerData[0..3]) -- binds the publicly-revealed note
//         ciphertext (eNoteR, eNoteC) to the leaf commitment via idHash.
//         issuerData is supplied by the prover and matches the flavor's
//         payload tail (A1: (m_issuer, sigma_R.x, sigma_R.y, sigma_s);
//         A2: (R_iss.x, R_iss.y, C_iss.x, C_iss.y)).  See
//         alberta_buck.wallet.notes.id_hash_a1/a2.
//
// V1 (predicate-only) deliberately *omitted* the cryptographic identity
// gate -- that constraint requires non-native BN254 G1 arithmetic in-circuit
// and would have multiplied constraint count ~10x.  V2 ships the identity
// gate as an OFF-CHAIN Chaum-Pedersen DLEQ proof verified by Solidity using
// the EIP-196 BN254 precompiles inside `IdentityRegistry.verifySpendCP`,
// called from `Notes.spend` (NOT inside the SNARK).  The role of this
// circuit's V2 constraints (I) is purely to *bind* the publicly-revealed
// note ciphertext to the spent leaf so the on-chain CP verifier and the
// SNARK agree on which ciphertext is in scope.  Cost of (I): ~265 R1CS for
// one Poseidon-8, well within the existing pot15 ptau.
//
// Public:  noteRoot, nullifier, face, recipient, chainId,
//          eNoteRx, eNoteRy, eNoteCx, eNoteCy
// Private: flavor, v, rho, idHash, predicate, issuerData[4],
//          pathElements[depth], pathIndices[depth]

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

template SpendA(depth) {
    // ---- public inputs ----
    signal input noteRoot;
    signal input nullifier;
    signal input face;
    signal input recipient;
    signal input chainId;
    // V2: ciphertext that the on-chain CP-DLEQ verifier will match against
    // the spender's registered E_addr.  Bound to the leaf via constraint (I).
    signal input eNoteRx;
    signal input eNoteRy;
    signal input eNoteCx;
    signal input eNoteCy;

    // ---- private witness ----
    signal input flavor;
    signal input v;
    signal input rho;
    signal input idHash;
    signal input predicate;
    // V2: the 4-tuple completing the id_hash payload past the (R_n, C_n)
    // public prefix.  A1: (m_issuer, sigma_R.x, sigma_R.y, sigma_s).
    // A2: (R_iss.x, R_iss.y, C_iss.x, C_iss.y).
    signal input issuerData[4];
    signal input pathElements[depth];
    signal input pathIndices[depth];

    // (F-A) Flavor must be A1 (1) or A2 (2).  Quadratic root-form constraint.
    (flavor - 1) * (flavor - 2) === 0;

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

    // (N) Nullifier = Poseidon-3 of (rho, idHash, 4243).  The 4243 tag
    //     domain-separates A-spend nullifiers from B-spend (4242), so the
    //     same on-chain nullifiers mapping serves both flavors with no
    //     cross-flavor collision.
    component nf = Poseidon(3);
    nf.inputs[0] <== rho;
    nf.inputs[1] <== idHash;
    nf.inputs[2] <== 4243;
    nullifier === nf.out;

    // (I) V2 binding: idHash === Poseidon-8 over the public ciphertext
    //     prefix (4 words) and the issuerData tail (4 words).  Mirrors
    //     alberta_buck.wallet.notes.id_hash_a1/a2 byte-for-byte (each input
    //     auto-reduces mod F_R in the field).  Forces the publicly-revealed
    //     E_n to match the one bound in the leaf at mint time -- a spender
    //     who substitutes a different ciphertext (to satisfy a CP-DLEQ they
    //     can satisfy with their own key) would fail this constraint.
    component idH = Poseidon(8);
    idH.inputs[0] <== eNoteRx;
    idH.inputs[1] <== eNoteRy;
    idH.inputs[2] <== eNoteCx;
    idH.inputs[3] <== eNoteCy;
    idH.inputs[4] <== issuerData[0];
    idH.inputs[5] <== issuerData[1];
    idH.inputs[6] <== issuerData[2];
    idH.inputs[7] <== issuerData[3];
    idH.out === idHash;

    // (B) Ghost-bind recipient and chainId so they participate in R1CS
    //     constraints (otherwise circom's optimizer may drop unreferenced
    //     public signals, which would silently widen the spend surface).
    signal recipBound;  recipBound <== recipient * recipient;
    signal chainBound;  chainBound <== chainId   * chainId;
}

// Tree depth pinned to 20 == Notes.TREE_DEPTH.
component main { public [
    noteRoot, nullifier, face, recipient, chainId,
    eNoteRx, eNoteRy, eNoteCx, eNoteCy
] } = SpendA(20);
