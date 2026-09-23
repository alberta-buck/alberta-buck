#!/usr/bin/env node
/**
 * Build an off-chain reference of the Notes incremental Merkle tree, so the
 * Solidity test can assert exact equality of the on-chain root against an
 * independently-computed Poseidon hash chain.
 *
 * Output: build/snark/poseidon/merkle_fixture.json
 *   {
 *     depth, zeroValue, fieldR,
 *     zeros: [zeros[0], zeros[1], ..., zeros[depth-1]],
 *     emptyRoot,
 *     leaves:        [hex, hex, hex, ...],
 *     rootAfterEach: [hex, hex, hex, ...]
 *   }
 *
 * The leaves are the same three Python-wallet commitments the Solidity test
 * already pins (CM1/CM2/CM3 in Notes.t.sol) so the on-chain mint and the
 * off-chain reference walk the same insertion order.
 *
 * The empty-leaf scalar is keccak256("AlbertaBuck/Notes/Zero/v2") % FIELD_R,
 * matching Notes.sol's constructor.
 */
"use strict";

const fs       = require("fs");
const path     = require("path");
const ethers   = require("ethers");
const { buildPoseidon } = require("circomlibjs");

const ROOT  = path.resolve(__dirname, "..", "..");
const OUT   = path.join(ROOT, "build", "snark", "poseidon", "merkle_fixture.json");

const DEPTH   = 20;
const FIELD_R = 21888242871839275222246405745257275088548364400416034343698204186575808495617n;

// Same commitments the Solidity Notes test pins.
const LEAVES = [
    BigInt("0x2f32199a12908d70cb27b94f766fccde66484f15ec37b1143f0c9958cdd3379d"),
    BigInt("0x067dc83e554e6adbf068d54a60a711b426be3a79cb43907396eee3dd1cd0b7ab"),
    BigInt("0x114e67cd78234325b9227116d9abc66397e28860b3287259aad1b60e7ac11346"),
];

function toHex(x) {
    return "0x" + x.toString(16);
}

async function main() {
    const poseidon = await buildPoseidon();
    const F        = poseidon.F;
    const H = (a, b) => BigInt(F.toString(poseidon([a, b])));

    const zKeccak = ethers.keccak256(ethers.toUtf8Bytes("AlbertaBuck/Notes/Zero/v2"));
    const zeroValue = BigInt(zKeccak) % FIELD_R;

    const zeros = [zeroValue];
    for (let i = 1; i < DEPTH; i++) {
        zeros.push(H(zeros[i - 1], zeros[i - 1]));
    }
    const emptyRoot = H(zeros[DEPTH - 1], zeros[DEPTH - 1]);

    // Tornado-style filled-subtrees insertion against an off-chain mirror.
    const filled = zeros.slice();
    let nextIdx = 0;
    const rootAfterEach = [];
    for (const leaf of LEAVES) {
        let cur = leaf;
        let idx = nextIdx;
        for (let level = 0; level < DEPTH; level++) {
            let left, right;
            if ((idx & 1) === 0) {
                left  = cur;
                right = zeros[level];
                filled[level] = cur;
            } else {
                left  = filled[level];
                right = cur;
            }
            cur = H(left, right);
            idx >>= 1;
        }
        nextIdx += 1;
        rootAfterEach.push(cur);
    }

    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    const out = {
        depth: DEPTH,
        fieldR: FIELD_R.toString(),
        zeroValue: toHex(zeroValue),
        zeros: zeros.map(toHex),
        emptyRoot: toHex(emptyRoot),
        leaves: LEAVES.map(toHex),
        rootAfterEach: rootAfterEach.map(toHex),
    };
    fs.writeFileSync(OUT, JSON.stringify(out, null, 2));
    console.log(`wrote ${OUT}`);
    console.log(`  zeroValue   = ${out.zeroValue}`);
    console.log(`  emptyRoot   = ${out.emptyRoot}`);
    out.rootAfterEach.forEach((r, i) => {
        console.log(`  root[${i}]    = ${r}`);
    });
}

main().then(() => process.exit(0)).catch((e) => {
    console.error(e);
    process.exit(1);
});
