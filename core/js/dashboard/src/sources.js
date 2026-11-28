// Where a dashboard's records come from. Every source yields the same run record
// (schema buck-run/1, alberta_buck/sim/runrecord.py) and says what else it can do, so a
// tab asks the source's capabilities before it offers anything:
//
//   manifest()          -> the run's manifest
//   frames(since)       -> frames after day `since`, in day order
//   interventions()     -> shocks and setting changes applied, with their days
//   agents(member?)     -> each household's latest line, or one household's lines
//   events(member)      -> one household's state-machine steps
//   capabilities        -> { live, control, agents, events }: live = the run is still
//                          growing; control = it accepts commands (later: the Python
//                          session's control channel); agents / events = the per-agent
//                          and state-machine files exist
//
// Sources:
//   HttpRunSource       a run directory served by alberta_buck.sim.dashboard: finished,
//                       or growing while a Python run writes it (polled by the page)
//   (later) a BrowserWorldSource: a small Tevm world running in this tab, writing the same
//                       frames in memory -- no server, fewer agents, no campaign-scale
//                       history. It is the public path: many visitors, each with their own
//                       world, and none of them loading a central server.

export class HttpRunSource {
  constructor(id, base = "") {
    this.id = id;
    this.base = base;
    this.meta = null;
  }

  async get(path) {
    const r = await fetch(`${this.base}/api/runs/${encodeURIComponent(this.id)}/${path}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`${path}: ${r.status} ${(await r.json().catch(() => ({}))).error ?? ""}`.trim());
    return r.json();
  }

  async manifest() {
    this.meta = await this.get("manifest");
    return this.meta;
  }

  frames(since = -1) {
    return this.get(`frames?since=${since}`);
  }

  interventions() {
    return this.get("interventions");
  }

  // Each household's latest line, or one household's lines day by day.
  agents(member = null) {
    return this.get(member === null ? "agents" : `agents?member=${encodeURIComponent(member)}`);
  }

  events(member) {
    return this.get(`events?member=${encodeURIComponent(member)}`);
  }

  wheel(day) {
    return this.get(`wheel?day=${encodeURIComponent(day)}`);
  }

  get capabilities() {
    const c = this.meta?.capabilities ?? {};
    return { live: this.meta?.status === "running", control: c.control ?? null, agents: !!c.agents, events: !!c.events };
  }
}

export async function listRuns(base = "") {
  const r = await fetch(`${base}/api/runs`, { cache: "no-store" });
  if (!r.ok) throw new Error(`runs: ${r.status}`);
  return r.json();
}

// What the Compare tab reads: the comparisons' indexes (alberta_buck/sim/compare.py), the runs, and
// any run's manifest and frames.
export const compareSource = (base = "") => ({
  async comparisons() {
    const r = await fetch(`${base}/api/comparisons`, { cache: "no-store" });
    if (!r.ok) throw new Error(`comparisons: ${r.status}`);
    return r.json();
  },
  runs: () => listRuns(base),
  manifest: (id) => new HttpRunSource(id, base).manifest(),
  frames: (id) => new HttpRunSource(id, base).frames(-1),
});
