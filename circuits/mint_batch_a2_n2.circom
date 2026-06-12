pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/bitify.circom";
include "../node_modules/circomlib/circuits/switcher.circom";

// A2 mint-batch circuit -- the private-issuer (addressed, encrypted-Identity)
// variant of mint_batch.circom.  It is used *only* by Notes.mint's private-mode
// (A2) path; public / bearer batches keep using mint_batch (unchanged), so they
// pay nothing for the A2 machinery.
//
// Difference from mint_batch: every leaf is constrained to flavor == A2, the
// committed idHash is opened to Poseidon-8(eNote, eIss) (the V2 A2 id_hash
// layout, mirroring spend_a.circom and alberta_buck.wallet.notes.id_hash_a2),
// and each leaf's E_iss-for-rec ciphertext eIss = (R.x, R.y, C.x, C.y) is
// exposed as a PUBLIC OUTPUT.  Notes.mint field-matches each exposed eIss
// against the eIss in the leaf's recipient-blinded re-encryption binding, so a
// binding cannot float to a different leaf -- the collusion-resistant A2 tie
// (alberta-buck-notes-decryptability.org, The Required Mint SNARK Signal).
//
// The note ciphertext eNote stays a PRIVATE witness: it is the value the A-spend
// later reveals, so keeping it out of the mint proof preserves mint<->spend
// unlinkability.  Exposing eIss is not a new disclosure -- the A2 binding
// already carries eIss on chain at mint today.
//
// Public:  eIss[N][4] (outputs), oldRoot, newRoot, nextLeafIndex, totalFace, cm[N]
// Private: flavor[N], v[N], rho[N], idHash[N], predicate[N],
//          eNote[N][4], eIssW[N][4], siblings[N][TREE_DEPTH]

// ---- ZERO_VALUE (mirrored from src/Notes.sol; see mint_batch.circom) -------
function ZERO_VALUE() {
    return 12478158023141672556814566805819277863195393802640872128727997243357085450959;
}

// One Tornado-style insertion step at a single tree level.
template InsertLevel() {
    signal input  cur;
    signal input  bit;
    signal input  sibling;
    signal output out;

    component sw = Switcher();
    sw.sel <== bit;
    sw.L   <== cur;
    sw.R   <== sibling;

    component h = Poseidon(2);
    h.inputs[0] <== sw.outL;
    h.inputs[1] <== sw.outR;
    out <== h.out;
}

// Walk a leaf up `depth` levels using witness siblings and a bit-decomposed
// index, producing the resulting root.
template MerkleWalk(depth) {
    signal input  leaf;
    signal input  bits[depth];
    signal input  siblings[depth];
    signal output root;

    component step[depth];
    signal levels[depth + 1];
    levels[0] <== leaf;
    for (var i = 0; i < depth; i++) {
        step[i] = InsertLevel();
        step[i].cur     <== levels[i];
        step[i].bit     <== bits[i];
        step[i].sibling <== siblings[i];
        levels[i + 1] <== step[i].out;
    }
    root <== levels[depth];
}

template MintBatchA2(N, DEPTH) {
    // ---- public inputs ----
    signal input oldRoot;
    signal input newRoot;
    signal input nextLeafIndex;
    signal input totalFace;
    signal input cm[N];

    // ---- public outputs ----
    // Per-leaf E_iss-for-rec ciphertext (R.x, R.y, C.x, C.y).
    signal output eIss[N][4];

    // ---- private witness ----
    signal input flavor[N];
    signal input v[N];
    signal input rho[N];
    signal input idHash[N];
    signal input predicate[N];
    signal input eNote[N][4];     // note ciphertext (R.x,R.y,C.x,C.y) -- PRIVATE
    signal input eIssW[N][4];     // E_iss-for-rec words (exposed via eIss)
    signal input siblings[N][DEPTH];

    // (O) Poseidon-5 commitment opening per leaf.
    component cmH[N];
    for (var i = 0; i < N; i++) {
        cmH[i] = Poseidon(5);
        cmH[i].inputs[0] <== flavor[i];
        cmH[i].inputs[1] <== v[i];
        cmH[i].inputs[2] <== rho[i];
        cmH[i].inputs[3] <== idHash[i];
        cmH[i].inputs[4] <== predicate[i];
        cm[i] === cmH[i].out;
    }

    // (A2) flavor === A2, and idHash opens to Poseidon-8(eNote, eIss).  Expose
    //      eIss; eNote stays private.
    component idH[N];
    for (var i = 0; i < N; i++) {
        flavor[i] === 2;            // FLAVOR_A2 (mirror wallet/Notes labels)

        idH[i] = Poseidon(8);
        idH[i].inputs[0] <== eNote[i][0];
        idH[i].inputs[1] <== eNote[i][1];
        idH[i].inputs[2] <== eNote[i][2];
        idH[i].inputs[3] <== eNote[i][3];
        idH[i].inputs[4] <== eIssW[i][0];
        idH[i].inputs[5] <== eIssW[i][1];
        idH[i].inputs[6] <== eIssW[i][2];
        idH[i].inputs[7] <== eIssW[i][3];
        idHash[i] === idH[i].out;

        eIss[i][0] <== eIssW[i][0];
        eIss[i][1] <== eIssW[i][1];
        eIss[i][2] <== eIssW[i][2];
        eIss[i][3] <== eIssW[i][3];
    }

    // (R) Range-bound totalFace and each v[i] to 128 bits.
    component totalRange = Num2Bits(128);
    totalRange.in <== totalFace;
    component vRange[N];
    var sumV = 0;
    for (var i = 0; i < N; i++) {
        vRange[i] = Num2Bits(128);
        vRange[i].in <== v[i];
        sumV += v[i];
    }
    totalFace === sumV;

    // (M) Per-leaf dual Merkle walk (empty-seat + filled-seat), identical to
    //     mint_batch.
    component idxBits[N];
    for (var i = 0; i < N; i++) {
        idxBits[i] = Num2Bits(DEPTH);
        idxBits[i].in <== nextLeafIndex + i;
    }

    component priorWalk[N];
    component postWalk[N];
    signal rollingRoot[N + 1];
    rollingRoot[0] <== oldRoot;
    for (var i = 0; i < N; i++) {
        priorWalk[i] = MerkleWalk(DEPTH);
        priorWalk[i].leaf <== ZERO_VALUE();
        for (var d = 0; d < DEPTH; d++) {
            priorWalk[i].bits[d]     <== idxBits[i].out[d];
            priorWalk[i].siblings[d] <== siblings[i][d];
        }
        priorWalk[i].root === rollingRoot[i];

        postWalk[i] = MerkleWalk(DEPTH);
        postWalk[i].leaf <== cm[i];
        for (var d = 0; d < DEPTH; d++) {
            postWalk[i].bits[d]     <== idxBits[i].out[d];
            postWalk[i].siblings[d] <== siblings[i][d];
        }
        rollingRoot[i + 1] <== postWalk[i].root;
    }

    // (F) Final rolling root must equal the public newRoot.
    rollingRoot[N] === newRoot;
}

// Pin N=16, DEPTH=20.  Re-templated per N by scripts/snark/setup.sh.
component main { public [ oldRoot, newRoot, nextLeafIndex, totalFace, cm ] } = MintBatchA2(2, 20);
