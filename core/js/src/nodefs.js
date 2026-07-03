// Node-only IO glue: repo/artifact discovery and file journal sinks.
// Keep environment-specific imports here so session.js/journal.js/v3.js
// stay loadable in the browser (Phase 4).

import { readFileSync, existsSync, mkdirSync, appendFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

import { JournalWriter } from "./journal.js";

/** The repository checkout root: ALBERTA_BUCK_REPO, or walk up from cwd
 *  (then from this file) looking for foundry.toml -- the same resolution
 *  the Python side uses (buck_core.artifacts.repo_root). */
export function repoRoot() {
  const starts = [];
  if (process.env.ALBERTA_BUCK_REPO) starts.push(process.env.ALBERTA_BUCK_REPO);
  starts.push(process.cwd(), dirname(new URL(import.meta.url).pathname));
  for (const start of starts) {
    let p = resolve(start);
    for (;;) {
      if (existsSync(join(p, "foundry.toml"))) return p;
      const up = dirname(p);
      if (up === p) break;
      p = up;
    }
  }
  throw new Error(
    "alberta-buck repo root not found (looked for foundry.toml); " +
    "set ALBERTA_BUCK_REPO or run from inside the repo checkout");
}

/** Return {abi, bytecode} for out/<solFile or name>.sol/<name>.json. */
export function loadArtifact(name, solFile = null) {
  const f = join(repoRoot(), "out", `${solFile ?? name}.sol`, `${name}.json`);
  const art = JSON.parse(readFileSync(f, "utf8"));
  return { abi: art.abi, bytecode: art.bytecode.object };
}

/** True when the Foundry artifacts are built (tests skip when not). */
export function artifactsAvailable() {
  try {
    loadArtifact("MockERC20");
    return true;
  } catch {
    return false;
  }
}

/** A JournalWriter appending JSONL lines to `path`. */
export function fileJournalWriter(path) {
  mkdirSync(dirname(resolve(path)), { recursive: true });
  return new JournalWriter((line) => appendFileSync(path, line));
}
