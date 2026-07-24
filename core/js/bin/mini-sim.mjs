#!/usr/bin/env node
// Run the journal-parity mini-scenario (core/vectors/mini-scenario.json)
// on a chosen backend, writing the journal for cross-platform comparison:
//
//   node bin/mini-sim.mjs --backend tevm  --journal out.jsonl
//   node bin/mini-sim.mjs --backend anvil --rpc http://127.0.0.1:8545 \
//                         --journal out.jsonl
//
// The anvil run signs with dev account 0 -- the SAME deployer the Python
// runner (python -m alberta_buck.sim.mini) uses -- so a fresh anvil gives
// byte-identical addresses and calldata.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { parseArgs } from "node:util";

import { anvilSession, tevmSession } from "../src/backends.js";
import { fileJournalWriter, loadArtifact, repoRoot } from "../src/nodefs.js";
import { runMini } from "../src/scenarios/mini.js";

const { values: opt } = parseArgs({
  options: {
    backend: { type: "string", default: "tevm" },
    rpc: { type: "string", default: "http://127.0.0.1:8545" },
    journal: { type: "string" },
    scenario: { type: "string" },
  },
});
if (!opt.journal) {
  console.error("usage: mini-sim.mjs --backend tevm|anvil --journal PATH [--rpc URL]");
  process.exit(2);
}

const sc = JSON.parse(readFileSync(
  opt.scenario ?? join(repoRoot(), "core", "vectors", "mini-scenario.json"),
  "utf8"));

const journal = fileJournalWriter(opt.journal);
const session = opt.backend === "anvil"
  ? anvilSession(opt.rpc, { accountIndex: 0, journal })
  : await tevmSession({ journal });

await runMini(session, loadArtifact, sc);

console.log(JSON.stringify({
  backend: opt.backend,
  mismatches: session.mismatches.length,
}));
process.exit(session.mismatches.length === 0 ? 0 : 1);
