// The savings world in a Web Worker (doc/review/bucknet-economy-implementation.org, "The savings
// world in the tab"): webevm + revm and the SavingsWorld (src/scenarios/savings-world.js), run a day
// at a time, the page's LocalLink (local-link.js) on the other end of postMessage. The host plays the
// Python server's part (alberta_buck/sim/server.py): it applies control ops and answers JSON-RPC
// batches only BETWEEN days (its chain lock), so a visitor's deposit never lands inside an agent's
// day; it keeps every frame, so a reconnecting page redraws its charts; and it answers sim_status
// from its own state.
//
// Messages in:  {type: "start", sets}        build the world (once; "reset" rebuilds it)
//               {type: "op", id, op}          {op: pause|resume|step|pace|shock|reset|stop, ...}
//               {type: "rpc", id, reqs}       a JSON-RPC batch
// Messages out: {type: "state", state, detail}    building | live | done | failed
//               {type: "frame", frame}             one day (and, on start, every day so far)
//               {type: "reset"}                    the world starts over from its first day
//               {type: "op", id, reply} / {type: "rpc", id, reply}

import { webevmSession } from "../../../src/webevm.js";
import { loadIdentity } from "../../../src/identity-web.js";
import { artifact } from "../../../artifacts/bundle.mjs";
import { SavingsWorld, SAVINGS_NODE_OPTIONS } from "../../../src/scenarios/savings-world.js";
// The economy's TOKENs and price paths, as the Python world resolves them (make sandbox-artifacts:
// alberta_buck.sim.savings_prices).
import prices from "../../../artifacts/savings-prices.json";

const host = {
  world: null, sets: {}, frames: [], day: 0,
  paused: false, pace: 1, stepLeft: 0, done: false, ops: 0, last: null, error: null,
  rpc: [], control: [], wake: null, generation: 0,
};
let identity = null;

const post = (m) => self.postMessage(m);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const waitForWork = () => new Promise((r) => { host.wake = r; });
const wake = () => { const w = host.wake; host.wake = null; w?.(); };

function status() {
  return { day: host.day, paused: host.paused, pace: host.pace, step_left: host.stepLeft > 0 ? host.stepLeft - 1 : null,
           done: host.done,
           chain: null, run: null, last: host.last, error: host.error, ops: host.ops,
           queued: host.control.length, local: true };
}

async function build() {
  identity ??= await loadIdentity("./wasm/buck_identity_bg.wasm");
  const session = await webevmSession({ wasm: "./wasm/revm.wasm", nodeOptions: SAVINGS_NODE_OPTIONS });
  const world = new SavingsWorld({ session, identity, artifacts: artifact, urArtifact: artifact("UniversalRouter"),
                                   sets: host.sets, prices });
  await world.boot();
  return world;
}

/** Control ops, as the server's (server.py _apply): applied between days. */
function apply({ op, ...args }) {
  if (op === "pace") host.pace = Math.max(0, Number(args.days_per_second ?? 0));
  else if (op === "pause") host.paused = true;
  else if (op === "resume") { host.paused = false; host.stepLeft = 0; }
  else if (op === "step") { host.stepLeft = Math.max(1, Math.floor(Number(args.days ?? 1))); host.paused = false; }
  else if (op === "shock") host.world?.shock.arm(args.side, Number(args.usd ?? 0), Number(args.days ?? 1));
  else if (op === "stop") host.done = true;
  else if (op === "reset") return "reset";
  else return `${op} is not available in a world in this tab`;
  return null;
}

async function serve() {
  while (host.control.length) {
    const { id, op } = host.control.shift();
    const err = apply(op);
    host.ops += 1;
    host.last = { op: op.op, ok: !err || err === "reset", note: err && err !== "reset" ? err : "", day: host.day };
    post({ type: "op", id, reply: err && err !== "reset" ? { ok: false, err } : { ok: true } });
    if (err === "reset") return "reset";
  }
  while (host.rpc.length) {
    const { id, reqs } = host.rpc.shift();
    const status_ = reqs.map((r) => (r.method === "sim_status" ? { jsonrpc: "2.0", id: r.id, result: status() } : null));
    const rest = reqs.filter((_, i) => !status_[i]);
    const answered = host.world ? await host.world.rpc(rest)
      : rest.map((r) => ({ jsonrpc: "2.0", id: r.id, error: { code: -32000, message: "the world is still building" } }));
    let j = 0;
    post({ type: "rpc", id, reply: status_.map((s) => s ?? answered[j++]) });
  }
  return null;
}

async function run() {
  const generation = ++host.generation;
  host.world = null; host.frames = []; host.day = 0; host.done = false; host.error = null;
  post({ type: "state", state: "building" });
  try {
    host.world = await build();
  } catch (e) {
    host.error = e?.message ?? String(e);
    post({ type: "state", state: "failed", detail: host.error });
    return;
  }
  post({ type: "state", state: "live" });
  while (generation === host.generation) {
    if (await serve() === "reset") {
      post({ type: "reset" });
      run();
      return;
    }
    if (host.done) { post({ type: "state", state: "done" }); await waitForWork(); continue; }
    if (host.paused && host.stepLeft <= 0) { await waitForWork(); continue; }
    const t0 = performance.now();
    let frame;
    try {
      frame = await host.world.step();
    } catch (e) {
      host.error = e?.message ?? String(e);
      post({ type: "state", state: "failed", detail: host.error });
      return;
    }
    host.day = host.world.day;
    const took = performance.now() - t0;
    frame = { ...frame, pace: Math.round(100000 / Math.max(took, 1)) / 100, paused: host.paused };
    host.frames.push(frame);
    post({ type: "frame", frame });
    if (host.stepLeft > 0 && --host.stepLeft === 0) host.paused = true;
    // The pace caps days a second (0: flat out); either way, messages get a turn between days.
    await sleep(host.pace > 0 ? Math.max(0, 1000 / host.pace - took) : 0);
  }
}

self.onmessage = (ev) => {
  const m = ev.data ?? {};
  if (m.type === "start") {
    if (host.world || host.generation) {                 // a reconnecting page: replay, then live
      for (const frame of host.frames) post({ type: "frame", frame });
      post({ type: "state", state: host.error ? "failed" : host.done ? "done" : "live", detail: host.error });
      return;
    }
    host.sets = m.sets ?? {};
    run();
  } else if (m.type === "op") {
    host.control.push(m);
    wake();
  } else if (m.type === "rpc") {
    host.rpc.push(m);
    wake();
  }
};
