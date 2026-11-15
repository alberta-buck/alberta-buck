// The link to a world in this tab: the savings world in a Web Worker (world-worker.js), with the
// same surface as SimLink (link.js) -- frames, control ops and JSON-RPC -- so the page drives either
// alike. The worker keeps the world while the page lives; a reconnect (after close) replays its
// frames, as the server does for a reloaded page.

import { RpcError } from "./link.js";

const RPC_TIMEOUT_MS = 180_000;     // a call may wait out a whole simulated day

export class LocalLink {
  /** sets: the world's settings (as SimLink's channels() carry them); onFrame(frame);
   *  onState(state, detail); onReset() when the world starts over. `worker` makes the worker (a
   *  test injects one); by default the bundled savings-world.js beside the page. */
  constructor({ sets = {}, onFrame, onState, onReset, worker } = {}) {
    Object.assign(this, { sets, onFrame, onState, onReset });
    this.makeWorker = worker ?? (() => new Worker(new URL("./savings-world.js", globalThis.location?.href),
                                                  { type: "module" }));
    this.state = "idle";
    this.closed = false;
    this.nextId = 1;
    this.nextRpc = 1;
    this.pending = new Map();
  }

  _set(state, detail) {
    this.state = state;
    this.onState?.(state, detail);
  }

  connect() {
    this.closed = false;
    if (!this.worker) {
      this.worker = this.makeWorker();
      this.worker.onmessage = (ev) => this._message(ev.data ?? {});
      this.worker.onerror = (ev) => this._set("failed", ev?.message ?? "the world's worker failed");
    }
    this._set("building");
    this.worker.postMessage({ type: "start", sets: this.sets });
  }

  _message(m) {
    if (m.type === "frame") {
      if (this.closed) return;
      if (this.state !== "live") this._set("live");
      this.onFrame?.(m.frame);
    } else if (m.type === "state") {
      if (!this.closed || m.state === "failed") this._set(m.state, m.detail);
    } else if (m.type === "reset") {
      this.onReset?.();
      this._set("building");
    } else if (m.type === "op" || m.type === "rpc") {
      const p = this.pending.get(m.id);
      if (!p) return;
      this.pending.delete(m.id);
      clearTimeout(p.timer);
      p.resolve(m.reply);
    }
  }

  /** The page leaves this world (a new world, new settings): its worker and world stop. */
  close() {
    this.terminate();
    this._set("closed");
  }

  /** Stop the worker and its world for good. */
  terminate() {
    this.closed = true;
    this.worker?.terminate();
    this.worker = null;
    for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(new Error("the world stopped")); }
    this.pending.clear();
  }

  _ask(type, body, timeout) {
    if (!this.worker) return Promise.reject(new Error("the world is not running"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("the world did not answer in time"));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.worker.postMessage({ type, id, ...body });
    });
  }

  /** A control op; resolves on the world's acknowledgement (applied at the next day boundary). */
  async op(msg) {
    const r = await this._ask("op", { op: msg }, 30_000);
    if (!r.ok) throw new Error(r.err ?? "the world refused that");
    return r;
  }

  /** One JSON-RPC call; throws RpcError on an error reply. */
  async call(method, params = []) {
    const [r] = await this.batch([{ method, params }]);
    if (r.error) throw new RpcError(r.error);
    return r.result;
  }

  /** A batch, answered between two of the world's days: [{method, params}] -> [{result} | {error}]. */
  async batch(reqs) {
    const env = reqs.map((r) => ({ jsonrpc: "2.0", id: this.nextRpc++, method: r.method, params: r.params ?? [] }));
    const out = await this._ask("rpc", { reqs: env }, RPC_TIMEOUT_MS);
    if (!Array.isArray(out)) throw new RpcError({ message: "a malformed reply" });
    const byId = new Map(out.map((r) => [r.id, r]));
    return env.map((e) => byId.get(e.id) ?? { error: { message: "no reply" } });
  }
}
