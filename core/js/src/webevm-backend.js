// A standalone in-process EVM on webevm (an execution-only, in-browser EIP-1193 node built on
// @ethereumjs/vm, github.com/wighawag/webevm), optionally with its revm-wasm engine: the
// candidate replacement for Tevm (doc/review/bucknet-economy-implementation.org, "In-browser
// engine"). Node-only for now (reads the wasm from disk); a browser build passes the wasm URL.
//
//   const session = await webevmSession({ engine: "revm" });   // or "ethereumjs"
//
// WEBEVM_DIR names a built checkout's packages/webevm (the fork carrying the block clock
// cheats); without it the installed "webevm" package is used. Every block is its parent plus one
// second (evm_setBlockTimestampInterval), so a world's timestamps do not depend on how fast it
// ran; days still move by evm_setNextBlockTimestamp + evm_mine, as on anvil and Tevm.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { createWalletClient, custom, publicActions } from "viem";
import { privateKeyToAccount } from "viem/accounts";

import { DEV_KEYS, devAccount } from "./backends.js";
import { Session } from "./session.js";

export const WEBEVM_CHAIN_ID = 31337;

export async function webevmSession({ accountIndex = 0, journal = null, engine = "revm",
                                      dir = process.env.WEBEVM_DIR, nodeOptions = {} } = {}) {
  const from = (sub, pkg) => dir ? import(pathToFileURL(join(dir, sub)).href) : import(pkg);
  const { createNode } = await from("dist/index.js", "webevm");
  let eng;
  if (engine === "revm") {
    const { createRevmEngine } = await from("dist/revm.js", "webevm/revm");
    const wasmPath = dir ? join(dir, "node_modules", "revm-wasm", "wasm", "revm.wasm")
      : new URL(await import.meta.resolve("revm-wasm/wasm-url")).pathname.replace(/dist\/wasm-url\.js$/, "wasm/revm.wasm");
    eng = await createRevmEngine({ wasm: await WebAssembly.compile(readFileSync(wasmPath)) });
  } else if (engine !== "ethereumjs") {
    throw new Error(`webevmSession: engine is "revm" or "ethereumjs", not ${engine}`);
  }
  const initialBalances = Object.fromEntries(DEV_KEYS.map((k) => [privateKeyToAccount(k).address, 10n ** 24n]));
  const node = await createNode({ chainId: WEBEVM_CHAIN_ID, miningConfig: { type: "auto" }, initialBalances,
                                  ...(eng ? { engine: eng } : {}), ...nodeOptions });
  await node.request({ method: "evm_setBlockTimestampInterval", params: [1] });
  const client = createWalletClient({ transport: custom(node) }).extend(publicActions);
  const session = new Session(client, { account: devAccount(accountIndex), journal });
  session.webevm = node;
  return session;
}
