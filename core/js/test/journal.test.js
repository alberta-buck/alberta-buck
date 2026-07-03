// The cross-language journal fixture: JS side.
//
// Asserts the SAME facts about core/vectors/journal-sample.jsonl as
// core/python/tests/test_journal_fixture.py.  Change the fixture only
// with both suites in hand.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  parseJournal, mismatches, totalGas, summarize,
} from "../src/journal.js";

const FIXTURE = fileURLToPath(
  new URL("../../vectors/journal-sample.jsonl", import.meta.url));
const entries = parseJournal(readFileSync(FIXTURE, "utf8"));

test("fixture parses", () => {
  assert.equal(entries.length, 4);
  assert.deepEqual(entries.map((e) => e.i), [1, 2, 3, 4]);
  assert.equal(entries[0].op, "deploy");
  assert.ok(entries.slice(1).every((e) => e.op === "send"));
});

test("fixture expectations", () => {
  assert.deepEqual(mismatches(entries).map((e) => e.i), [4]);
  // the expected revert is matched, and carries its Solidity reason
  assert.equal(entries[2].expect, "revert");
  assert.equal(entries[2].matched, true);
  assert.equal(entries[2].err, "BUCK: sender identity not verified");
});

test("fixture gas total", () => {
  assert.equal(totalGas(entries), 4_222_456);
});

test("summarize rollup", () => {
  assert.deepEqual(summarize(entries), {
    entries: 4,
    ops: { deploy: 1, send: 3 },
    reverts: 1,
    mismatches: 1,
    gas: 4_222_456,
  });
});

test("malformed journals are rejected", () => {
  assert.throws(() => parseJournal('{"i":1}'), /missing field/);
  assert.throws(() => parseJournal("not json"), /invalid JSON/);
});
