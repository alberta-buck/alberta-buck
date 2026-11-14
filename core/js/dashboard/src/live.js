// Live control of a session the dashboard's server runs (alberta_buck.sim.dashboard). A run whose
// manifest names capabilities.control is a live session at that address; every action is a JSON
// POST, applied between the world's days. World-changing actions are interventions (the same
// vocabulary as an experiment's [[interventions]]), recorded with source "operator", and the
// session's experiment can be exported with them and replayed exactly.
//
// A world running in the browser tab (later) gives the page the same object with the actions it
// supports; the controls ask `supports(op)` before they offer one.

async function json(r) {
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error ?? `${r.status}`);
  return body;
}

const post = (url, body) => fetch(url, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
}).then(json);

export class LiveControl {
  constructor(address, base = "") {
    this.address = `${base}${address}`;
  }

  status() { return fetch(this.address, { cache: "no-store" }).then(json); }
  members() { return fetch(`${this.address}/members`, { cache: "no-store" }).then(json); }
  send(op) { return post(`${this.address}/control`, op); }
  intervene(intervention) { return this.send({ op: "intervene", intervention }); }
  exportUrl() { return `${this.address}/experiment`; }
  supports() { return true; }
}

export const experiments = (base = "") => fetch(`${base}/api/experiments`, { cache: "no-store" }).then(json);
export const startSession = (body, base = "") => post(`${base}/api/sessions`, body);

// "key=value" lines (an experiment's --set overrides), blank lines and # comments ignored.
export function parseSets(text) {
  return String(text ?? "").split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#"))
    .map((l) => {
      if (!/^[A-Za-z0-9_.]+=.+$/.test(l)) throw new Error(`not key=value: ${l}`);
      return l;
    });
}

// The filled fields of a form as an object: numbers where the input is numeric, blanks left out.
export function filled(fields) {
  const out = {};
  for (const [key, input] of Object.entries(fields)) {
    const v = input.value.trim();
    if (v === "") continue;
    out[key] = input.type === "number" ? Number(v) : v;
  }
  return out;
}
