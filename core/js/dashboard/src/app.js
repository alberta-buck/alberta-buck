// The dashboard page: pick a run, watch it. A finished run is read once; a running one is
// polled for the days it has added. One tab is shown at a time and only it is redrawn; the
// Insurers and Events tabs are named and not yet built.

import { AgentsTab } from "./agents.js";
import { CompareTab } from "./compare.js";
import { HoverBus } from "./chart.js";
import { ControlPanel, newSessionForm } from "./controls.js";
import { LiveControl } from "./live.js";
import { ChartTab, OverviewTab } from "./overview.js";
import { BASKET, JUBILEE, MARKET, runStatus, tabTiles } from "./series.js";
import { HttpRunSource, compareSource, listRuns } from "./sources.js";

const POLL_MS = 2000;
const MANIFEST_MS = 10000;
const TABS = [
  ["overview", "Overview"], ["market", "Market"], ["basket", "BuckBasket"], ["jubilee", "Jubilee fund"],
  ["insurers", "Insurers and pools", "Not yet recorded: the frames do not read the insurers' pools"],
  ["agents", "Agents"], ["events", "Events", "Not yet built"], ["compare", "Compare"],
];
const AGENTS_MS = 10000;
const STATUS = {
  running: ["Running", "good"], complete: ["Complete", "neutral"], failed: ["Failed", "critical"],
  stale: ["Stale: not updated for 10 minutes", "warning"], incomplete: ["Incomplete", "warning"],
  killed: ["Killed: its process ended without closing the record", "critical"],
  stopped: ["Stopped", "neutral"], reset: ["Reset: a new record follows", "neutral"], paused: ["Paused", "neutral"],
};

const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
};

const state = { source: null, manifest: null, frames: [], interventions: [], range: "all", timer: null, lastManifest: 0,
  control: null, session: null, tab: "overview", lastAgents: 0 };
const bus = new HoverBus();
let views = {};
let panel = null;

// A live session's own state outranks its record's age: a paused world writes nothing for a while.
function effectiveStatus() {
  const s = state.session;
  if (state.control && s && !s.done) return s.paused ? "paused" : "running";
  return runStatus(state.manifest);
}
const following = () => state.control ? !(state.session?.done) : runStatus(state.manifest) === "running";

function theme(next) {
  const mode = next ?? store.get("dashboard-theme") ?? "auto";
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
  $("theme").textContent = `Theme: ${mode}`;
  store.set("dashboard-theme", mode);
}

function tabs() {
  const bar = $("tabs");
  const panels = $("panels");
  for (const [id, label, off] of TABS) {
    const b = document.createElement("button");
    b.type = "button";
    b.role = "tab";
    b.id = `tab-${id}`;
    b.textContent = label;
    b.setAttribute("aria-controls", `panel-${id}`);
    if (off) { b.disabled = true; b.title = off; }
    b.addEventListener("click", () => select(id));
    bar.append(b);
    const p = document.createElement("main");
    p.id = `panel-${id}`;
    p.className = "panel";
    p.setAttribute("role", "tabpanel");
    p.setAttribute("aria-labelledby", `tab-${id}`);
    p.hidden = true;
    panels.append(p);
  }
}

// Each run gets fresh views: its commodities name the Market and BuckBasket series.
function view(id) {
  if (views[id]) return views[id];
  const root = $(`panel-${id}`), tokens = state.manifest?.tokens ?? [];
  views[id] = id === "overview" ? new OverviewTab(root, bus)
    : id === "market" ? new ChartTab(root, bus, MARKET(tokens, !state.frames.length || state.frames.some((f) => f.market?.pools)),
      (f) => tabTiles("market", f))
      : id === "basket" ? new ChartTab(root, bus, BASKET(tokens), (f) => tabTiles("basket", f))
        : id === "jubilee" ? new ChartTab(root, bus, JUBILEE(), (f) => tabTiles("jubilee", f))
          : id === "agents" ? new AgentsTab(root, bus, panel)
            : id === "compare" ? new CompareTab(root, bus, compareSource()) : null;
  return views[id];
}

function select(id) {
  state.tab = id;
  for (const [t] of TABS) {
    $(`tab-${t}`).setAttribute("aria-selected", String(t === id));
    $(`panel-${t}`).hidden = t !== id;
  }
  const url = new URL(location.href);
  url.searchParams.set("tab", id);
  history.replaceState(null, "", url);
  render(true);
}

function showStatus() {
  const s = effectiveStatus();
  const [label, tone] = STATUS[s] ?? [s, "neutral"];
  const pill = $("status");
  pill.className = `status status-${tone}`;
  pill.replaceChildren();
  const dot = document.createElement("span");
  dot.className = "status-icon";
  dot.setAttribute("aria-hidden", "true");
  dot.textContent = s === "paused" ? "‖" : { good: "●", critical: "✖", warning: "⚠", neutral: "✓" }[tone];
  pill.append(dot, document.createTextNode(` ${label}`));
  const m = state.manifest ?? {};
  const rev = m.revision ? `${m.revision.commit}${m.revision.dirty ? " (changed)" : ""}` : "revision not recorded";
  $("meta").textContent = [m.scenario?.name, m.engine, `seed ${m.scenario?.seed ?? "?"}`, rev, m.error ? `error: ${m.error}` : null]
    .filter(Boolean).join(" · ");
}

function render(switched = false) {
  $("empty").hidden = state.frames.length > 0 || state.tab === "compare";
  if (state.tab === "compare") {
    // Compare reads its own runs, whichever run is open above; drawn on switching to it.
    if (switched) view("compare").show();
    return;
  }
  if (!state.source) return;
  const v = view(state.tab);
  if (!v) return;
  if (v instanceof AgentsTab) {
    // The table is read on demand: on switching to it, and every ten seconds while a run grows.
    if (switched || Date.now() - state.lastAgents > AGENTS_MS) {
      state.lastAgents = Date.now();
      v.show({ source: state.source, range: state.range });
    }
    return;
  }
  v.update({ frames: state.frames, manifest: state.manifest, interventions: state.interventions, range: state.range });
}

async function poll() {
  const src = state.source;
  try {
    const last = state.frames.length ? state.frames[state.frames.length - 1].day : -1;
    const fresh = await src.frames(last);
    const now = Date.now();
    if (now - state.lastManifest > MANIFEST_MS) {
      state.manifest = await src.manifest();
      state.interventions = await src.interventions();
      state.lastManifest = now;
      showStatus();
    }
    if (src !== state.source) return;           // another run was chosen meanwhile
    if (state.control) {
      state.session = await state.control.status();
      panel.refresh(state.session);
      showStatus();
      if (state.session.run && state.session.run !== src.id) {          // reset: a new record
        await refreshRuns(state.session.run);
        return open(state.session.run);
      }
      if (state.session.done) {
        state.manifest = await src.manifest();
        state.interventions = await src.interventions();
        showStatus();
      }
    }
    if (fresh.length) {
      state.frames = state.frames.concat(fresh);
      state.interventions = await src.interventions();   // a new day may have recorded actions
      render();
      label();
    }
    $("problem").hidden = true;
  } catch (e) {
    $("problem").hidden = false;
    $("problem").textContent = `Could not read the run: ${e.message}`;
  }
  if (src === state.source && following()) state.timer = setTimeout(poll, POLL_MS);
}

async function open(id) {
  clearTimeout(state.timer);
  const src = new HttpRunSource(id);
  Object.assign(state, { source: src, frames: [], interventions: [], lastManifest: Date.now(), control: null, session: null,
    lastAgents: 0 });
  views = {};
  for (const [t] of TABS) $(`panel-${t}`).replaceChildren();
  $("run").value = id;
  const url = new URL(location.href);
  url.searchParams.set("run", id);
  history.replaceState(null, "", url);
  try {
    state.manifest = await src.manifest();
    state.interventions = await src.interventions();
    const address = src.capabilities.control;
    if (address) {
      state.control = new LiveControl(address);
      state.session = await state.control.status().catch(() => null);
      if (state.session) panel.attach(state.control, state.manifest);
      else state.control = null;                // the server that ran it is gone: a record only
    }
    if (!state.control) panel.detach();
    showStatus();
    state.frames = await src.frames(-1);
    render(true);
    $("problem").hidden = true;
  } catch (e) {
    $("problem").hidden = false;
    $("problem").textContent = `Could not open ${id}: ${e.message}`;
    return;
  }
  if (following()) state.timer = setTimeout(poll, POLL_MS);
}

// The open run's entry in the picker follows its growth.
function label() {
  const o = [...$("run").options].find((x) => x.value === state.source?.id);
  if (o) o.textContent = `${state.source.id} (${effectiveStatus()}, ${state.frames.length} days)`;
}

async function refreshRuns(select = null) {
  const runs = await listRuns().catch(() => []);
  const pick = $("run");
  const current = select ?? pick.value;
  pick.replaceChildren(...runs.map((r) => {
    const o = document.createElement("option");
    o.value = r.id;
    o.textContent = `${r.id} (${runStatus(r)}, ${r.frames ?? 0} days)`;
    return o;
  }));
  if (current && runs.some((r) => r.id === current)) pick.value = current;
  return runs;
}

// A new session's record appears once its world is deployed (its first day begins): wait for
// the session to name it, then open it.
async function follow(started, tries = 240) {
  const control = new LiveControl(`/api/sessions/${started.sid}`);
  for (let i = 0; i < tries; i++) {
    const s = await control.status().catch(() => null);
    if (s?.error || s?.done) break;
    if (s?.run) {
      await refreshRuns(s.run);
      return open(s.run);
    }
    await new Promise((ok) => setTimeout(ok, 1000));
  }
  const s = await control.status().catch(() => null);
  $("problem").hidden = false;
  $("problem").textContent = `${started.run} did not start: ${s?.error ?? "no first day within four minutes"}`;
}

async function main() {
  theme();
  $("theme").addEventListener("click", () => {
    const order = ["auto", "light", "dark"];
    theme(order[(order.indexOf(store.get("dashboard-theme") ?? "auto") + 1) % order.length]);
  });
  tabs();
  setInterval(() => { if (state.tab === "compare" && views.compare) views.compare.show(true); }, 10000);
  panel = new ControlPanel($("live"));
  newSessionForm($("new-session"), (started) => follow(started));
  for (const b of document.querySelectorAll("#range button")) {
    b.addEventListener("click", () => {
      state.range = b.dataset.range;
      for (const o of document.querySelectorAll("#range button")) o.setAttribute("aria-pressed", String(o === b));
      if (state.frames.length) render(true);
    });
  }
  const query = new URL(location.href).searchParams;
  const wantedTab = query.get("tab") ?? (query.has("comparison") ? "compare" : null);
  select(TABS.some(([t, , off]) => t === wantedTab && !off) ? wantedTab : "overview");
  const runs = await refreshRuns();
  const pick = $("run");
  pick.addEventListener("change", () => open(pick.value));
  const wanted = new URL(location.href).searchParams.get("run");
  const first = runs.find((r) => r.id === wanted) ?? runs[0];
  if (first) {
    pick.value = first.id;
    await open(first.id);
  } else {
    $("empty").hidden = false;
    $("empty").textContent = "No runs yet. Run a simulation (it records under build/runs) or import a vector: python -m alberta_buck.sim.runrecord import <vector.json>.";
  }
}

main();
