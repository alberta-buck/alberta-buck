#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Pack our webevm fork as `alberta-buck-webevm`, the package alberta-buck-core
// depends on until upstream releases the node clock (core/js/vendor/README.md).
//
// webevm (MIT, github.com/wighawag/webevm) is the in-browser EVM every JS world
// runs on.  Its node has no controllable clock; our fork (github.com/pjkundert/
// webevm, branch feature/block-clock-cheats) adds one with anvil's time cheats,
// offered upstream as wighawag/webevm#23.  A published alberta-buck-core cannot
// depend on a `file:` tarball, so until upstream releases the clock we publish
// the fork's build under our own name and depend on it by npm alias:
//
//     "webevm": "npm:alberta-buck-webevm@<version>"
//
// which keeps every `import ... from "webevm"` unchanged; when upstream
// releases, the alias becomes a plain "webevm" version and this script goes.
//
// The package is the fork's packages/webevm build (dist + src) with upstream's
// README and LICENSE, as upstream's own prepack assembles it, and only its
// package.json changed: our name and version, the fork as its repository, a
// notice at the top of the README, and `albertaBuckSource` recording exactly
// what was built.  Scripts and devDependencies are dropped (nothing to build at
// install time; upstream's prepack would look for files outside the package).
//
// Usage (from the repository root, inside nix develop):
//
//     node scripts/webevm-fork.mjs [--fork ~/src/webevm] [--version 0.9.1-clock.1] [--out core/js/vendor]
//
// It refuses a fork checkout with uncommitted changes, builds it (corepack
// pnpm build), and writes <out>/alberta-buck-webevm-<version>.tgz.

import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join, resolve } from "node:path";

const NAME = "alberta-buck-webevm";
const FORK_URL = "https://github.com/pjkundert/webevm";
const BRANCH = "feature/block-clock-cheats";
const PR = "https://github.com/wighawag/webevm/pull/23";

const args = process.argv.slice(2);
const opt = (name, dflt) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : dflt;
};
const fork = resolve(opt("fork", process.env.WEBEVM_FORK ?? join(homedir(), "src/webevm")));
const version = opt("version", process.env.WEBEVM_FORK_VERSION ?? "0.9.1-clock.1");
const out = resolve(opt("out", "core/js/vendor"));
const pkgDir = join(fork, "packages/webevm");

const run = (cmd, argv, cwd) => execFileSync(cmd, argv, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] }).trim();

if (!existsSync(join(pkgDir, "package.json"))) throw new Error(`${pkgDir}: not a webevm checkout`);
const dirty = run("git", ["status", "--porcelain", "--untracked-files=no"], fork);
if (dirty) throw new Error(`${fork} has uncommitted changes:\n${dirty}`);
const commit = run("git", ["rev-parse", "HEAD"], fork);
const branch = run("git", ["rev-parse", "--abbrev-ref", "HEAD"], fork);
if (branch !== BRANCH) console.warn(`note: ${fork} is on ${branch}, not ${BRANCH}`);

console.log(`building webevm at ${commit.slice(0, 7)} (${branch})`);
run("corepack", ["pnpm", "build"], pkgDir);

const upstream = JSON.parse(readFileSync(join(pkgDir, "package.json"), "utf8"));
const stage = mkdtempSync(join(tmpdir(), "alberta-buck-webevm-"));
try {
  for (const f of upstream.files) cpSync(join(pkgDir, f), join(stage, f), { recursive: true });
  cpSync(join(fork, "LICENSE"), join(stage, "LICENSE"));
  const readme = readFileSync(join(fork, "README.md"), "utf8");
  writeFileSync(join(stage, "README.md"),
    `> **${NAME}** is [webevm](https://github.com/wighawag/webevm) ${upstream.version} with the node\n` +
    `> clock and its anvil-compatible time cheats (\`evm_setNextBlockTimestamp\`, \`evm_increaseTime\`,\n` +
    `> \`evm_mine\`, \`anvil_mine\`, \`anvil_setBlockTimestampInterval\`), offered upstream as\n` +
    `> [wighawag/webevm#23](${PR}). It is published by the Alberta Buck project so that its\n` +
    `> packages can depend on a released build until upstream releases the clock; then use\n` +
    `> \`webevm\` itself. Built from [pjkundert/webevm](${FORK_URL}/tree/${commit}) at \`${commit.slice(0, 7)}\`.\n` +
    `> MIT, as upstream; all credit for webevm to its author.\n\n` + readme);

  const pkg = { ...upstream };
  delete pkg.scripts;
  delete pkg.devDependencies;
  Object.assign(pkg, {
    name: NAME,
    version,
    description: `webevm ${upstream.version} with a controllable node clock (anvil-compatible time cheats; ` +
      `wighawag/webevm#23), published until upstream releases it. ${upstream.description}`,
    homepage: `${FORK_URL}/tree/${BRANCH}#readme`,
    repository: { type: "git", url: `git+${FORK_URL}.git`, directory: "packages/webevm" },
    bugs: { url: `${FORK_URL}/issues` },
    albertaBuckSource: { upstream: `webevm@${upstream.version}`, fork: FORK_URL, branch, commit, pr: PR },
  });
  writeFileSync(join(stage, "package.json"), JSON.stringify(pkg, null, "\t") + "\n");

  const packed = JSON.parse(run("npm", ["pack", "--json", "--pack-destination", out], stage));
  const entry = Array.isArray(packed) ? packed[0] : Object.values(packed)[0];
  console.log(`${join(out, entry.filename)}: ${entry.entryCount} files, ${entry.size} bytes, integrity ${entry.integrity}`);
} finally {
  rmSync(stage, { recursive: true, force: true });
}
