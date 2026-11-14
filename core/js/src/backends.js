// Session constructors for the two Phase 1 backends.
//
//   anvilSession(url)  -- join a running anvil (e.g. one a Python sim
//                         deployed into): HTTP transport, zero-fee legacy
//                         txs matching the sim's anvil profile.
//   tevmSession()      -- standalone in-process EVM (Tevm MemoryClient);
//                         the same session/agents run wholly in JS.
//
// Both sign with well-known anvil/tevm dev accounts (mnemonic "test test
// ... junk"), so one code path -- local-account raw txs -- serves both.

import { bytesToHex, createPublicClient, createWalletClient, http, publicActions } from "viem";
import { devAccount } from "./dev-keys.js";

import { Session } from "./session.js";

export { DEV_KEYS, devAccount } from "./dev-keys.js";

/** Join a running anvil node (the Python sim's zero-fee profile). */
export function anvilSession(url, { accountIndex = 9, journal = null } = {}) {
  const client = createWalletClient({ transport: http(url) })
    .extend(publicActions);
  return new Session(client, {
    account: devAccount(accountIndex),
    journal,
    txOverrides: { gasPrice: 0n },   // anvil runs --base-fee 0 --gas-price 0
  });
}

/** Standalone in-process EVM. Import lazily: tevm is a heavy dependency. */
export async function tevmSession({ accountIndex = 0, journal = null } = {}) {
  const { createMemoryClient } = await import("tevm");
  const client = createMemoryClient();
  return new Session(client, {
    account: devAccount(accountIndex),
    journal,
  });
}

/**
 * A Tevm chain as data: every account's state (hex strings, JSON-safe) and
 * the clock -- the block number and timestamp, which Tevm's own dump omits.
 * Blocks, transactions and logs are NOT kept: a restored chain has the
 * state and the time, not the history.
 */
export async function snapshotTevm(session) {
  const { state } = await session.client.tevmDumpState();
  const head = await session.client.getBlock();
  return { state, block: head.number, timestamp: head.timestamp };
}

/**
 * Load snapshotTevm() into a FRESH tevm session and put its clock back:
 * the same block number and timestamp, so contract clocks (demurrage,
 * depreciation, the controller's cadence) carry on where they stopped.
 * Rebuilding the height mines empty blocks, one per second of past time,
 * ending on the snapshot's own timestamp.
 */
export async function restoreTevm(session, snap) {
  const c = session.client;
  await c.tevmLoadState({ state: snap.state });
  const block = Number(snap.block);
  const ts = Number(snap.timestamp);
  if (block > 1) {
    await c.request({ method: "evm_setNextBlockTimestamp", params: [ts - (block - 1)] });
    await c.tevmMine({ blockCount: block - 1, interval: 1 });
  }
  if (block > 0) {
    await c.request({ method: "evm_setNextBlockTimestamp", params: [ts] });
    await c.request({ method: "evm_mine", params: [] });
  }
  return session;
}

// --- Tevm's retained history (doc/reports/tevm-memory.org) ----------------------------------------
// Tevm keeps a full copy of the world state under every mined block's state root and, under
// automine, mines a block per transaction; every read deep-copies the VM, so its cost grows with
// the state. Pruning old roots and trimming the two system rings at each day keeps both flat.

// Cancun's EIP-4788 (2 slots/block) and Prague's EIP-2935 (1 slot/block)
// ring buffers: 8191-entry rings, so up to 24,573 slots of live state that
// every Tevm deepCopy (each eth_call, each mined block) dumps and copies.
export const SYSTEM_RINGS = [
  "0x000F3df6D732807Ef1319fB7B8bB8522d0Beac02",   // EIP-4788 beacon roots
  "0x0000F90827F1C53a10cb7A02335B175320002935",   // EIP-2935 history
];

/** Clear the system rings' storage (code, nonce, balance kept). */
export async function trimSystemRings(client) {
  for (const address of SYSTEM_RINGS) await client.tevmSetAccount({ address, state: {} });
}

/** Tevm's retained history, by kind (doc/reports/tevm-memory.org). */
export async function tevmRetained(session, { dumpSize = false } = {}) {
  const n = session.client.transport.tevm;
  const vm = await n.getVm();
  const bs = vm.stateManager._baseState;
  const rm = await n.getReceiptsManager();
  const pool = await n.getTxPool();
  const out = {
    roots: bs.stateRoots.size,
    chainBlocks: vm.blockchain.blocksByNumber.size,
    rcptKeys: rm.mapDb._cache?.size ?? -1,
    poolHandled: pool.handled?.size ?? -1,
  };
  if (dumpSize) {
    const cur = bs.stateRoots.get(bs.getCurrentStateRoot());
    let accounts = 0, slots = 0;
    for (const v of Object.values(cur ?? {})) {
      accounts += 1;
      slots += Object.keys(v.storage ?? {}).length;
    }
    out.dumpKB = Math.round(JSON.stringify(cur,
      (_, v) => (typeof v === "bigint" ? v.toString() : v)).length / 1024);
    out.accounts = accounts;
    out.slots = slots;
  }
  return out;
}

/**
 * Drop Tevm's per-block history: the full-state snapshot keyed by each
 * block's state root (all but the last `keepRoots` blocks -- only calls
 * AT an old block need them), and the block objects + receipts + tx-hash
 * index older than `keep` blocks (>= 256 keeps BLOCKHASH and parent
 * lookups whole).  Genesis, every tagged block and the live state root
 * are always kept.  Returns how many state snapshots were dropped.
 */
export async function pruneTevm(session, { keep = 256, keepRoots = 8 } = {}) {
  const n = session.client.transport.tevm;
  const vm = await n.getVm();
  const chain = vm.blockchain;
  const bs = vm.stateManager._baseState;
  const rm = await n.getReceiptsManager();
  const head = chain.blocksByTag.get("latest").header.number;
  const floor = head - BigInt(keep);
  const rootFloor = head - BigInt(keepRoots);
  const live = new Set([bs.getCurrentStateRoot()]);
  const drop = [];
  for (const [num, blk] of chain.blocksByNumber) {
    if (num === 0n || num > rootFloor) live.add(bytesToHex(blk.header.stateRoot));
    if (num !== 0n && num <= floor) drop.push(blk);
  }
  for (const blk of chain.blocksByTag.values()) live.add(bytesToHex(blk.header.stateRoot));
  for (const blk of drop) {
    chain.blocksByNumber.delete(blk.header.number);
    await rm.deleteReceipts(blk);
  }
  for (const [h, blk] of chain.blocks) {
    const num = blk.header.number;
    if (num !== 0n && num <= floor) chain.blocks.delete(h);
  }
  let dropped = 0;
  for (const r of [...bs.stateRoots.keys()]) {
    if (!live.has(r)) { bs.stateRoots.delete(r); dropped++; }
  }
  // The pool's seen-hash map only dedupes resubmissions of mined txs.
  const pool = await n.getTxPool();
  if (pool.txsInPool === 0) pool.handled?.clear();
  return dropped;
}
