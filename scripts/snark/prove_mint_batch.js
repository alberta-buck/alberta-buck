#!/usr/bin/env node
/**
 * Generate a mint_batch proof for an N-leaf batch and emit a fixture JSON
 * the forge test can ingest.
 *
 * Output (written to build/snark/mint_batch_n${N}/fixtures/<name>.json):
 *   {
 *     N, depth,
 *     public: {
 *       oldRoot, newRoot, nextLeafIndex, totalFace,
 *       cm: [cm_0, ..., cm_{N-1}]
 *     },
 *     witness: { flavor, v, rho, idHash, predicate, siblings },
 *     proof:   { pA: [..], pB: [[..]], pC: [..] },
 *     proofBytes: "0x..."   // abi.encode(uint256[2], uint256[2][2], uint256[2])
 *   }
 *
 * Inputs (CLI):
 *   prove_mint_batch.js [--name=<name>] [--n=<N>] [--seed=<seed>]
 *                       [--start-leaf=<idx>] [--initial-state=<path>]
 *                       [--live-leaves=<v0,v1,...>]
 *
 *   --name           Fixture filename (default "basic")
 *   --n              Batch size; must match a deployed mint_batch_n${N} circuit
 *   --seed           PRNG seed for rho/idHash (default 1)
 *   --start-leaf     nextLeafIndex the batch is being inserted at (default 0)
 *   --initial-state  JSON file holding {filledSubtrees, oldRoot, nextLeafIndex}
 *                    that the wallet would normally maintain.  If omitted we
 *                    bootstrap from the empty tree at startLeaf=0.
 *   --live-leaves    Comma-separated face values (in BUCK wei) per leaf in the
 *                    batch.  Defaults to a deterministic v[i] = (i+1)*1e18 so
 *                    totalFace = N*(N+1)/2 * 1e18.
 *
 * The "wallet's local tree mirror" is implemented inline: we walk filled-
 * subtrees through every batch leaf, recording the sibling each step needs
 * (which is what the circuit will fold).  The mirror state at the end is
 * NOT persisted -- this script is a fixture generator, not a wallet.
 */
"use strict";

const fs   = require("fs");
const path = require("path");
const { buildPoseidon } = require("circomlibjs");
const snarkjs           = require("snarkjs");
const ethers            = require("ethers");

// ---- constants -----------------------------------------------------------

const ROOT  = path.resolve(__dirname, "..", "..");
const DEPTH = 20;
const FIELD_R = 21888242871839275222246405745257275088548364400416034343698204186575808495617n;

// Mirror Notes.sol's keccak("AlbertaBuck:Notes:zero") % FIELD_R.
const ZERO_VALUE = (() => {
    const k = ethers.keccak256(ethers.toUtf8Bytes("AlbertaBuck:Notes:zero"));
    return BigInt(k) % FIELD_R;
})();

// ---- helpers -------------------------------------------------------------

function fieldFrom(u) {
    let v = BigInt(u) % FIELD_R;
    if (v < 0n) v += FIELD_R;
    return v;
}

// Seeded deterministic scalar generator (not cryptographic -- fixture only).
function mkRng(seed) {
    let s = BigInt(seed);
    return () => {
        s = (s * 6364136223846793005n + 1442695040888963407n) % (1n << 64n);
        const hi = s;
        s = (s * 6364136223846793005n + 1442695040888963407n) % (1n << 64n);
        const mid = s;
        s = (s * 6364136223846793005n + 1442695040888963407n) % (1n << 64n);
        const lo = s;
        return fieldFrom((hi << 128n) ^ (mid << 64n) ^ lo);
    };
}

function parseArgs() {
    const out = {
        name: "basic",
        n: 16,
        seed: 1n,
        startLeaf: 0n,
        initialState: null,
        liveLeaves: null,
        pin: {},
    };
    for (const a of process.argv.slice(2)) {
        const m = a.match(/^--([^=]+)=(.*)$/);
        if (!m) throw new Error(`bad arg: ${a}`);
        const [_, k, v] = m;
        switch (k) {
            case "name": out.name = v; break;
            case "n": out.n = parseInt(v, 10); break;
            case "seed": out.seed = BigInt(v); break;
            case "start-leaf": out.startLeaf = BigInt(v); break;
            case "initial-state": out.initialState = v; break;
            case "live-leaves":
                out.liveLeaves = v.split(",").map((s) => BigInt(s));
                break;
            case "pin": {
                // --pin=i:flavor,v,rho,idHash — pin leaf i's FULL opening to a
                // wallet-generated note (e2e fixtures), overriding the seeded
                // RNG.  Repeatable.
                const [idx, words] = v.split(":");
                const w = words.split(",").map((s) => BigInt(s));
                if (w.length !== 4) throw new Error(`--pin wants flavor,v,rho,idHash`);
                out.pin[parseInt(idx, 10)] = { flavor: w[0], v: w[1], rho: w[2], idHash: w[3] };
                break;
            }
            default: throw new Error(`unknown arg: --${k}`);
        }
    }
    return out;
}

// Pre-compute the empty-subtree root at every level (Tornado-style).
function emptyZeros(H) {
    const z = [ZERO_VALUE];
    for (let i = 1; i < DEPTH; i++) {
        z.push(H(z[i - 1], z[i - 1]));
    }
    return z;
}

// Tornado-style insertion of `leaf` at `idx` against the rolling
// `filledSubtrees` mirror.  Returns:
//   { siblings: [DEPTH], rootBefore, rootAfter, newFilled }
// and DOES NOT mutate filledSubtrees in place (caller swaps).
function insertOne(H, zeros, filledSubtrees, leaf, idx) {
    const siblings = new Array(DEPTH);
    const newFilled = filledSubtrees.slice();

    // Walk up from the empty seat at `idx` to recompute rootBefore.
    let curBefore = ZERO_VALUE;
    let curAfter  = leaf;
    let cursor    = idx;
    for (let level = 0; level < DEPTH; level++) {
        let sibling;
        if ((cursor & 1n) === 0n) {
            // We are a left child; sibling is on the right == zeros[level]
            // (the right subtree at this seam is still empty).
            sibling = zeros[level];
            // Tornado's filledSubtrees[i] is the LEFT subtree value waiting to
            // pair with a future right sibling -- i.e. the pre-hash curAfter.
            newFilled[level] = curAfter;
            curBefore = H(curBefore, zeros[level]);
            curAfter  = H(curAfter,  zeros[level]);
        } else {
            // We are a right child; sibling is the previously-stashed left.
            sibling = filledSubtrees[level];
            curBefore = H(filledSubtrees[level], curBefore);
            curAfter  = H(filledSubtrees[level], curAfter);
            // newFilled[level] unchanged: sealed at the prior left value.
        }
        siblings[level] = sibling;
        cursor >>= 1n;
    }
    return {
        siblings,
        rootBefore: curBefore,
        rootAfter:  curAfter,
        newFilled,
    };
}

// ---- main ----------------------------------------------------------------

async function main() {
    const args = parseArgs();
    const N = args.n;

    const buildDir = path.join(ROOT, "build", "snark", `mint_batch_n${N}`);
    const wasm     = path.join(buildDir, `mint_batch_n${N}_js`, `mint_batch_n${N}.wasm`);
    const zkey     = path.join(buildDir, `mint_batch_n${N}_final.zkey`);
    const fixDir   = path.join(buildDir, "fixtures");
    if (!fs.existsSync(wasm) || !fs.existsSync(zkey)) {
        throw new Error(
            `Missing circuit artifacts for N=${N}; run scripts/snark/setup.sh ` +
            `with MINT_BATCH_PINS="... ${N} ..." first`
        );
    }
    fs.mkdirSync(fixDir, { recursive: true });

    const poseidon = await buildPoseidon();
    const F        = poseidon.F;
    const H = (a, b) => BigInt(F.toString(poseidon([a, b])));

    const zeros = emptyZeros(H);

    // Initial wallet-mirror state.  If --initial-state was provided, load it;
    // otherwise bootstrap an empty tree.
    let filledSubtrees;
    let oldRoot;
    let nextLeafIndex = Number(args.startLeaf);
    if (args.initialState) {
        const init = JSON.parse(fs.readFileSync(args.initialState, "utf8"));
        filledSubtrees = init.filledSubtrees.map(BigInt);
        oldRoot        = BigInt(init.oldRoot);
        nextLeafIndex  = Number(init.nextLeafIndex);
    } else {
        filledSubtrees = zeros.slice();
        oldRoot = H(zeros[DEPTH - 1], zeros[DEPTH - 1]);
    }

    // Generate per-leaf openings.
    const rng = mkRng(args.seed);
    const liveCount = args.liveLeaves ? args.liveLeaves.length : N;
    if (liveCount > N) throw new Error(`live leaves ${liveCount} > N=${N}`);

    const flavor    = new Array(N);
    const v         = new Array(N);
    const rho       = new Array(N);
    const idHash    = new Array(N);
    const predicate = new Array(N);
    for (let i = 0; i < N; i++) {
        if (args.pin[i]) {
            // Pinned to a wallet-generated opening (e2e fixtures).  Counts as
            // a live leaf; rng() calls below keep the stream aligned for any
            // unpinned siblings.
            flavor[i]    = args.pin[i].flavor;
            v[i]         = args.pin[i].v;
            rho[i]       = args.pin[i].rho;
            idHash[i]    = args.pin[i].idHash;
            predicate[i] = 0n;
            rng(); rng();   // consume the rho/idHash draws this leaf would have used
        } else if (i < liveCount) {
            // "Real" leaf.  flavor must lie in {A1=1, A2=2, B1=3} (the mint
            // circuit now range-constrains it and projects issuerMode).  This
            // generic fixture uses A1 (addressed, public issuer) for every
            // leaf so issuerMode is uniformly PUBLIC.
            flavor[i]    = 1n;
            v[i]         = args.liveLeaves
                ? args.liveLeaves[i]
                : BigInt(i + 1) * 1000000000000000000n; // (i+1) * 1e18
            rho[i]       = rng();
            idHash[i]    = rng();
            predicate[i] = 0n;
        } else {
            // "Dummy" leaf padding the batch up to N.  v=0 contributes nothing
            // to totalFace; rho/idHash distinct so the commitment is unique.
            // flavor still must be a valid label -- use A1 like the live leaves.
            flavor[i]    = 1n;
            v[i]         = 0n;
            rho[i]       = rng();
            idHash[i]    = rng();
            predicate[i] = 0n;
        }
    }

    // issuerMode[i] mirrors the circuit's flavor->mode projection (A2 -> 2,
    // else 1); it is a PUBLIC OUTPUT that leads the proof's publicSignals.
    const issuerMode = flavor.map((f) => (f === 2n ? 2n : 1n));

    // Compute commitments.
    const cm = [];
    for (let i = 0; i < N; i++) {
        const digest = poseidon([flavor[i], v[i], rho[i], idHash[i], predicate[i]]);
        cm.push(BigInt(F.toString(digest)));
    }

    // Walk the wallet mirror through the batch to derive siblings + newRoot.
    const siblings = new Array(N);
    let rolling = oldRoot;
    let mirror  = filledSubtrees;
    for (let i = 0; i < N; i++) {
        const idx = BigInt(nextLeafIndex + i);
        const step = insertOne(H, zeros, mirror, cm[i], idx);
        if (step.rootBefore !== rolling) {
            throw new Error(
                `wallet mirror divergence at leaf ${i}: ` +
                `rootBefore=${step.rootBefore} rolling=${rolling}`
            );
        }
        siblings[i] = step.siblings;
        rolling     = step.rootAfter;
        mirror      = step.newFilled;
    }
    const newRoot = rolling;
    const postFilled = mirror;
    const postNextLeafIndex = nextLeafIndex + N;

    const totalFace = v.reduce((acc, x) => acc + x, 0n);

    // Assemble circuit inputs.
    const input = {
        oldRoot:       oldRoot.toString(),
        newRoot:       newRoot.toString(),
        nextLeafIndex: nextLeafIndex.toString(),
        totalFace:     totalFace.toString(),
        cm:            cm.map((x) => x.toString()),
        flavor:        flavor.map((x) => x.toString()),
        v:             v.map((x) => x.toString()),
        rho:           rho.map((x) => x.toString()),
        idHash:        idHash.map((x) => x.toString()),
        predicate:     predicate.map((x) => x.toString()),
        siblings:      siblings.map((row) => row.map((x) => x.toString())),
    };

    console.log(`prover: N=${N} nextLeafIndex=${nextLeafIndex} totalFace=${totalFace}`);
    console.log(`        oldRoot=0x${oldRoot.toString(16)}`);
    console.log(`        newRoot=0x${newRoot.toString(16)}`);
    console.log(`groth16 fullProve...`);

    const t0 = Date.now();
    const { proof, publicSignals } = await snarkjs.groth16.fullProve(input, wasm, zkey);
    const t1 = Date.now();
    console.log(`        proof in ${(t1 - t0) / 1000}s`);

    // publicSignals order: circom emits main-component OUTPUTS first, then the
    // public inputs in declaration order, so:
    //   [issuerMode[0..N-1], oldRoot, newRoot, nextLeafIndex, totalFace, cm[0..N-1]]
    const expectPub = [
        ...issuerMode.map((x) => x.toString()),
        input.oldRoot,
        input.newRoot,
        input.nextLeafIndex,
        input.totalFace,
        ...input.cm,
    ];
    if (publicSignals.length !== expectPub.length) {
        throw new Error(
            `publicSignals length mismatch: ${publicSignals.length} vs ${expectPub.length}`
        );
    }
    for (let i = 0; i < expectPub.length; i++) {
        if (publicSignals[i] !== expectPub[i]) {
            throw new Error(
                `publicSignals[${i}] mismatch: ${publicSignals[i]} vs ${expectPub[i]}`
            );
        }
    }

    // Encode the proof into the calldata layout the adapter decodes.
    const pA = [proof.pi_a[0], proof.pi_a[1]];
    const pB = [[proof.pi_b[0][1], proof.pi_b[0][0]],
                [proof.pi_b[1][1], proof.pi_b[1][0]]];
    const pC = [proof.pi_c[0], proof.pi_c[1]];

    const coder = ethers.AbiCoder.defaultAbiCoder();
    const proofBytes = coder.encode(
        ["uint256[2]", "uint256[2][2]", "uint256[2]"],
        [pA, pB, pC],
    );

    const fixture = {
        N,
        depth: DEPTH,
        public: {
            issuerMode:    issuerMode.map((x) => x.toString()),
            oldRoot:       input.oldRoot,
            newRoot:       input.newRoot,
            nextLeafIndex: input.nextLeafIndex,
            totalFace:     input.totalFace,
            cm:            input.cm,
        },
        witness: input,
        proof:   { pA, pB, pC },
        proofBytes,
    };

    const outPath = path.join(fixDir, `${args.name}.json`);
    fs.writeFileSync(outPath, JSON.stringify(fixture, null, 2));
    console.log(`wrote ${outPath}`);

    // Post-mint wallet-mirror state.  Pass via --initial-state to the next
    // invocation to chain proofs across batches (e.g. mint at leaf 0 then at
    // leaf N).  Format matches the loader at the top of main().
    const statePath = path.join(fixDir, `${args.name}-state.json`);
    const stateOut = {
        filledSubtrees: postFilled.map((x) => x.toString()),
        oldRoot:        newRoot.toString(),
        nextLeafIndex:  postNextLeafIndex,
    };
    fs.writeFileSync(statePath, JSON.stringify(stateOut, null, 2));
    console.log(`wrote ${statePath}`);

    // snarkjs's own verify as a sanity check before we ship the fixture.
    const vk = JSON.parse(fs.readFileSync(path.join(buildDir, "verification_key.json"), "utf8"));
    const ok = await snarkjs.groth16.verify(vk, publicSignals, proof);
    if (!ok) throw new Error("snarkjs verify failed on own proof");
    console.log("  snarkjs verify: OK");
}

main().then(() => process.exit(0)).catch((e) => {
    console.error(e);
    process.exit(1);
});
