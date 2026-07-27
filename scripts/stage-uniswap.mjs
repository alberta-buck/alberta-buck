#!/usr/bin/env node
// SPDX-License-Identifier: GPL-3.0-or-later
//
// Stage the OFFICIAL Uniswap artifacts into out/, in the forge layout that
// vm.deployCode() and our JS/Python artifact loaders already expect.
//
// Why these come from npm rather than from source:
//
//   The Uniswap contracts we deploy are pinned to pre-0.8 pragmas -- v2-core
//   =0.5.16, v2-periphery =0.6.6, v3-core =0.7.6.  Building them here meant
//   three extra compilers, and foundry on arm64 macOS cannot resolve any of
//   them: `forge build` fails with "No solc version exists that matches the
//   version requirement", even with the binaries present in ~/.svm.  The
//   artifacts that used to sit in out/ were stale leftovers from a toolchain
//   that no longer exists on this machine, kept alive only because
//   v2-patch-init-code-hash guards its rebuild behind `test -f`.  A fresh
//   clone -- or CI -- could not reproduce them at all.
//
//   Uniswap publishes the compiled artifacts themselves, version-pinned, on
//   npm.  Taking them from there removes three compilers, two foundry
//   profiles and two compile-trigger directories, and makes the dependency
//   as deterministic as any other locked npm package.  It is also exactly
//   what we tell consumers of alberta-buck-contracts to do: take Uniswap
//   from Uniswap (alberta-buck-deployment.org, P2.5).
//
//   NOTE: this bytecode is NOT the canonical mainnet bytecode -- the npm
//   build used different compiler settings, so UniswapV2Pair hashes to
//   0x9fc9c0c8... rather than mainnet's 0x96e8ac42...  The init-code-hash
//   patch therefore still applies; it just now hashes a pinned artifact
//   instead of the output of a local compile that varied by machine.
//
// Usage:  node scripts/stage-uniswap.mjs [--check]

import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const REPO = dirname(dirname(fileURLToPath(import.meta.url)));
const OUT = join(REPO, "out");

// Every third-party artifact any of our worlds deploys.  Sourced from the
// package that publishes it; the versions are pinned exactly in
// package.json, so this list plus the lockfile fully determines the bytes.
const STAGE = [
  ["@uniswap/v2-core", "build/UniswapV2Factory.json", "UniswapV2Factory"],
  ["@uniswap/v2-core", "build/UniswapV2Pair.json", "UniswapV2Pair"],
  ["@uniswap/v2-periphery", "build/UniswapV2Router02.json", "UniswapV2Router02"],
  ["@uniswap/v2-periphery", "build/WETH9.json", "WETH9"],
  ["@uniswap/v3-core",
   "artifacts/contracts/UniswapV3Factory.sol/UniswapV3Factory.json",
   "UniswapV3Factory"],
  ["@uniswap/v3-core",
   "artifacts/contracts/UniswapV3Pool.sol/UniswapV3Pool.json",
   "UniswapV3Pool"],
];

// The two upstream shapes:
//   v2  (solc combined-json):  { abi, bytecode: "6080..",   evm: {...} }
//   v3  (hardhat):             { abi, bytecode: "0x6080..", deployedBytecode: "0x.." }
// forge artifacts want { abi, bytecode: { object }, deployedBytecode: { object } }.
//
// Note the 0x: v2's combined-json omits it and v3's hardhat output includes
// it.  foundry tolerates both, so the forge suite passes either way -- but
// viem/tevm reject unprefixed hex, and the JS worlds failed with "deploy
// WETH9 reverted" until this normalized the prefix.  Always emit 0x.
function normalize(raw, name, pkg, version) {
  const hex = (v) => {
    const s = typeof v === "string" ? v : v?.object ?? "";
    if (!s) return "";
    return s.startsWith("0x") ? s : `0x${s}`;
  };
  const deployed =
    hex(raw.deployedBytecode) ||
    hex(raw.evm?.deployedBytecode) ||
    "";
  return {
    abi: raw.abi,
    bytecode: { object: hex(raw.bytecode) },
    deployedBytecode: { object: deployed },
    // Provenance, so an artifact in out/ can always be traced to its source.
    // Not a forge field; loaders ignore what they do not read.
    albertaBuckSource: { package: pkg, version, contract: name },
  };
}

function pkgVersion(pkg) {
  return require(`${pkg}/package.json`).version;
}

const check = process.argv.includes("--check");
let staged = 0;
const problems = [];

for (const [pkg, rel, name] of STAGE) {
  let raw, version;
  try {
    version = pkgVersion(pkg);
    raw = JSON.parse(readFileSync(require.resolve(`${pkg}/${rel}`), "utf8"));
  } catch (e) {
    problems.push(`${name}: cannot read ${pkg}/${rel} -- run \`npm install\` (${e.code ?? e.message})`);
    continue;
  }

  const art = normalize(raw, name, pkg, version);
  if (!art.bytecode.object || art.bytecode.object === "0x") {
    problems.push(`${name}: ${pkg}@${version} has no bytecode`);
    continue;
  }

  const dest = join(OUT, `${name}.sol`, `${name}.json`);
  const text = JSON.stringify(art, null, 2) + "\n";

  if (check) {
    if (!existsSync(dest)) {
      problems.push(`${name}: not staged (missing ${dest.slice(REPO.length + 1)})`);
    } else if (readFileSync(dest, "utf8") !== text) {
      problems.push(`${name}: staged copy differs from ${pkg}@${version}`);
    }
  } else {
    mkdirSync(dirname(dest), { recursive: true });
    writeFileSync(dest, text);
  }
  staged += 1;
  const kb = (art.bytecode.object.length / 2 / 1024).toFixed(1);
  if (!check) console.log(`  ${name.padEnd(20)} ${String(pkg + "@" + version).padEnd(34)} ${kb.padStart(6)} KB`);
}

if (problems.length) {
  console.error("stage-uniswap: FAILED");
  for (const p of problems) console.error(`  ${p}`);
  process.exit(1);
}
console.log(check
  ? `stage-uniswap: ${staged} artifacts match their pinned packages`
  : `stage-uniswap: staged ${staged} official artifacts into out/`);
