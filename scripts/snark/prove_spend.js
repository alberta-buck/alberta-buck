#!/usr/bin/env node
/**
 * Generate a spend-circuit proof against a freshly minted pair of A2-style
 * notes, and emit a fixture JSON the forge test ingests.  The fixture
 * encodes everything the on-chain spend path needs:
 *
 * Output (build/snark/spend/fixtures/<name>.json):
 *   {
 *     mintFixture: { ... copy of the mint fixture for re-minting ... },
 *     spend: {
 *       leafIndex,                       // which of the two minted leaves
 *       public: { noteRoot, nullifier, face, recipient, chainId },
 *       proof:  { pA, pB, pC },
 *       proofBytes: "0x..."              // ABI-encoded for the adapter
 *     }
 *   }
 *
 * Recipient and chainId are arguments so the fixture binds them at proving
 * time -- they become public inputs the Groth16 verifier checks.
 *
 * Usage:
 *   node scripts/snark/prove_spend.js <mintName> <leafIndex> <recipientHex> <chainId> [<spendName>]
 * e.g.
 *   node scripts/snark/prove_spend.js basic 0 0x1111111111111111111111111111111111111111 1 spendA
 */
"use strict";

const fs   = require("fs");
const path = require("path");
const { buildPoseidon }   = require("circomlibjs");
const snarkjs             = require("snarkjs");
const ethers              = require("ethers");

const ROOT  = path.resolve(__dirname, "..", "..");
const MINT  = path.join(ROOT, "build", "snark", "mint");
const SPEND = path.join(ROOT, "build", "snark", "spend");
const WASM  = path.join(SPEND, "spend_js", "spend.wasm");
const ZKEY  = path.join(SPEND, "spend_final.zkey");
const VKEY  = path.join(SPEND, "verification_key.json");
const FIX   = path.join(SPEND, "fixtures");

const FIELD_R = 21888242871839275222246405745257275088548364400416034343698204186575808495617n;
const TREE_DEPTH = 20;
const NULLIFIER_TAG = 4242n;

const MINT_NAME       = process.argv[2] || "basic";
const LEAF_INDEX      = Number(process.argv[3] || "0");
const RECIPIENT       = process.argv[4] || "0x1111111111111111111111111111111111111111";
const CHAIN_ID        = BigInt(process.argv[5] || "1");
const SPEND_NAME      = process.argv[6] || `spend_leaf${LEAF_INDEX}`;

function toBig(s) {
    if (typeof s === "bigint") return s;
    if (typeof s === "string" && s.startsWith("0x")) return BigInt(s);
    return BigInt(s);
}

async function main() {
    fs.mkdirSync(FIX, { recursive: true });
    if (!fs.existsSync(WASM) || !fs.existsSync(ZKEY)) {
        throw new Error(`Missing spend artifacts; run scripts/snark/setup.sh first`);
    }

    const mintFixturePath = path.join(MINT, "fixtures", `${MINT_NAME}.json`);
    const mintFixture = JSON.parse(fs.readFileSync(mintFixturePath, "utf8"));

    const poseidon = await buildPoseidon();
    const F        = poseidon.F;
    const H2 = (a, b)        => BigInt(F.toString(poseidon([a, b])));
    const H3 = (a, b, c)     => BigInt(F.toString(poseidon([a, b, c])));

    // ZERO_VALUE matches Notes.sol: keccak256("AlbertaBuck:Notes:zero") % r.
    const zeroValue = BigInt(ethers.keccak256(
        ethers.toUtf8Bytes("AlbertaBuck:Notes:zero")
    )) % FIELD_R;
    const zeros = [zeroValue];
    for (let i = 1; i < TREE_DEPTH; i++) {
        zeros.push(H2(zeros[i - 1], zeros[i - 1]));
    }

    // Materialize the tree layer-by-layer against the final state (all cms
    // inserted).  `layer[l]` holds every populated node at level l; any
    // position not present is implicitly the empty-subtree root `zeros[l]`.
    // The filled-subtrees walk Notes.sol uses is an *insertion* optimization
    // that happens to yield the same root as this explicit layering, but
    // it captures siblings-at-insertion-time rather than siblings-at-the-
    // final-tree, which is what the spend SNARK's Merkle membership path
    // actually needs.
    const cms = mintFixture.public.cm.map(toBig);
    const layers = [cms.slice()];
    for (let l = 0; l < TREE_DEPTH; l++) {
        const cur = layers[l];
        const next = [];
        for (let i = 0; i < cur.length; i += 2) {
            const left  = cur[i];
            const right = (i + 1 < cur.length) ? cur[i + 1] : zeros[l];
            next.push(H2(left, right));
        }
        // Pad with the appropriate empty-subtree root so noteRoot agrees
        // with the on-chain walk even when this layer has odd width.
        layers.push(next);
    }
    // At every level l we need exactly ceil(n / 2) populated nodes plus
    // empty-subtree padding.  noteRoot is the sibling pair at the top of
    // the populated left spine versus the empty-subtree root at the top
    // level (same as the on-chain filled-subtrees insertion).
    let noteRoot = layers[TREE_DEPTH][0] ?? zeros[TREE_DEPTH];
    // In the overwhelmingly common case (n <= 2^depth) the top layer has
    // exactly one element.  If it doesn't (e.g. n=0) we fall back to the
    // empty-tree root.
    if (layers[TREE_DEPTH].length === 0) {
        noteRoot = H2(zeros[TREE_DEPTH - 1], zeros[TREE_DEPTH - 1]);
    }

    // Extract the spend leaf's sibling path and its index-bit decomposition
    // against the final tree.
    const pathSiblings = [];
    const pathBits = [];
    {
        let idx = LEAF_INDEX;
        for (let l = 0; l < TREE_DEPTH; l++) {
            const sibIdx = idx ^ 1;
            const sibling = (sibIdx < layers[l].length)
                            ? layers[l][sibIdx]
                            : zeros[l];
            pathSiblings.push(sibling);
            pathBits.push(BigInt(idx & 1));
            idx = idx >> 1;
        }
    }

    // Witness opening for the spent leaf (taken from the mint fixture).
    const w = mintFixture.witness;
    const flavor    = toBig(w.flavor[LEAF_INDEX]);
    const v         = toBig(w.v[LEAF_INDEX]);
    const rho       = toBig(w.rho[LEAF_INDEX]);
    const idHash    = toBig(w.idHash[LEAF_INDEX]);
    const predicate = toBig(w.predicate[LEAF_INDEX]);
    const face      = v;
    const nullifier = H3(rho, idHash, NULLIFIER_TAG);

    const recipient = BigInt(RECIPIENT);
    const input = {
        noteRoot:      noteRoot.toString(),
        nullifier:     nullifier.toString(),
        face:          face.toString(),
        recipient:     recipient.toString(),
        chainId:       CHAIN_ID.toString(),

        flavor:        flavor.toString(),
        v:             v.toString(),
        rho:           rho.toString(),
        idHash:        idHash.toString(),
        predicate:     predicate.toString(),
        pathElements:  pathSiblings.map((x) => x.toString()),
        pathIndices:   pathBits.map((x) => x.toString()),
    };

    const { proof, publicSignals } = await snarkjs.groth16.fullProve(input, WASM, ZKEY);

    // Public-signal order matches `component main { public [...] }`:
    //   [ noteRoot, nullifier, face, recipient, chainId ]
    const expectPub = [
        input.noteRoot, input.nullifier, input.face, input.recipient, input.chainId,
    ];
    for (let i = 0; i < 5; i++) {
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

    const fixture = {
        mintFixture,
        spend: {
            leafIndex: LEAF_INDEX,
            public: {
                noteRoot:  input.noteRoot,
                nullifier: input.nullifier,
                face:      input.face,
                recipient: RECIPIENT,
                chainId:   input.chainId,
            },
            witness: input,
            proof:   { pA, pB, pC },
            proofBytes,
        },
    };

    const outPath = path.join(FIX, `${SPEND_NAME}.json`);
    fs.writeFileSync(outPath, JSON.stringify(fixture, null, 2));
    console.log(`wrote ${outPath}`);
    console.log(`  noteRoot  = ${input.noteRoot}`);
    console.log(`  nullifier = ${input.nullifier}`);
    console.log(`  face      = ${input.face}`);
    console.log(`  recipient = ${RECIPIENT}`);
    console.log(`  chainId   = ${input.chainId}`);

    const vk = JSON.parse(fs.readFileSync(VKEY, "utf8"));
    const ok = await snarkjs.groth16.verify(vk, publicSignals, proof);
    if (!ok) throw new Error("snarkjs verify failed on own proof");
    console.log("  snarkjs verify: OK");
}

main().then(() => process.exit(0)).catch((e) => {
    console.error(e);
    process.exit(1);
});
