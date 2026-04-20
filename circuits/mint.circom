pragma circom 2.1.4;

include "../node_modules/circomlib/circuits/poseidon.circom";

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
//   (1) sum_i v[i] == totalFace
//   (2) cm[i] == Poseidon([flavor[i], v[i], rho[i], id_hash[i], predicate[i]])
template Mint(N) {
    signal input  totalFace;
    signal input  cm[N];

    signal input  flavor[N];
    signal input  v[N];
    signal input  rho[N];
    signal input  idHash[N];
    signal input  predicate[N];

    component H[N];
    var running = 0;
    for (var i = 0; i < N; i++) {
        H[i] = Poseidon(5);
        H[i].inputs[0] <== flavor[i];
        H[i].inputs[1] <== v[i];
        H[i].inputs[2] <== rho[i];
        H[i].inputs[3] <== idHash[i];
        H[i].inputs[4] <== predicate[i];
        cm[i] === H[i].out;
        running += v[i];
    }
    totalFace === running;
}

// The on-chain Notes contract currently pins N = 2 per mint batch (see
// MintVerifierAdapter.sol); a larger N re-templates and redeploys.
component main { public [ totalFace, cm ] } = Mint(2);
