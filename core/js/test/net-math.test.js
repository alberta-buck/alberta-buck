import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createNetMath } from "../src/net-math.js";

// Missing or stale math WASM is an acceptance failure. The Make target
// explicitly rebuilds it; silent skips would conceal legacy arithmetic.
const require = createRequire(import.meta.url);
const kernel = require("alberta-buck-kernel/math");
const math = createNetMath(kernel);
const vectors = JSON.parse(readFileSync(new URL("../../../test/vectors/net-math-vectors.json", import.meta.url)));
const MAX = (1n << 256n) - 1n;

test("canonical shared integer vectors through the BigInt WASM wrapper", () => {
  assert.equal(vectors.format, 1);
  assert.equal(vectors.cases.length, vectors.count);
  assert.ok(vectors.count >= 300);
  const functions = { decay: math.decay, fundNetAfter: math.fundNetAfter, targetRelief: math.targetRelief, mulDiv: math.mulDiv,
    carryToNet: math.carryToNet, carryToNetScaled: math.carryToNetScaled, carryToNominal: math.carryToNominal,
    quoteAtTick: math.quoteAtTick };
  for (const row of vectors.cases) {
    let args = row.args.map(BigInt);
    if (row.op === "mulDiv") args.push(row.roundUp);
    // A tick travels as tick + 2^23; then the base, and whether it is token0.
    if (row.op === "quoteAtTick") args = [Number(args[0] - (1n << 23n)), args[1], args[2] === 1n];
    if (row.error) assert.throws(() => functions[row.op](...args), { message: row.error }, row.name);
    else assert.equal(functions[row.op](...args), BigInt(row.value), row.name);
  }
});

test("the full uint256 range crosses in both directions without Number conversion", () => {
  assert.equal(math.scale, 10n ** 36n);
  for (const value of [0n, 1n, (1n << 128n) - 1n, 1n << 128n, (1n << 200n) + 123n, MAX]) {
    assert.equal(math.decay(value, 0n), value);
    assert.equal(math.mulDiv(value, MAX, MAX), value);
  }
  assert.equal(math.decay(MAX, MAX), 0n);
});

test("invalid units and stale kernels fail explicitly", () => {
  for (const value of [-1n, MAX + 1n]) {
    assert.throws(() => math.decay(value, 1n), RangeError);
    assert.throws(() => math.decay(1n, value), RangeError);
  }
  for (const value of [1, "1", true]) assert.throws(() => math.decay(value, 1n), TypeError);
  assert.throws(() => math.mulDiv(1n, 1n, 1n, 1), TypeError);
  assert.throws(() => createNetMath({}), /rebuild the kernel/);
  assert.throws(() => kernel.net_decay(new Uint8Array(31), new Uint8Array(32)), /exactly 32 bytes/);
  // A rejected input does not trap or poison subsequent WASM calls.
  assert.equal(math.decay(10n ** 42n, 0n), 10n ** 42n);
});
