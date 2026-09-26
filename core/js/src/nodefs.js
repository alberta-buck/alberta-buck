// Node-only IO glue: repo/artifact discovery and file journal sinks.
// Keep environment-specific imports here so session.js/journal.js/v3.js
// stay loadable in the browser (Phase 4).

import { readFileSync, existsSync, mkdirSync, appendFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";

import { JournalWriter } from "./journal.js";

const require = createRequire(import.meta.url);

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

/** Return {abi, bytecode} for a contract.
 *
 *  Resolution order, mirroring buck_core.artifacts.load_artifact:
 *
 *    1. out/<solFile or name>.sol/<name>.json in a repo checkout -- the
 *       developer path, so a freshly rebuilt contract takes effect at once;
 *    2. the installed alberta-buck-contracts package.
 *
 *  Without (2) this package is unusable outside a checkout: repoRoot()
 *  throws when no foundry.toml is reachable, so `npm i alberta-buck-core`
 *  would import cleanly and then fail on the first deploy.
 */
export function loadArtifact(name, solFile = null) {
  try {
    const f = join(repoRoot(), "out", `${solFile ?? name}.sol`, `${name}.json`);
    const art = JSON.parse(readFileSync(f, "utf8"));
    return { abi: art.abi, bytecode: art.bytecode.object };
  } catch {
    // No checkout, or that contract is not built here -- try the package.
  }

  let bundle;
  try {
    bundle = require("alberta-buck-contracts/contracts.json");
  } catch {
    throw new Error(
      `no artifact for ${name}: not in a repo checkout with out/ built ` +
      `(run \`make build\`), and alberta-buck-contracts is not installed`);
  }

  const c = bundle.contracts[name];
  if (!c) {
    throw new Error(
      `no artifact for ${name}: alberta-buck-contracts ships ` +
      `${Object.keys(bundle.contracts).join(", ")}. Third-party contracts ` +
      `(Uniswap, WETH9) come from their own packages.`);
  }
  return { abi: c.abi, bytecode: c.bytecode };
}

/** A vendored third-party build (alberta_buck/sim/artifacts/<name>.json:
 *  UniversalRouter, Permit2), flattened to {abi, bytecode}. */
export function vendoredArtifact(name) {
  // Committed files: look first in the checkout this module belongs to,
  // then wherever repoRoot() points (ALBERTA_BUCK_REPO may name another).
  const rel = join("alberta_buck", "sim", "artifacts", `${name}.json`);
  const own = resolve(dirname(new URL(import.meta.url).pathname), "..", "..", "..");
  const f = [join(own, rel), join(repoRoot(), rel)].find((p) => existsSync(p));
  if (!f) throw new Error(`no vendored artifact ${rel}`);
  const art = JSON.parse(readFileSync(f, "utf8"));
  return { abi: art.abi, bytecode: art.bytecode.object ?? art.bytecode };
}

/** loadArtifact, then the vendored builds: everything a market world needs. */
export function loadAnyArtifact(name) {
  try {
    return loadArtifact(name);
  } catch (e) {
    try {
      return vendoredArtifact(name);
    } catch {
      throw e;
    }
  }
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
