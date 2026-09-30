// The link to a server-hosted world (alberta_buck/sim/server.py): three
// websockets to one session -- frames (lite, replayed from the world's
// first day on every connect), control (pause, step, shock, chain), and
// JSON-RPC (reads and the saver's signed transactions).  The server answers
// each channel's messages in order, so replies are matched first in, first
// out.  A dropped frames channel reconnects; the world lives on the server
// for ten idle minutes, so a reload or a flaky network finds it again.

const RPC_TIMEOUT_MS = 180_000;     // a call may wait out a whole simulated day
const RETRY_MS = 3_000;
const UNREACHABLE_RETRY_MS = 15_000;

class Channel {
  constructor(url, WS, onClose) {
    this.pending = [];
    this.ws = new WS(url);
    this.open = new Promise((resolve, reject) => {
      this.ws.onopen = () => resolve();
      this.ws.onerror = () => reject(new Error(`cannot reach ${url.split("?")[0]}`));
    });
    this.ws.onmessage = (ev) => {
      const p = this.pending.shift();
      if (!p) return;
      clearTimeout(p.timer);
      try { p.resolve(JSON.parse(ev.data)); } catch (e) { p.reject(e); }
    };
    this.ws.onclose = () => {
      for (const p of this.pending.splice(0)) {
        clearTimeout(p.timer);
        p.reject(new Error("the world's connection closed"));
      }
      onClose?.();
    };
  }

  async ask(msg, timeout = RPC_TIMEOUT_MS) {
    await this.open;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        const i = this.pending.findIndex((p) => p.timer === timer);
        if (i >= 0) this.pending.splice(i, 1);
        reject(new Error("the world did not answer in time"));
      }, timeout);
      this.pending.push({ resolve, reject, timer });
      this.ws.send(JSON.stringify(msg));
    });
  }

  close() {
    try { this.ws.close(); } catch { /* already */ }
  }
}

/** An error from the chain, carrying what viem needs to decode a revert. */
export class RpcError extends Error {
  constructor(err) {
    super(err?.message ?? "the world refused the call");
    this.code = err?.code;
    this.data = err?.data;
  }
}

export class SimLink {
  /** urls: channels(base, sid, sets); onFrame(frame); onState(state, detail);
   *  onReset() when the server rebuilds the world from its first day. */
  constructor({ urls, onFrame, onState, onReset, WebSocket: WS = globalThis.WebSocket }) {
    Object.assign(this, { urls, onFrame, onState, onReset, WS });
    this.state = "idle";
    this.closed = false;
    this.nextId = 1;
  }

  _set(state, detail) {
    this.state = state;
    this.onState?.(state, detail);
  }

  connect() {
    this.closed = false;
    this._set("connecting");
    const ws = new this.WS(this.urls.frames);
    let opened = false;
    this.frames = ws;
    ws.onopen = () => {
      opened = true;
      this._set("building");
    };
    ws.onerror = () => {};
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.error) {
        this._set(m.refused ? "refused" : "failed", m.error);
        this.closed = true;
        return;
      }
      if (m.reset) {                    // the world starts over: same link, first day
        this.onReset?.();
        this._set("building");
        return;
      }
      if (m.done) {
        this._set("done");
        return;
      }
      if (this.state !== "live") this._set("live");
      this.onFrame?.(m);
    };
    ws.onclose = () => {
      if (this.frames !== ws) return;
      this.control?.close();
      this.rpcCh?.close();
      this.control = this.rpcCh = null;
      if (this.closed) return;
      // Never opened: no server there (a static copy of the page, say).
      this._set(opened ? "reconnecting" : "unreachable");
      setTimeout(() => !this.closed && this.connect(), opened ? RETRY_MS : UNREACHABLE_RETRY_MS);
    };
    // The other channels open lazily, after the frames channel built the world.
    this.control = null;
    this.rpcCh = null;
  }

  close() {
    this.closed = true;
    this.frames?.close();
    this.control?.close();
    this.rpcCh?.close();
    this.control = this.rpcCh = null;
    this._set("closed");
  }

  _rpc() {
    if (!this.rpcCh) this.rpcCh = new Channel(this.urls.rpc, this.WS, () => { this.rpcCh = null; });
    return this.rpcCh;
  }

  _control() {
    if (!this.control) {
      this.control = new Channel(this.urls.control, this.WS, () => { this.control = null; });
    }
    return this.control;
  }

  /** Send a control op; resolves on the server's acknowledgement (the op
   *  itself applies at the next day boundary). */
  async op(msg) {
    const r = await this._control().ask(msg, 30_000);
    if (!r.ok) throw new Error(r.err ?? "the world refused that");
    return r;
  }

  /** One JSON-RPC call; throws RpcError on an error reply. */
  async call(method, params = []) {
    const [r] = await this.batch([{ method, params }]);
    if (r.error) throw new RpcError(r.error);
    return r.result;
  }

  /** A batch, answered under one hold of the world's chain: [{method,
   *  params}] -> [{result} | {error}], in order. */
  async batch(reqs) {
    const env = reqs.map((r) => ({ jsonrpc: "2.0", id: this.nextId++, method: r.method,
                                   params: r.params ?? [] }));
    const out = await this._rpc().ask(env);
    if (!Array.isArray(out)) throw new RpcError(out?.error ?? { message: "a malformed reply" });
    const byId = new Map(out.map((r) => [r.id, r]));
    return env.map((e) => byId.get(e.id) ?? { error: { message: "no reply" } });
  }
}
