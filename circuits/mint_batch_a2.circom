pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/bitify.circom";
include "../node_modules/circomlib/circuits/switcher.circom";
include "./note_tags.circom";

// A2 mint-batch circuit -- the private-issuer (addressed, encrypted-Identity)
// variant of mint_batch.circom.  It is used *only* by Notes.mint's private-mode
// (A2) path; public / bearer batches keep using mint_batch (unchanged), so they
// pay nothing for the A2 machinery.
//
// Difference from mint_batch: every leaf is constrained to flavor == A2, the
// committed idHash is opened to Poseidon-11(T_ID, eNote, eIss, T) (mirroring
// alberta_buck.wallet.notes.id_hash_a2), and each leaf's E_iss-for-rec
// ciphertext eIss = (R.x, R.y, C.x, C.y) and its binding's T = (x, y) are
// exposed as PUBLIC OUTPUTS.  Notes.mint field-matches each exposed pair
// against the leaf's recipient-blinded re-encryption binding, so a binding
// cannot float to a different leaf (alberta-buck-notes.org, "The
// Non-Deniable-Receipt Invariant").
//
// Why T.  The binding proves eIss carries the minter's registered Identity to
// the key hidden in its Q -- but an ElGamal ciphertext does not bind its
// plaintext to one key, so that key need not be the recipient's.  The spend can
// reach the mint only through idHash, so idHash commits T = r'*pk + gamma*H,
// and the A2 fold proves T opens under the spender's own key
// (doc/review/notes-receiving-key.org, section 4.6).
//
// The note ciphertext eNote stays a PRIVATE witness: it is the value the spend
// later reveals, so keeping it out of the mint proof preserves mint<->spend
// unlinkability.  Exposing eIss and T is not a new disclosure -- the A2 binding
// carries both on chain at mint.
//
// Public:  eIss[N][4], T[N][2] (outputs), oldRoot, newRoot, nextLeafIndex, totalFace, cm[N]
// Private: flavor[N], v[N], rho[N], idHash[N], predicate[N],
//          eNote[N][4], eIssW[N][4], TW[N][2], siblings[N][TREE_DEPTH]

// ---- ZERO_VALUE (mirrored from src/Notes.sol; see mint_batch.circom) -------
function ZERO_VALUE() {
    return 460097596457234765974707969191747880107513410278794739541636231580225950866;
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
    // Per-leaf E_iss-for-rec ciphertext (R.x, R.y, C.x, C.y), and the
    // binding's T (x, y).
    signal output eIss[N][4];
    signal output T[N][2];

    // ---- private witness ----
    signal input flavor[N];
    signal input v[N];
    signal input rho[N];
    signal input idHash[N];
    signal input predicate[N];
    signal input eNote[N][4];     // note ciphertext (R.x,R.y,C.x,C.y) -- PRIVATE
    signal input eIssW[N][4];     // E_iss-for-rec words (exposed via eIss)
    signal input TW[N][2];        // the binding's T words (exposed via T)
    signal input siblings[N][DEPTH];

    // (O) Tagged Poseidon-6 commitment opening per leaf.
    component cmH[N];
    for (var i = 0; i < N; i++) {
        cmH[i] = Poseidon(6);
        cmH[i].inputs[0] <== NOTE_TAG_COMMITMENT();
        cmH[i].inputs[1] <== flavor[i];
        cmH[i].inputs[2] <== v[i];
        cmH[i].inputs[3] <== rho[i];
        cmH[i].inputs[4] <== idHash[i];
        cmH[i].inputs[5] <== predicate[i];
        cm[i] === cmH[i].out;
    }

    // (A2) flavor === A2, and idHash opens to Poseidon-11(T_ID, eNote, eIss, T).
    //      Expose eIss and T; eNote stays private.
    component idH[N];
    for (var i = 0; i < N; i++) {
        flavor[i] === 2;            // FLAVOR_A2 (mirror wallet/Notes labels)

        idH[i] = Poseidon(11);
        idH[i].inputs[0] <== NOTE_TAG_ID_HASH();
        idH[i].inputs[1] <== eNote[i][0];
        idH[i].inputs[2] <== eNote[i][1];
        idH[i].inputs[3] <== eNote[i][2];
        idH[i].inputs[4] <== eNote[i][3];
        idH[i].inputs[5] <== eIssW[i][0];
        idH[i].inputs[6] <== eIssW[i][1];
        idH[i].inputs[7] <== eIssW[i][2];
        idH[i].inputs[8] <== eIssW[i][3];
        idH[i].inputs[9] <== TW[i][0];
        idH[i].inputs[10] <== TW[i][1];
        idHash[i] === idH[i].out;

        eIss[i][0] <== eIssW[i][0];
        eIss[i][1] <== eIssW[i][1];
        eIss[i][2] <== eIssW[i][2];
        eIss[i][3] <== eIssW[i][3];
        T[i][0] <== TW[i][0];
        T[i][1] <== TW[i][1];
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
component main { public [ oldRoot, newRoot, nextLeafIndex, totalFace, cm ] } = MintBatchA2(16, 20);
