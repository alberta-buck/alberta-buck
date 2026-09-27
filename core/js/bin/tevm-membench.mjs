#!/usr/bin/env node
// tevm-membench: how much memory does a long eqworld run cost on Tevm, and
// what does each mitigation buy?  (doc/reports/tevm-memory.org)
//
//   nix develop ../.. --command node --expose-gc --max-old-space-size=8192 \
//        bin/tevm-membench.mjs --mode auto --days 180 --every 10 --out auto.jsonl
//
// Modes:
//   auto    the stock session (automine: one block per transaction)
//   prune   automine, and at each day's end drop Tevm's per-block history:
//           the full-state snapshot Tevm stores under every block's state
//           root (all but the last --keep-roots blocks), and the block
//           objects + receipts/tx index older than --keep blocks.  The
//           live state is untouched.
//   reload  automine, and every --reload-every days snapshot the state
//           (snapshotTevm), load it into a FRESH client and swap it into
//           the running Session; --height full rebuilds the block height
//           (restoreTevm, mining empty blocks), --height skip keeps only
//           the timestamp.  A before/after probe checks the world reads
//           back identically.
//   --trim-system (prune, reload): also clear the storage of the two
//           system contracts that grow by 3 slots per block under Tevm's
//           default hardfork (EIP-4788 beacon roots, EIP-2935 block-hash
//           history); nothing in this world reads them.
//   empty   no world: an empty chain mining --days x 100 empty blocks, to
//           show the per-block cost with nothing deployed, and which
//           system contracts grow storage per block.
//   readcost no world: one MockERC20 on an otherwise empty chain; mine up
//           to --days x 100 empty blocks (pruning history between chunks)
//           and time an eth_call as the system rings fill, then trim them.
//   batch   a probe of manual mining (one block for many txs): builds the
//           world, switches the node to manual mining, and times reads
//           at 'latest' vs 'pending' and one k-tx block vs k 1-tx blocks;
//           then a TWAP probe: a day's clock jump plus a basket-pool swap,
//           mined as separate blocks (automine) and as ONE block (manual),
//           reading the pool's 600 s TWAP tick against the pre/post ticks.
//
// The run is made reproducible (fixed genesis-relative clock, fixed agent
// keys) so the final world.series digest can be compared across modes:
// equal digests mean the mitigation changed nothing the agents observe.
//
// Samples are JSON lines (stdout, and --out if given); heap figures are
// taken after a forced GC when node runs with --expose-gc.

import { parseArgs } from "node:util";
import { appendFileSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { bytesToHex, keccak256, toBytes, toHex } from "viem";
import { privateKeyToAccount } from "viem/accounts";

import { tevmSession, snapshotTevm, restoreTevm } from "../src/backends.js";
import { advanceTime, DAY } from "../src/buckworld.js";
import { encodePath, urExecArgs } from "../src/router.js";
import { loadArtifact, repoRoot } from "../src/nodefs.js";
import { buildEquilibriumWorld, DayClock, PidKeeper, MonthlyIncome }
  from "../src/scenarios/eqworld.js";
import { PinWhale } from "../src/agents/whale.js";
import { ParityArb } from "../src/agents/arb.js";
import { BasketSaver } from "../src/agents/saver.js";
import { MortgageRetiree } from "../prototypes/mortgage-retiree.js";
import { runDays } from "../src/world.js";
import id from "../src/identity.js";

const { values: a } = parseArgs({ options: {
  mode:          { type: "string", default: "auto" },  // auto|prune|reload|batch|empty|readcost
  days:          { type: "string", default: "60" },
  every:         { type: "string", default: "10" },    // sample interval, days
  tokens:        { type: "string", default: "1" },     // basket constituents 1..6
  keep:          { type: "string", default: "256" },   // prune: blocks kept
  "keep-roots":  { type: "string", default: "8" },     // prune: state snapshots kept
  "reload-every": { type: "string", default: "30" },   // reload: days
  height:        { type: "string", default: "skip" },  // reload: full|skip
  "stop-heap":   { type: "string", default: "0" },     // MB; 0 = no guard
  "batch-k":     { type: "string", default: "50" },    // batch: txs per block
  "arb-bp":      { type: "string", default: "100" },   // ParityArb threshold (busier < 100)
  "trim-system": { type: "boolean", default: false },  // prune/reload: clear EIP-4788/2935 ring storage
  out:           { type: "string", default: "" },
} });

const DAYS = Number(a.days);
const EVERY = Number(a.every);
const MB = 1024 * 1024;
const T0 = 1_900_000_000;     // pinned chain clock origin (2030-03-17)

// Up to six constituents (the demo's commodity basket shape).  p0 is USDC
// base units per whole token; all 18-dec.
const COMMODITIES = [
  { sym: "CNST", name: "Construction", dec: 18, p0: 2_500_000n },
  { sym: "NRGY", name: "Energy",       dec: 18, p0: 70_000_000n },
  { sym: "BULN", name: "Bullion",      dec: 18, p0: 2_400_000_000n },
  { sym: "FOOD", name: "Food",         dec: 18, p0: 6_000_000n },
  { sym: "METL", name: "Metals",       dec: 18, p0: 9_000_000n },
  { sym: "LABR", name: "Labour",       dec: 18, p0: 35_000_000n },
];

function emit(rec) {
  const line = JSON.stringify(rec, (_, v) => (typeof v === "bigint" ? v.toString() : v));
  console.log(line);
  if (a.out) appendFileSync(a.out, line + "\n");
}

const gc = globalThis.gc ?? (() => {});
const node = (session) => session.client.transport.tevm;

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

/** Tevm's retained history, by kind. */
async function tevmInternals(session, { dumpSize = false } = {}) {
  const n = node(session);
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
  const n = node(session);
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

/** Everything the agents observe, for before/after-reload equality. */
async function probe(world, holders) {
  const s = world.session;
  const head = await s.client.getBlock();
  const [K, bvib, spotUB, ff] = await Promise.all([
    world.K(), world.bvib(), world.spotUB(), s.call(world.kctrl, "fundingFactor")]);
  const spots = await Promise.all(world.tokens.map((_, i) => world.spotUsd(i)));
  const spotsBuck = await Promise.all(world.tokens.map((_, i) => world.spotBuck(i)));
  const bals = [];
  for (const h of holders) {
    bals.push(await s.call(world.buck, "signedBalanceOf", [h]));
    bals.push(await s.call(world.usdc, "balanceOf", [h]));
  }
  const supply = await s.call(world.buck, "totalSupply");
  return { timestamp: head.timestamp, K, bvib, spotUB, ff, spots, spotsBuck,
           bals, supply };
}

const same = (x, y) => JSON.stringify(x, (_, v) => (typeof v === "bigint" ? v.toString() : v))
                     === JSON.stringify(y, (_, v) => (typeof v === "bigint" ? v.toString() : v));

/** Swap a fresh Tevm client (restored from a snapshot) into `session`. */
async function reload(world, holders, height) {
  const session = world.session;
  const before = await probe(world, holders);
  const t0 = performance.now();
  const snap = await snapshotTevm(session);
  const snapChars = JSON.stringify(snap,
    (_, v) => (typeof v === "bigint" ? v.toString() : v)).length;
  if (a["trim-system"]) {
    for (const [addr, acct] of Object.entries(snap.state)) {
      if (SYSTEM_RINGS.some((r) => r.toLowerCase() === addr.toLowerCase())) acct.storage = {};
    }
  }
  const fresh = await tevmSession();
  if (height === "full") {
    await restoreTevm(fresh, snap);
  } else {
    const c = fresh.client;
    await c.tevmLoadState({ state: snap.state });
    await c.request({ method: "evm_setNextBlockTimestamp", params: [Number(snap.timestamp)] });
    await c.request({ method: "evm_mine", params: [] });
  }
  session.client = fresh.client;          // handles are {abi, address}: all stay valid
  const ms = Math.round(performance.now() - t0);
  gc(); gc();
  const heapAfterMB = +(process.memoryUsage().heapUsed / MB).toFixed(1);
  const after = await probe(world, holders);
  // Time must match exactly; the chain carries on at the same clock.
  return { ms, heapAfterMB, snapMB: +(snapChars / MB).toFixed(2), ok: same(before, after),
           fromBlock: Number(snap.block),
           toBlock: Number((await session.client.getBlock()).number) };
}

// ---------------------------------------------------------------------------

if (a.mode === "empty") {
  const { createMemoryClient } = await import("tevm");
  const c = createMemoryClient();
  await c.tevmReady();
  const nd = c.transport.tevm;
  const show = async (label, t0) => {
    gc(); gc();
    const vm = await nd.getVm();
    const bs = vm.stateManager._baseState;
    const cur = bs.stateRoots.get(bs.getCurrentStateRoot());
    const grown = Object.entries(cur)
      .map(([addr, v]) => [addr, Object.keys(v.storage ?? {}).length])
      .filter(([, k]) => k > 0);
    emit({ mode: "empty", phase: label, block: Number(await c.getBlockNumber()),
           roots: bs.stateRoots.size, heapMB: +(process.memoryUsage().heapUsed / MB).toFixed(1),
           dumpKB: Math.round(JSON.stringify(cur,
             (_, v) => (typeof v === "bigint" ? v.toString() : v)).length / 1024),
           storage: grown, ms: t0 ? Math.round(performance.now() - t0) : 0 });
  };
  await show("start");
  const N = DAYS * 100;
  let t = performance.now();
  await c.tevmMine({ blockCount: N });
  await show(`tevmMine blockCount=${N}`, t);
  t = performance.now();
  for (let i = 0; i < N; i++) await c.request({ method: "evm_mine", params: [] });
  await show(`evm_mine x${N}`, t);
  process.exit(0);
}

if (a.mode === "readcost") {
  const s = await tevmSession();
  const tok = await s.deploy(loadArtifact("MockERC20"), ["Probe", "PRB", 18], { name: "Probe" });
  const readMs = async (reps = 10) => {
    const t = performance.now();
    for (let i = 0; i < reps; i++) await s.call(tok, "balanceOf", [s.account.address]);
    return +((performance.now() - t) / reps).toFixed(1);
  };
  const report = async (phase) => {
    gc(); gc();
    const i = await tevmInternals(s, { dumpSize: true });
    emit({ mode: "readcost", phase, block: Number(await s.client.getBlockNumber()),
           readMs: await readMs(), slots: i.slots, dumpKB: i.dumpKB, roots: i.roots,
           heapMB: +(process.memoryUsage().heapUsed / MB).toFixed(1) });
  };
  const N = DAYS * 100;
  for (const target of [1, 1000, 2000, 4000, 8200, N].filter((x, j, arr) => x <= N && arr.indexOf(x) === j)) {
    let b = Number(await s.client.getBlockNumber());
    while (b < target) {
      const n = Math.min(500, target - b);
      await s.client.tevmMine({ blockCount: n });
      await pruneTevm(s, { keep: 256, keepRoots: 8 });
      b += n;
    }
    await report(`mined+pruned`);
  }
  await trimSystemRings(s.client);
  await report("trimmed");
  process.exit(0);
}

const session = await tevmSession();
await session.client.request({ method: "evm_setNextBlockTimestamp", params: [T0] });
await session.client.request({ method: "evm_mine", params: [] });

let sends = 0;
for (const fn of ["send", "deploy"]) {
  const orig = session[fn].bind(session);
  session[fn] = (...args) => { sends++; return orig(...args); };
}

const urArtifact = JSON.parse(readFileSync(
  join(repoRoot(), "alberta_buck", "sim", "artifacts", "UniversalRouter.json"), "utf8"));
const nTok = Math.max(1, Math.min(6, Number(a.tokens)));
const tBuild = performance.now();
const world = await buildEquilibriumWorld(session, loadArtifact, {
  identity: id, urArtifact, tokens: COMMODITIES.slice(0, nTok),
  feedSeed: 61445, stepBp: 80 });

const key = (label) => keccak256(toBytes(`tevm-membench:${label}`));
const debtorAcct = privateKeyToAccount(key("debtor"));
const saver = new BasketSaver({ account: privateKeyToAccount(key("saver")),
  budget: 25_000n * 10n ** 6n, holdDays: 60 });
const debtor = new MortgageRetiree({
  house: 400_000n * 10n ** 6n, mortgageBp: 550n, premiumBp: 50n, aggrBp: 0n,
  payment: 3_000n * 10n ** 6n, account: debtorAcct });
const agents = [
  new DayClock(),
  ...world.tokens.map((t, i) => new PinWhale({
    simlp: world.simlp, pool: t.poolUsdc, token: t.erc20.address,
    tokenDec: t.dec, quote: world.usdc.address,
    refPrice: (d) => world.feeds[i][d % world.feeds[i].length] })),
  new ParityArb({ thresholdBp: BigInt(a["arb-bp"]) }),
  new PidKeeper(),
  saver,
  new MonthlyIncome({ account: debtorAcct, amount: 3_000n * 10n ** 6n }),
  debtor,
];
const buildMs = Math.round(performance.now() - tBuild);

async function sample(day, extra = {}) {
  gc(); gc();
  const m = process.memoryUsage();
  const head = await session.client.getBlock();
  const internals = await tevmInternals(session, { dumpSize: true });
  return {
    mode: a.mode, tokens: nTok, day, block: Number(head.number), sends,
    heapMB: +(m.heapUsed / MB).toFixed(1), rssMB: +(m.rss / MB).toFixed(1),
    extMB: +(m.external / MB).toFixed(1), abMB: +(m.arrayBuffers / MB).toFixed(1),
    ...internals, ...extra,
  };
}

// ---- batch: the manual-mining probe (mitigation (a)) ----------------------
if (a.mode === "batch") {
  for (const ag of agents) if (ag.setup) await ag.setup(world);
  const n = node(session);
  const k = Number(a["batch-k"]);
  const me = session.account;
  const c = session.client;
  const timeIt = async (f, reps = 5) => {
    const t = performance.now();
    for (let i = 0; i < reps; i++) await f();
    return +((performance.now() - t) / reps).toFixed(1);
  };
  const read = (blockTag) => c.readContract({ address: world.kctrl.address,
    abi: world.kctrl.abi, functionName: "buckK", ...(blockTag ? { blockTag } : {}) });
  emit(await sample(0, { phase: "built", buildMs }));

  // TWAP probe: jump a day, push the TOKEN/BUCK basket pool with a
  // USDC -> BUCK -> TOKEN route, read slot0 + the basket's 600 s window.
  const t0k = world.tokens[0];
  const twapWin = Number(await session.call(world.basket, "twapWindow"));
  const tick = async () => Number((await session.call(t0k.poolBuck, "slot0"))[1]);
  const twapTick = async () => {
    const [cum] = await session.call(t0k.poolBuck, "observe", [[twapWin, 0]]);
    return Math.floor(Number(cum[1] - cum[0]) / twapWin);
  };
  const swapArgs = (amt) => urExecArgs(me.address, amt, encodePath(
    [world.usdc.address, world.fees.ub, world.buck.address, world.fees.buck, t0k.erc20.address]));
  const AMT = 400_000n * 10n ** 6n;
  const twapProbe = async (label, batched) => {
    const head = await c.getBlock();
    const pre = await tick();
    if (!batched) {
      await advanceTime(world, DAY);
      await session.send(world.usdc, "transfer", [world.router.address, AMT], { tag: "twap:fund" });
      await session.send(world.router, "execute", swapArgs(AMT), { tag: "twap:swap", gas: 1_500_000n });
    } else {
      await c.request({ method: "evm_setNextBlockTimestamp",
                        params: [Number(head.timestamp) + DAY] });
      let nn = await c.getTransactionCount({ address: me.address });
      await c.writeContract({ address: world.usdc.address, abi: world.usdc.abi,
        functionName: "transfer", args: [world.router.address, AMT], account: me,
        gas: 300_000n, nonce: nn++, chain: null });
      await c.writeContract({ address: world.router.address, abi: world.router.abi,
        functionName: "execute", args: swapArgs(AMT), account: me,
        gas: 1_500_000n, nonce: nn++, chain: null });
      await c.tevmMine();
    }
    const blk = await c.getBlock();
    const post = await tick();
    const tw = await twapTick();
    const out = { phase: `twap:${label}`, window: twapWin, preTick: pre, postTick: post,
      twapTick: tw, twapFromPre: tw - pre, twapFromPost: tw - post,
      blocks: Number(blk.number - head.number),
      secondsIntoDay: Number(blk.timestamp - head.timestamp) - DAY };
    // One more day later (a quiet block), the window has caught up.
    if (!batched) await advanceTime(world, DAY);
    else {
      await c.request({ method: "evm_setNextBlockTimestamp",
                        params: [Number(blk.timestamp) + DAY] });
      await c.tevmMine();
    }
    out.nextDayTwapFromPost = (await twapTick()) - post;
    emit(out);
  };
  await twapProbe("automine", false);

  // k one-tx blocks (automine) for reference.
  let s0 = await sample(0, { phase: "pre-auto" });
  for (let i = 0; i < k; i++) {
    await session.send(world.usdc, "mint", [me.address, 1n], { tag: "probe:auto" });
  }
  let s1 = await sample(0, { phase: `auto:${k}tx`, readLatestMs: await timeIt(() => read()) });
  emit({ ...s1, dHeapKBperTx: Math.round((s1.heapMB - s0.heapMB) * 1024 / k),
         dBlocks: s1.block - s0.block });

  // k txs into ONE block (manual mining).
  n.setMiningConfig({ type: "manual" });
  s0 = await sample(0, { phase: "pre-manual" });
  let nonce = await c.getTransactionCount({ address: me.address });
  const hashes = [];
  const tSub = performance.now();
  for (let i = 0; i < k; i++) {
    hashes.push(await c.writeContract({ address: world.usdc.address, abi: world.usdc.abi,
      functionName: "mint", args: [me.address, 1n], account: me, gas: 200_000n,
      nonce: nonce++, chain: null }));
    if (i === 0 || i === k - 1) {
      emit({ phase: `manual:pending=${i + 1}`,
             readLatestMs: await timeIt(() => read(), 3),
             readPendingMs: await timeIt(() => read("pending"), 3),
             receiptBeforeMine: await c.request({ method: "eth_getTransactionReceipt",
                                                  params: [hashes[i]] }) });
    }
  }
  const subMs = Math.round(performance.now() - tSub);
  const tMine = performance.now();
  await c.tevmMine();
  const mineMs = Math.round(performance.now() - tMine);
  const blk = await c.getBlock();
  const r = await c.getTransactionReceipt({ hash: hashes[k - 1] });
  s1 = await sample(0, { phase: `manual:${k}tx`, subMs, mineMs,
                         txsInBlock: blk.transactions.length, lastStatus: r.status });
  emit({ ...s1, dHeapKBperTx: Math.round((s1.heapMB - s0.heapMB) * 1024 / k),
         dBlocks: s1.block - s0.block });
  await twapProbe("one-block", true);
  process.exit(0);
}

// ---- the day loop (auto / prune / reload) ---------------------------------
emit(await sample(0, { phase: "built", buildMs }));
const holders = [];
let tLast = performance.now();
let lastDay = 0;
const reloads = [];
const keep = Number(a.keep);
const keepRoots = Number(a["keep-roots"]);
const reloadEvery = Number(a["reload-every"]);
const stopHeap = Number(a["stop-heap"]);
let pruneMs = 0;

class Stop extends Error {}

try {
  await runDays(world, agents, { days: DAYS, onDay: async (d) => {
    await world.record(d);
    const done = d + 1;
    if (holders.length === 0) {
      holders.push(saver.account.address, world.holderAddress(debtorAcct), world.simlp.address);
    }
    if (a.mode === "prune") {
      const t = performance.now();
      await pruneTevm(session, { keep, keepRoots });
      if (a["trim-system"]) await trimSystemRings(session.client);
      pruneMs += performance.now() - t;
    }
    let extra = {};
    if (a.mode === "reload" && done % reloadEvery === 0) {
      const r = await reload(world, holders, a.height);
      reloads.push({ day: done, ...r });
      extra = { reload: r };
    }
    if (done % EVERY === 0 || done === DAYS) {
      const now = performance.now();
      const s = await sample(done, {
        secPerDay: +((now - tLast) / 1000 / (done - lastDay)).toFixed(2),
        ...(a.mode === "prune" ? { pruneMsTotal: Math.round(pruneMs) } : {}),
        ...extra });
      emit(s);
      tLast = performance.now();
      lastDay = done;
      if (stopHeap && s.heapMB > stopHeap) throw new Stop(`heap ${s.heapMB} MB > ${stopHeap}`);
    }
  } });
} catch (e) {
  if (!(e instanceof Stop)) throw e;
  emit({ mode: a.mode, stopped: e.message });
}

const digest = keccak256(toHex(JSON.stringify(world.series,
  (_, v) => (typeof v === "bigint" ? v.toString() : v))));
const last = world.series[world.series.length - 1];
emit({ mode: a.mode, final: true, days: world.series.length, sends,
       mismatches: session.mismatches.length, seriesDigest: digest,
       K: Number(last.K) / 1e18, bvib: Number(last.bvib) / 1e18,
       reloadsOk: reloads.every((r) => r.ok), reloads: reloads.length });
process.exit(0);
