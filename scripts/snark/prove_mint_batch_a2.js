#!/usr/bin/env node
/**
 * Generate a mint_batch_a2 (private-issuer A2) proof for an N-leaf batch and
 * emit a fixture JSON the forge tests ingest.  Mirrors prove_mint_batch.js but
 * for the A2 circuit: every leaf is flavor == A2, idHash opens to
 * Poseidon-8(eNote, eIss), and eIss = (R.x, R.y, C.x, C.y) is a PUBLIC OUTPUT
 * (it leads publicSignals).
 *
 * Output (build/snark/mint_batch_a2_n${N}/fixtures/<name>.json):
 *   { N, depth,
 *     public: { eIss:[[..4..],..], oldRoot, newRoot, nextLeafIndex, totalFace, cm:[..] },
 *     witness: {...}, proof: {pA,pB,pC}, proofBytes }
 *
 * CLI:
 *   prove_mint_batch_a2.js [--name=<n>] [--n=<N>] [--seed=<s>]
 *                          [--start-leaf=<i>] [--initial-state=<path>]
 *                          [--live-leaves=<v0,v1,..>] [--eiss=<i>:Rx,Ry,Cx,Cy ...]
 *
 *   --eiss   Pin leaf i's eIss to a specific ciphertext (repeatable).  Used to
 *            tie a fixture leaf to a real issuer_reenc binding (the leaf-tie /
 *            collusion regression tests).  Coords are reduced mod FIELD_R, so
 *            pass the registry point's raw coords; honest points are < FIELD_R.
 */
"use strict";

const fs   = require("fs");
const path = require("path");
const { buildPoseidon } = require("circomlibjs");
const snarkjs           = require("snarkjs");
const ethers            = require("ethers");

const ROOT  = path.resolve(__dirname, "..", "..");
const DEPTH = 20;
const FIELD_R = 21888242871839275222246405745257275088548364400416034343698204186575808495617n;

const ZERO_VALUE = (() => {
    const k = ethers.keccak256(ethers.toUtf8Bytes("AlbertaBuck:Notes:zero"));
    return BigInt(k) % FIELD_R;
})();

function fieldFrom(u) {
    let v = BigInt(u) % FIELD_R;
    if (v < 0n) v += FIELD_R;
    return v;
}

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
        name: "basic", n: 2, seed: 1n, startLeaf: 0n,
        initialState: null, liveLeaves: null, eiss: {},
    };
    for (const a of process.argv.slice(2)) {
        const m = a.match(/^--([^=]+)=(.*)$/);
        if (!m) throw new Error(`bad arg: ${a}`);
        const [, k, v] = m;
        switch (k) {
            case "name": out.name = v; break;
            case "n": out.n = parseInt(v, 10); break;
            case "seed": out.seed = BigInt(v); break;
            case "start-leaf": out.startLeaf = BigInt(v); break;
            case "initial-state": out.initialState = v; break;
            case "live-leaves": out.liveLeaves = v.split(",").map((s) => BigInt(s)); break;
            case "eiss": {
                const [idx, words] = v.split(":");
                out.eiss[parseInt(idx, 10)] = words.split(",").map((w) => fieldFrom(w));
                break;
            }
            default: throw new Error(`unknown arg: --${k}`);
        }
    }
    return out;
}

function emptyZeros(H) {
    const z = [ZERO_VALUE];
    for (let i = 1; i < DEPTH; i++) z.push(H(z[i - 1], z[i - 1]));
    return z;
}

function insertOne(H, zeros, filledSubtrees, leaf, idx) {
    const siblings = new Array(DEPTH);
    const newFilled = filledSubtrees.slice();
    let curBefore = ZERO_VALUE, curAfter = leaf, cursor = idx;
    for (let level = 0; level < DEPTH; level++) {
        let sibling;
        if ((cursor & 1n) === 0n) {
            sibling = zeros[level];
            newFilled[level] = curAfter;
            curBefore = H(curBefore, zeros[level]);
            curAfter  = H(curAfter,  zeros[level]);
        } else {
            sibling = filledSubtrees[level];
            curBefore = H(filledSubtrees[level], curBefore);
            curAfter  = H(filledSubtrees[level], curAfter);
        }
        siblings[level] = sibling;
        cursor >>= 1n;
    }
    return { siblings, rootBefore: curBefore, rootAfter: curAfter, newFilled };
}

async function main() {
    const args = parseArgs();
    const N = args.n;

    const buildDir = path.join(ROOT, "build", "snark", `mint_batch_a2_n${N}`);
    const wasm     = path.join(buildDir, `mint_batch_a2_n${N}_js`, `mint_batch_a2_n${N}.wasm`);
    const zkey     = path.join(buildDir, `mint_batch_a2_n${N}_final.zkey`);
    const fixDir   = path.join(buildDir, "fixtures");
    if (!fs.existsSync(wasm) || !fs.existsSync(zkey)) {
        throw new Error(
            `Missing A2 circuit artifacts for N=${N}; run 'make snark-a2' ` +
            `(or setup.sh with MINT_BATCH_A2_PINS="... ${N} ...") first`
        );
    }
    fs.mkdirSync(fixDir, { recursive: true });

    const poseidon = await buildPoseidon();
    const F = poseidon.F;
    const H = (a, b) => BigInt(F.toString(poseidon([a, b])));
    const P = (arr) => BigInt(F.toString(poseidon(arr)));
    const zeros = emptyZeros(H);

    let filledSubtrees, oldRoot, nextLeafIndex = Number(args.startLeaf);
    if (args.initialState) {
        const init = JSON.parse(fs.readFileSync(args.initialState, "utf8"));
        filledSubtrees = init.filledSubtrees.map(BigInt);
        oldRoot        = BigInt(init.oldRoot);
        nextLeafIndex  = Number(init.nextLeafIndex);
    } else {
        filledSubtrees = zeros.slice();
        oldRoot = H(zeros[DEPTH - 1], zeros[DEPTH - 1]);
    }

    const rng = mkRng(args.seed);
    const liveCount = args.liveLeaves ? args.liveLeaves.length : N;
    if (liveCount > N) throw new Error(`live leaves ${liveCount} > N=${N}`);

    const flavor = new Array(N), v = new Array(N), rho = new Array(N);
    const idHash = new Array(N), predicate = new Array(N);
    const eNote = new Array(N), eIss = new Array(N);
    for (let i = 0; i < N; i++) {
        flavor[i]    = 2n;  // FLAVOR_A2 -- the A2 circuit constrains every leaf
        v[i]         = (i < liveCount)
            ? (args.liveLeaves ? args.liveLeaves[i] : BigInt(i + 1) * 1000000000000000000n)
            : 0n;
        rho[i]       = rng();
        predicate[i] = 0n;
        eNote[i]     = [rng(), rng(), rng(), rng()];
        eIss[i]      = args.eiss[i] ? args.eiss[i].slice() : [rng(), rng(), rng(), rng()];
        idHash[i]    = P([...eNote[i], ...eIss[i]]);  // Poseidon-8(eNote, eIss)
    }

    const cm = [];
    for (let i = 0; i < N; i++) {
        cm.push(P([flavor[i], v[i], rho[i], idHash[i], predicate[i]]));
    }

    const siblings = new Array(N);
    let rolling = oldRoot, mirror = filledSubtrees;
    for (let i = 0; i < N; i++) {
        const idx = BigInt(nextLeafIndex + i);
        const step = insertOne(H, zeros, mirror, cm[i], idx);
        if (step.rootBefore !== rolling) {
            throw new Error(`mirror divergence at leaf ${i}`);
        }
        siblings[i] = step.siblings;
        rolling     = step.rootAfter;
        mirror      = step.newFilled;
    }
    const newRoot = rolling;
    const totalFace = v.reduce((acc, x) => acc + x, 0n);

    const input = {
        oldRoot:       oldRoot.toString(),
        newRoot:       newRoot.toString(),
        nextLeafIndex: nextLeafIndex.toString(),
        totalFace:     totalFace.toString(),
        cm:            cm.map(String),
        flavor:        flavor.map(String),
        v:             v.map(String),
        rho:           rho.map(String),
        idHash:        idHash.map(String),
        predicate:     predicate.map(String),
        eNote:         eNote.map((r) => r.map(String)),
        eIssW:         eIss.map((r) => r.map(String)),
        siblings:      siblings.map((r) => r.map(String)),
    };

    console.log(`prover(a2): N=${N} nextLeafIndex=${nextLeafIndex} totalFace=${totalFace}`);
    const t0 = Date.now();
    const { proof, publicSignals } = await snarkjs.groth16.fullProve(input, wasm, zkey);
    console.log(`        proof in ${(Date.now() - t0) / 1000}s`);

    // publicSignals: [eIss(4N) outputs, oldRoot, newRoot, nextLeafIndex, totalFace, cm(N)]
    const expectPub = [
        ...eIss.flatMap((r) => r.map(String)),
        input.oldRoot, input.newRoot, input.nextLeafIndex, input.totalFace,
        ...input.cm,
    ];
    if (publicSignals.length !== expectPub.length) {
        throw new Error(`publicSignals length ${publicSignals.length} vs ${expectPub.length}`);
    }
    for (let i = 0; i < expectPub.length; i++) {
        if (publicSignals[i] !== expectPub[i]) {
            throw new Error(`publicSignals[${i}] ${publicSignals[i]} vs ${expectPub[i]}`);
        }
    }

    const pA = [proof.pi_a[0], proof.pi_a[1]];
    const pB = [[proof.pi_b[0][1], proof.pi_b[0][0]], [proof.pi_b[1][1], proof.pi_b[1][0]]];
    const pC = [proof.pi_c[0], proof.pi_c[1]];
    const coder = ethers.AbiCoder.defaultAbiCoder();
    const proofBytes = coder.encode(
        ["uint256[2]", "uint256[2][2]", "uint256[2]"], [pA, pB, pC]);

    const fixture = {
        N, depth: DEPTH,
        public: {
            eIss:          eIss.map((r) => r.map(String)),
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

    const statePath = path.join(fixDir, `${args.name}-state.json`);
    fs.writeFileSync(statePath, JSON.stringify({
        filledSubtrees: mirror.map(String),
        oldRoot: newRoot.toString(),
        nextLeafIndex: nextLeafIndex + N,
    }, null, 2));

    const vk = JSON.parse(fs.readFileSync(path.join(buildDir, "verification_key.json"), "utf8"));
    const ok = await snarkjs.groth16.verify(vk, publicSignals, proof);
    if (!ok) throw new Error("snarkjs verify failed on own proof");
    console.log("  snarkjs verify: OK");
}

main().then(() => process.exit(0)).catch((e) => { console.error(e); process.exit(1); });
