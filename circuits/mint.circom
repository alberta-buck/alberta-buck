pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";
include "../node_modules/circomlib/circuits/bitify.circom";

// Mint circuit -- Phase 2 minimum viable shape for BUCK Notes.
//
// Proves that each public commitment cm[i] opens (under a Poseidon hash) to a
// private witness (flavor, v, rho, id_hash, predicate), and that the v[i]
// sum equals the public totalFace.  The id-payload is collapsed to a single
// field element `id_hash`; flavor-specific witness verification (Schnorr for
// A1/B1, ElGamal re-randomization well-formedness for A2) is deferred to a
// later revision of this circuit.  The Notes contract's stub verifier is
// replaced by the Solidity verifier auto-generated from this circuit.
//
// Public:  totalFace, cm[N]
// Private: flavor[N], v[N], rho[N], id_hash[N], predicate[N]
//
// Constraints:
//   (1) v[i] in [0, 2^128) for each i (Num2Bits range bound)
//   (2) totalFace in [0, 2^128) (sum cannot wrap mod r either)
//   (3) sum_i v[i] == totalFace
//   (4) cm[i] == Poseidon([flavor[i], v[i], rho[i], id_hash[i], predicate[i]])
//
// (1)+(2) close Open Question 1 (alberta-buck-proofs.org Part IV): without
// per-v range bounds the sum constraint holds only modulo r, so an adversarial
// witness with v_k = totalFace + r would satisfy (3) and let a future spend
// release totalFace + r BUCK against it.  128 bits is enough to fit any BUCK
// face value (max supply << 2^128 wei, BUCK is 18-decimal so 2^128 wei is
// ~3.4e20 BUCK -- comfortably above any realistic mint).  Range-bounding
// totalFace as well prevents an adversary from picking a public input near r.
template Mint(N) {
    signal input  totalFace;
    signal input  cm[N];

    signal input  flavor[N];
    signal input  v[N];
    signal input  rho[N];
    signal input  idHash[N];
    signal input  predicate[N];

    // Range-bound totalFace and each v[i] to 128 bits.  Num2Bits enforces
    // strict little-endian bit decomposition; if the value exceeds 2^128 - 1
    // the witness is unsatisfiable.
    component totalRange = Num2Bits(128);
    totalRange.in <== totalFace;

    component vRange[N];
    component H[N];
    var running = 0;
    for (var i = 0; i < N; i++) {
        vRange[i] = Num2Bits(128);
        vRange[i].in <== v[i];

        H[i] = Poseidon(5);
        H[i].inputs[0] <== flavor[i];
        H[i].inputs[1] <== v[i];
        H[i].inputs[2] <== rho[i];
        H[i].inputs[3] <== idHash[i];
        H[i].inputs[4] <== predicate[i];
        cm[i] === H[i].out;
        running += v[i];
    }
    // sum of N values each < 2^128 fits in 2^128 + log2(N) bits << r, so the
    // equality below holds in integers as well as in F_r.
    totalFace === running;
}

// The on-chain Notes contract currently pins N = 2 per mint batch (see
// MintVerifierAdapter.sol); a larger N re-templates and redeploys.
component main { public [ totalFace, cm ] } = Mint(2);
