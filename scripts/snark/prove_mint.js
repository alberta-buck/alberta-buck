#!/usr/bin/env node
/**
 * Generate a mint-circuit proof for a pair of notes, and emit a fixture JSON
 * the forge test can ingest.
 *
 * Output (written to build/snark/mint/fixtures/<name>.json):
 *   {
 *     public: { totalFace, cm: [cm0, cm1] },
 *     witness: { flavor, v, rho, idHash, predicate },
 *     proof:   { pA: [..], pB: [[..]], pC: [..] },
 *     proofBytes: "0x..."   // abi.encode(uint256[2], uint256[2][2], uint256[2])
 *   }
 *
 * `proofBytes` is the exact calldata the IMintVerifier.verifyMint adapter
 * will decode on-chain.  All field elements are emitted as decimal strings
 * (snarkjs convention); forge's vm.parseJsonUint() accepts both decimal and
 * 0x-prefixed hex, so the test-side reader just calls parseJsonUint.
 *
 * Usage:
 *   node scripts/snark/prove_mint.js <name> [<seed>]
 * e.g.
 *   node scripts/snark/prove_mint.js basic 1
 */
"use strict";

const fs   = require("fs");
const path = require("path");
const { buildPoseidon }   = require("circomlibjs");
const snarkjs             = require("snarkjs");
const ethers              = require("ethers");

const ROOT  = path.resolve(__dirname, "..", "..");
const MINT  = path.join(ROOT, "build", "snark", "mint");
const WASM  = path.join(MINT, "mint_js", "mint.wasm");
const ZKEY  = path.join(MINT, "mint_final.zkey");
const FIX   = path.join(MINT, "fixtures");

const NAME = process.argv[2] || "basic";
const SEED = BigInt(process.argv[3] || "1");

// Field modulus of BN254 scalar field.
const R = 21888242871839275222246405745257275088548364400416034343698204186575808495617n;

function fieldFrom(u) {
    let v = BigInt(u) % R;
    if (v < 0n) v += R;
    return v;
}

// Seeded deterministic scalar generator (not cryptographic -- fixture only).
function nextScalar(seedRef) {
    seedRef.s = (seedRef.s * 6364136223846793005n + 1442695040888963407n) % (1n << 64n);
    // widen to 200+ bits so the result is non-trivial after mod-r reduction
    const hi = seedRef.s;
    seedRef.s = (seedRef.s * 6364136223846793005n + 1442695040888963407n) % (1n << 64n);
    const mid = seedRef.s;
    seedRef.s = (seedRef.s * 6364136223846793005n + 1442695040888963407n) % (1n << 64n);
    const lo = seedRef.s;
    return fieldFrom((hi << 128n) ^ (mid << 64n) ^ lo);
}

async function main() {
    fs.mkdirSync(FIX, { recursive: true });
    if (!fs.existsSync(WASM) || !fs.existsSync(ZKEY)) {
        throw new Error(`Missing circuit artifacts; run scripts/snark/setup.sh first`);
    }

    const poseidon = await buildPoseidon();
    const F        = poseidon.F;

    const seed = { s: SEED };
    // Two A2 notes -- flavor = 2.  v[0] = 100e18, v[1] = 25e18, totalFace = 125e18.
    const flavor = [2n, 2n];
    const v      = [100000000000000000000n, 25000000000000000000n];
    const totalFace = v[0] + v[1];
    const rho       = [nextScalar(seed), nextScalar(seed)];
    const idHash    = [nextScalar(seed), nextScalar(seed)];
    const predicate = [0n, 0n];

    // Poseidon(5) commitments, field-native.
    const cm = [];
    for (let i = 0; i < 2; i++) {
        const digest = poseidon([flavor[i], v[i], rho[i], idHash[i], predicate[i]]);
        cm.push(BigInt(F.toString(digest)));
    }

    const input = {
        totalFace: totalFace.toString(),
        cm: cm.map((x) => x.toString()),
        flavor:    flavor.map((x) => x.toString()),
        v:         v.map((x) => x.toString()),
        rho:       rho.map((x) => x.toString()),
        idHash:    idHash.map((x) => x.toString()),
        predicate: predicate.map((x) => x.toString()),
    };

    const { proof, publicSignals } = await snarkjs.groth16.fullProve(input, WASM, ZKEY);

    // publicSignals order matches the `public` list declared in mint.circom's
    // `component main` line: [ totalFace, cm[0], cm[1] ].
    const expectPub = [input.totalFace, input.cm[0], input.cm[1]];
    for (let i = 0; i < 3; i++) {
        if (publicSignals[i] !== expectPub[i]) {
            throw new Error(`publicSignals[${i}] mismatch: ${publicSignals[i]} vs ${expectPub[i]}`);
        }
    }

    const pA = [proof.pi_a[0], proof.pi_a[1]];
    // snarkjs emits pi_b with (x1, x0, y1, y0) -- the pairing precompile and
    // the snarkjs Solidity template expect the Fp2 limbs in reversed order.
    // The generated verifier handles this swap internally; the a/b/c the
    // verifier expects as calldata match proof.pi_* 1:1 when serialized in
    // this [[b[0][1], b[0][0]], [b[1][1], b[1][0]]] convention.
    const pB = [[proof.pi_b[0][1], proof.pi_b[0][0]],
                [proof.pi_b[1][1], proof.pi_b[1][0]]];
    const pC = [proof.pi_c[0], proof.pi_c[1]];

    const coder = ethers.AbiCoder.defaultAbiCoder();
    const proofBytes = coder.encode(
        ["uint256[2]", "uint256[2][2]", "uint256[2]"],
        [pA, pB, pC],
    );

    const fixture = {
        public:  { totalFace: input.totalFace, cm: input.cm },
        witness: input,
        proof:   { pA, pB, pC },
        proofBytes,
    };

    const outPath = path.join(FIX, `${NAME}.json`);
    fs.writeFileSync(outPath, JSON.stringify(fixture, null, 2));
    console.log(`wrote ${outPath}`);
    console.log(`  totalFace = ${input.totalFace}`);
    console.log(`  cm[0]     = ${input.cm[0]}`);
    console.log(`  cm[1]     = ${input.cm[1]}`);

    // snarkjs's own verify as a sanity check before we ship the fixture.
    const vk = JSON.parse(fs.readFileSync(path.join(MINT, "verification_key.json"), "utf8"));
    const ok = await snarkjs.groth16.verify(vk, publicSignals, proof);
    if (!ok) throw new Error("snarkjs verify failed on own proof");
    console.log("  snarkjs verify: OK");
}

main().then(() => process.exit(0)).catch((e) => {
    console.error(e);
    process.exit(1);
});
