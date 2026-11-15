// The in-process EVM for the browser worlds and the JS simulations: webevm (an execution-only
// EIP-1193 node on @ethereumjs/vm, github.com/wighawag/webevm) running the revm-wasm engine. It
// replaced Tevm for the sandbox (doc/review/bucknet-economy-implementation.org, "In-browser
// engine"): the same worlds run about a thousand times faster, because a read executes against
// one flat state instead of a copy of it.
//
//   const session = await webevmSession({ wasm: "./wasm/revm.wasm" });     // a browser page
//   const session = await webevmSession({ wasm: moduleOrBytes });           // Node (nodefs.js)
//   const snap = await snapshotWebevm(session);   // JSON-safe: state, blocks, receipts, logs
//   await restoreWebevm(await webevmSession(...), snap);
//
// Browser-safe: no node: imports, and no Tevm (a bundle of this module does not carry it). The
// dependency is the fork that adds the node's clock (evm_setNextBlockTimestamp, evm_mine, the
// block timestamp interval), vendored as core/js/vendor/webevm-*.tgz until upstream releases it.
// Every block is its parent's timestamp plus one second unless a day is advanced, so a world's
// timestamps never depend on how fast it ran.

import { createWalletClient, custom, publicActions } from "viem";
import { privateKeyToAccount } from "viem/accounts";

import { DEV_KEYS, devAccount } from "./dev-keys.js";
import { Session } from "./session.js";

export const WEBEVM_CHAIN_ID = 31337;
/** Between two blocks, as on Ethereum (its 12 s slot): a tick's transactions follow its clock 12 s
 *  apart, so a pool's oracle sees one observation per block (the Python worlds' BLOCK_SECONDS). */
export const BLOCK_SECONDS = Number(globalThis.process?.env?.SIM_BLOCK_SECONDS ?? 12);
const DEV_BALANCE = 10n ** 24n;

/**
 * A fresh in-process chain with the dev accounts funded, wrapped in a Session.
 *
 * @param opts.engine "revm" (default) or "ethereumjs" (webevm's own interpreter, no wasm)
 * @param opts.wasm   revm's module for "revm": a URL (string or URL), bytes, a Response, or a
 *                    compiled WebAssembly.Module (Node: nodefs.js revmWasm())
 * @param opts.nodeOptions passed to webevm's createNode (e.g. a block gas limit)
 * @param opts.blockSeconds between two auto-mined blocks (BLOCK_SECONDS, Ethereum's 12 s slot); a
 *        time warp still sets a block's timestamp exactly
 */
export async function webevmSession({ accountIndex = 0, journal = null, engine = "revm", wasm,
                                      nodeOptions = {}, blockSeconds = BLOCK_SECONDS } = {}) {
  const { createNode } = await import("webevm");
  let eng;
  if (engine === "revm") {
    if (wasm === undefined) throw new Error("webevmSession: the revm engine needs its wasm (opts.wasm)");
    const { createRevmEngine } = await import("webevm/revm");
    eng = await createRevmEngine({ wasm });
  } else if (engine !== "ethereumjs") {
    throw new Error(`webevmSession: engine is "revm" or "ethereumjs", not ${engine}`);
  }
  const initialBalances = Object.fromEntries(
    DEV_KEYS.map((k) => [privateKeyToAccount(k).address, DEV_BALANCE]));
  const node = await createNode({ chainId: WEBEVM_CHAIN_ID, miningConfig: { type: "auto" }, initialBalances,
                                  ...(eng ? { engine: eng } : {}), ...nodeOptions });
  await node.request({ method: "anvil_setBlockTimestampInterval", params: [blockSeconds] });
  // node.request is looked up at each call, so a layer may wrap it later (savings-world.js acts
  // as a visitor it enrolls by routing that visitor's sends to evm_sendRawTransactionAs).
  const client = createWalletClient({ transport: custom({ request: (args) => node.request(args) }) })
    .extend(publicActions);
  const session = new Session(client, { account: devAccount(accountIndex), journal });
  session.webevm = node;
  return session;
}

/** The whole chain as JSON-safe data: state, every block, receipt and log (webevm's dumpState). */
export async function snapshotWebevm(session) {
  if (!session.webevm) throw new Error("snapshotWebevm: not a webevm session");
  return { webevm: await session.webevm.dumpState() };
}

/**
 * Load snapshotWebevm() into a FRESH webevm session: the same state, blocks and clock (the
 * head's timestamp; the session's one-second interval carries on from it).
 */
export async function restoreWebevm(session, snap) {
  if (!session.webevm) throw new Error("restoreWebevm: not a webevm session");
  if (!snap?.webevm) throw new Error("restoreWebevm: not a webevm snapshot");
  await session.webevm.loadState(snap.webevm);
  return session;
}
