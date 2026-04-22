pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/bitify.circom";
include "../node_modules/circomlib/circuits/switcher.circom";

// Mint-batch circuit -- Phase 7-bis pivot for BUCK Notes.
//
// Replaces per-leaf on-chain Poseidon insertion with an in-circuit incremental
// Merkle insertion: the contract's only Merkle responsibility shrinks to
// "trust the SNARK-attested newRoot and advance nextLeafIndex".  This makes
// on-chain mint cost essentially constant in N -- one Groth16 verify, three
// SSTOREs, and the calldata to publish cms[].
//
// Public:  oldRoot, newRoot, nextLeafIndex, totalFace, cm[N]
// Private: flavor[N], v[N], rho[N], idHash[N], predicate[N],
//          siblings[N][TREE_DEPTH]
//
// Constraints (per leaf i):
//   (O) cm[i] = Poseidon-5(flavor[i], v[i], rho[i], idHash[i], predicate[i])
//   (R) v[i] in [0, 2^128); totalFace == sum_i v[i] (with totalFace also <2^128)
//   (M) walking ZERO_VALUE up the tree at index (nextLeafIndex+i), using
//       siblings[i][.], reproduces the rolling root *before* this leaf is
//       inserted (oldRoot for i=0, the previous post-insert root for i>0).
//       Walking cm[i] up the tree at the same index/siblings yields the new
//       rolling root, which becomes the prior root for leaf i+1.
//   (F) After all N leaves, the final rolling root === newRoot.
//
// The dual walk per leaf (ZERO_VALUE and cm[i] both folded with the same
// siblings) is what makes the witness siblings non-malleable: a hostile
// prover cannot fabricate sibling values without breaking either the prior
// root constraint (collisions on Poseidon-2) or the cm/totalFace bindings.
//
// We expose cm[N] directly as public inputs rather than a cmBatchHash for
// N<=128.  Each Groth16 public input costs ~6K gas in the EVM verifier; at
// N=16 the additional 16 inputs add ~96K gas to the per-tx verify cost --
// cheaper than recomputing keccak on-chain over the calldata, and *much*
// cheaper than recomputing Poseidon over N elements in-circuit + on-chain.
// Pinned-N>>128 deployments will need to re-introduce a hash binding.

// ---- ZERO_VALUE ------------------------------------------------------------
//
// Mirrored from src/Notes.sol:
//   ZERO_VALUE = uint256(keccak256("AlbertaBuck:Notes:zero")) % FIELD_R
// We harden the constant by computing it in the wallet and asserting on-chain
// equality, but the circuit needs the literal here.  Recompute with:
//   node -e 'console.log((BigInt(require("ethers").keccak256(require("ethers").toUtf8Bytes("AlbertaBuck:Notes:zero"))) % 21888242871839275222246405745257275088548364400416034343698204186575808495617n).toString())'
// -> 12478158023141672556814566805819277863195393802640872128727997243357085450959
function ZERO_VALUE() {
    return 12478158023141672556814566805819277863195393802640872128727997243357085450959;
}

// One Tornado-style insertion step at a single tree level.  Given the rolling
// `cur` value, the path bit (0=left, 1=right), and the sibling provided as
// witness, returns Poseidon([left, right]) per the bit's choice.
template InsertLevel() {
    signal input  cur;
    signal input  bit;       // 0 or 1; constrained by Num2Bits at the leaf
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
// index, producing the resulting root.  The same `siblings[]` are reused for
// the empty-seat walk (preceding the insertion) and the filled-seat walk
// (after the insertion); two MerkleWalk instances per leaf insertion bind
// each sibling value into both the prior and the new rolling root.
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

template MintBatch(N, DEPTH) {
    // ---- public inputs ----
    signal input oldRoot;
    signal input newRoot;
    signal input nextLeafIndex;
    signal input totalFace;
    signal input cm[N];

    // ---- private witness ----
    signal input flavor[N];
    signal input v[N];
    signal input rho[N];
    signal input idHash[N];
    signal input predicate[N];
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

    // (M) Per-leaf dual Merkle walk: bits derived from (nextLeafIndex + i),
    //     "empty" walk (ZERO_VALUE) constrains siblings against the rolling
    //     prior root, "filled" walk (cm[i]) advances the rolling root.

    // Decompose each leaf's path index into TREE_DEPTH bits.
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
        // Empty-seat walk: starting leaf is ZERO_VALUE, walking up with the
        // witness siblings must reproduce the rolling prior root.  This
        // constrains every sibling against the in-flight tree state, which
        // includes leaves 0..i-1 already folded in.
        priorWalk[i] = MerkleWalk(DEPTH);
        priorWalk[i].leaf <== ZERO_VALUE();
        for (var d = 0; d < DEPTH; d++) {
            priorWalk[i].bits[d]     <== idxBits[i].out[d];
            priorWalk[i].siblings[d] <== siblings[i][d];
        }
        priorWalk[i].root === rollingRoot[i];

        // Filled-seat walk: cm[i] up with the same siblings yields the new
        // rolling root for leaf i+1.
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

// Pin N=16 and DEPTH=20.  Re-template per N (larger sizes get their own
// circuit + verifier, dispatched on-chain by `cms.length`).
component main { public [ oldRoot, newRoot, nextLeafIndex, totalFace, cm ] } = MintBatch(32, 20);
