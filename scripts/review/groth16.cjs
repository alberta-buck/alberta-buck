// SPDX-License-Identifier: GPL-3.0-or-later
// Python-driven local proof helper. Writes only the explicit temporary directory.
const fs = require('node:fs');
const path = require('node:path');
const snarkjs = require('snarkjs');

async function main() {
  const [mode, inputFile, wasm, key, outDir] = process.argv.slice(2);
  if (mode === 'prove') {
    const input = JSON.parse(fs.readFileSync(inputFile, 'utf8'));
    const {proof, publicSignals} = await snarkjs.groth16.fullProve(input, wasm, key);
    const vk = await snarkjs.zKey.exportVerificationKey(key);
    if (!await snarkjs.groth16.verify(vk, publicSignals, proof)) throw Error('own proof rejected');
    fs.writeFileSync(path.join(outDir, 'proof.json'), JSON.stringify({proof, publicSignals}));
  } else if (mode === 'setup') {
    // inputFile = r1cs, wasm = existing ptau, key = output zkey.
    // Ephemeral un-contributed setup: TEST ONLY, never deploy this key.
    await snarkjs.zKey.newZKey(inputFile, wasm, key);
  } else {
    throw Error('unknown mode');
  }
}
main().then(() => process.exit(0)).catch(e => { console.error(e); process.exit(1); });
