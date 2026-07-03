// Journal: the shared JSONL operation record of a ChainSession run.
//
// Schema (alberta-buck-platform.org, Layer 1; pinned by
// core/vectors/journal-sample.jsonl, asserted identically by the Python
// suite): one JSON object per line with fields
//   i, tag, op, fn, sender, expect, outcome, matched, gas, tx, block, err
// Unknown fields are ignored; extra whitespace/blank lines are tolerated.
//
// The reader comes first (JS consumes Python-produced journals for demos
// and cross-platform diffing); the writer lands with the JS ChainSession.

const REQUIRED = ["i", "op", "fn", "expect", "outcome", "matched"];

/** Parse JSONL journal text into an array of entry objects. */
export function parseJournal(text) {
  const entries = [];
  for (const [n, raw] of text.split("\n").entries()) {
    const line = raw.trim();
    if (!line) continue;
    let entry;
    try {
      entry = JSON.parse(line);
    } catch (e) {
      throw new Error(`journal line ${n + 1}: invalid JSON: ${e.message}`);
    }
    for (const f of REQUIRED) {
      if (!(f in entry)) {
        throw new Error(`journal line ${n + 1}: missing field '${f}'`);
      }
    }
    entries.push(entry);
  }
  return entries;
}

/** Entries whose outcome contradicted their declared expectation. */
export function mismatches(entries) {
  return entries.filter((e) => e.matched === false);
}

/** Total gasUsed across entries. */
export function totalGas(entries) {
  return entries.reduce((sum, e) => sum + (e.gas ?? 0), 0);
}

/** One-object rollup of a journal: op counts, reverts, mismatches, gas. */
export function summarize(entries) {
  const ops = {};
  let reverts = 0;
  for (const e of entries) {
    ops[e.op] = (ops[e.op] ?? 0) + 1;
    if (e.outcome === "revert") reverts += 1;
  }
  return {
    entries: entries.length,
    ops,
    reverts,
    mismatches: mismatches(entries).length,
    gas: totalGas(entries),
  };
}
