#!/usr/bin/env node
/**
 * Generate a spend_a-circuit (Phase 8 V2, A-flavor) Groth16 proof for the
 * fixture leaf described in `test/vectors/identity.json` (`spend_a_v2`
 * section, emitted by alberta_buck.wallet.vectors).  The fixture pins:
 *
 *   - flavor = A2 (=2)
 *   - face, rho, predicate (chosen by the Python builder)
 *   - idHash = Poseidon-8(E_n.R, E_n.C, E_iss.R, E_iss.C) reduced mod F_R
 *   - cm = Poseidon-5(flavor, face, rho, idHash, predicate)
 *   - nullifier = Poseidon-3(rho, idHash, 4243)
 *   - E_n_alice (public, threaded through the SNARK and the off-chain CP-DLEQ)
 *   - E_iss_alice (private witness; the four issuerData[] words)
 *   - cp_proof (CP-DLEQ proof verified by IdentityRegistry.verifySpendCP)
 *
 * The legacy mint circuit is reused: it does not constrain idHash (just
 * commits the leaf), so any cm produced from a valid (flavor, v, rho,
 * idHash, predicate) opening is mintable.  The fixture builds a single-leaf
 * tree (cm at index 0, all other slots = ZERO_VALUE) and packages the
 * Merkle path so the spend_a witness slots in directly.
 *
 * Output (build/snark/spend_a/fixtures/<name>.json):
 *   {
 *     mintInput:  { ...inputs for the mint SNARK to insert cm at index 0... },
 *     spend: {
 *       leafIndex,
 *       public: { noteRoot, nullifier, face, recipient, chainId,
 *                 eNoteRx, eNoteRy, eNoteCx, eNoteCy },
 *       cpProof: { e, s, T1, T2 },
 *       proof:  { pA, pB, pC },
 *       proofBytes: "0x..."
 *     }
 *   }
 *
 * Usage:
 *   node scripts/snark/prove_spend_a.js [<fixtureName>]
 */
"use strict";

const fs   = require("fs");
const path = require("path");
const { buildPoseidon } = require("circomlibjs");
const snarkjs           = require("snarkjs");
const ethers            = require("ethers");

const ROOT     = path.resolve(__dirname, "..", "..");
const SPEND    = path.join(ROOT, "build", "snark", "spend_a");
const WASM     = path.join(SPEND, "spend_a_js", "spend_a.wasm");
const ZKEY     = path.join(SPEND, "spend_a_final.zkey");
const VKEY     = path.join(SPEND, "verification_key.json");
const FIX      = path.join(SPEND, "fixtures");
const VECTORS  = path.join(ROOT, "test", "vectors", "identity.json");

const FIELD_R = 21888242871839275222246405745257275088548364400416034343698204186575808495617n;
const TREE_DEPTH = 20;

const FIXTURE_NAME = process.argv[2] || "spendA_v2_alice";

function toBig(s) {
    if (typeof s === "bigint") return s;
    if (typeof s === "string" && s.startsWith("0x")) return BigInt(s);
    return BigInt(s);
}

async function main() {
    fs.mkdirSync(FIX, { recursive: true });
    if (!fs.existsSync(WASM) || !fs.existsSync(ZKEY)) {
        throw new Error(`Missing spend_a artifacts; run scripts/snark/setup.sh first`);
    }
    if (!fs.existsSync(VECTORS)) {
        throw new Error(`Missing ${VECTORS}; run \`python -m alberta_buck.wallet.cli emit-vectors\` first`);
    }

    const vectors = JSON.parse(fs.readFileSync(VECTORS, "utf8"));
    const sa      = vectors.spend_a_v2;

    const poseidon = await buildPoseidon();
    const F        = poseidon.F;
    const H2 = (a, b)        => BigInt(F.toString(poseidon([a, b])));

    // ZERO_VALUE matches Notes.ZERO_VALUE (and the in-circuit literal).
    const zeroValue = BigInt(ethers.keccak256(
        ethers.toUtf8Bytes("AlbertaBuck:Notes:zero")
    )) % FIELD_R;

    // Single-leaf tree at index 0.  The path siblings are the empty-tree
    // zero hashes at each level; the path bits are all zero (left-leaning).
    const cm        = toBig(sa.cm);
    const nullifier = toBig(sa.nullifier);
    const face      = toBig(sa.face);
    const flavor    = toBig(sa.flavor);
    const rho       = toBig(sa.rho);
    const idHash    = toBig(sa.idHash);
    const predicate = toBig(sa.predicate);
    const recipient = toBig(sa.recipient);
    const chainId   = toBig(sa.chainid);
    const eNoteRx   = toBig(sa.E_n.R.x);
    const eNoteRy   = toBig(sa.E_n.R.y);
    const eNoteCx   = toBig(sa.E_n.C.x);
    const eNoteCy   = toBig(sa.E_n.C.y);
    const issuerData = sa.issuerData.map(toBig);

    // Build the empty-tree zeros chain so the single-leaf root computation
    // matches the Notes contract's EMPTY_ROOT semantics for unused slots.
    const zeros = [zeroValue];
    for (let i = 1; i < TREE_DEPTH; i++) {
        zeros.push(H2(zeros[i - 1], zeros[i - 1]));
    }

    // leaf 0 = cm; sibling at every level is the zero-hash of that level.
    const pathSiblings = [];
    const pathBits     = [];
    {
        let cur = cm;
        for (let l = 0; l < TREE_DEPTH; l++) {
            pathSiblings.push(zeros[l]);
            pathBits.push(0n);
            cur = H2(cur, zeros[l]);
        }
        var noteRoot = cur;
    }

    const input = {
        noteRoot:      noteRoot.toString(),
        nullifier:     nullifier.toString(),
        face:          face.toString(),
        recipient:     recipient.toString(),
        chainId:       chainId.toString(),
        eNoteRx:       eNoteRx.toString(),
        eNoteRy:       eNoteRy.toString(),
        eNoteCx:       eNoteCx.toString(),
        eNoteCy:       eNoteCy.toString(),

        flavor:        flavor.toString(),
        v:             face.toString(),
        rho:           rho.toString(),
        idHash:        idHash.toString(),
        predicate:     predicate.toString(),
        issuerData:    issuerData.map((x) => x.toString()),
        pathElements:  pathSiblings.map((x) => x.toString()),
        pathIndices:   pathBits.map((x) => x.toString()),
    };

    console.log(`prover: spend_a V2 leaf=0 flavor=${flavor} face=${face}`);
    console.log(`        noteRoot  = ${input.noteRoot}`);
    console.log(`        nullifier = ${input.nullifier}`);
    console.log(`        E_n.R     = (${input.eNoteRx}, ${input.eNoteRy})`);
    console.log(`        E_n.C     = (${input.eNoteCx}, ${input.eNoteCy})`);
    console.log(`groth16 fullProve...`);
    const t0 = Date.now();
    const { proof, publicSignals } = await snarkjs.groth16.fullProve(input, WASM, ZKEY);
    const t1 = Date.now();
    console.log(`        proof in ${(t1 - t0) / 1000}s`);

    const expectPub = [
        input.noteRoot, input.nullifier, input.face, input.recipient, input.chainId,
        input.eNoteRx,  input.eNoteRy,   input.eNoteCx, input.eNoteCy,
    ];
    for (let i = 0; i < expectPub.length; i++) {
        if (publicSignals[i] !== expectPub[i]) {
            throw new Error(`publicSignals[${i}] mismatch: ${publicSignals[i]} vs ${expectPub[i]}`);
        }
    }

    const pA = [proof.pi_a[0], proof.pi_a[1]];
    const pB = [[proof.pi_b[0][1], proof.pi_b[0][0]],
                [proof.pi_b[1][1], proof.pi_b[1][0]]];
    const pC = [proof.pi_c[0], proof.pi_c[1]];

    const coder = ethers.AbiCoder.defaultAbiCoder();
    const proofBytes = coder.encode(
        ["uint256[2]", "uint256[2][2]", "uint256[2]"],
        [pA, pB, pC],
    );

    // Mint inputs: rebuild the leaf at index 0 against EMPTY_ROOT.
    // The existing legacy mint.circom takes a single leaf (cm) and inserts
    // it at the next slot; our forge test reuses Notes.mint with the
    // mint_batch (N=1) prover.  To keep this script self-contained we
    // package only the cm + leaf-index data; the test computes the rest.
    const fixture = {
        $schema: "spend_a_v2",
        mint: {
            cm:        sa.cm,
            face:      sa.face,
            leafIndex: 0,
        },
        spend: {
            leafIndex: 0,
            public: {
                noteRoot:  input.noteRoot,
                nullifier: input.nullifier,
                face:      input.face,
                recipient: sa.recipient,
                chainId:   input.chainId,
                eNoteRx:   input.eNoteRx,
                eNoteRy:   input.eNoteRy,
                eNoteCx:   input.eNoteCx,
                eNoteCy:   input.eNoteCy,
            },
            cpProof: sa.cp_proof,
            E_n:     sa.E_n,
            spender: sa.spender,
            witness: input,
            proof:   { pA, pB, pC },
            proofBytes,
        },
    };

    const outPath = path.join(FIX, `${FIXTURE_NAME}.json`);
    fs.writeFileSync(outPath, JSON.stringify(fixture, null, 2));
    console.log(`wrote ${outPath}`);

    const vk = JSON.parse(fs.readFileSync(VKEY, "utf8"));
    const ok = await snarkjs.groth16.verify(vk, publicSignals, proof);
    if (!ok) throw new Error("snarkjs verify failed on own proof");
    console.log("  snarkjs verify: OK");
}

main().then(() => process.exit(0)).catch((e) => {
    console.error(e);
    process.exit(1);
});
