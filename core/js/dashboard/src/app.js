// The dashboard page: pick a run, watch it. A finished run is read once; a running one is
// polled for the days it has added. Tabs other than the Overview are named but not yet built.

import { HoverBus } from "./chart.js";
import { OverviewTab } from "./overview.js";
import { runStatus } from "./series.js";
import { HttpRunSource, listRuns } from "./sources.js";

const POLL_MS = 2000;
const MANIFEST_MS = 10000;
const TABS = [
  ["overview", "Overview"], ["market", "Market"], ["basket", "BuckBasket"], ["jubilee", "Jubilee fund"],
  ["insurers", "Insurers and pools"], ["agents", "Agents"], ["events", "Events"],
];
const STATUS = {
  running: ["Running", "good"], complete: ["Complete", "neutral"], failed: ["Failed", "critical"],
  stale: ["Stale: not updated for 2 minutes", "warning"], incomplete: ["Incomplete", "warning"],
};

const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
};

const state = { source: null, manifest: null, frames: [], interventions: [], range: "all", timer: null, lastManifest: 0 };
const bus = new HoverBus();
let overview = null;

function theme(next) {
  const mode = next ?? store.get("dashboard-theme") ?? "auto";
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", mode);
  $("theme").textContent = `Theme: ${mode}`;
  store.set("dashboard-theme", mode);
}

function tabs() {
  const bar = $("tabs");
  for (const [id, label] of TABS) {
    const b = document.createElement("button");
    b.type = "button";
    b.role = "tab";
    b.textContent = label;
    b.setAttribute("aria-selected", String(id === "overview"));
    if (id !== "overview") { b.disabled = true; b.title = "Not yet built"; }
    bar.append(b);
  }
}

function showStatus() {
  const s = runStatus(state.manifest);
  const [label, tone] = STATUS[s] ?? [s, "neutral"];
  const pill = $("status");
  pill.className = `status status-${tone}`;
  pill.replaceChildren();
  const dot = document.createElement("span");
  dot.className = "status-icon";
  dot.setAttribute("aria-hidden", "true");
  dot.textContent = { good: "●", critical: "✖", warning: "⚠", neutral: "✓" }[tone];
  pill.append(dot, document.createTextNode(` ${label}`));
  const m = state.manifest ?? {};
  const rev = m.revision ? `${m.revision.commit}${m.revision.dirty ? " (changed)" : ""}` : "revision not recorded";
  $("meta").textContent = [m.scenario?.name, m.engine, `seed ${m.scenario?.seed ?? "?"}`, rev, m.error ? `error: ${m.error}` : null]
    .filter(Boolean).join(" · ");
}

function render() {
  if (!overview) overview = new OverviewTab($("overview"), bus);
  overview.update({ frames: state.frames, manifest: state.manifest, interventions: state.interventions, range: state.range });
  $("empty").hidden = state.frames.length > 0;
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
    if (fresh.length) {
      state.frames = state.frames.concat(fresh);
      render();
    }
    $("problem").hidden = true;
  } catch (e) {
    $("problem").hidden = false;
    $("problem").textContent = `Could not read the run: ${e.message}`;
  }
  if (src === state.source && runStatus(state.manifest) === "running") state.timer = setTimeout(poll, POLL_MS);
}

async function open(id) {
  clearTimeout(state.timer);
  const src = new HttpRunSource(id);
  Object.assign(state, { source: src, frames: [], interventions: [], lastManifest: Date.now() });
  const url = new URL(location.href);
  url.searchParams.set("run", id);
  history.replaceState(null, "", url);
  try {
    state.manifest = await src.manifest();
    state.interventions = await src.interventions();
    showStatus();
    state.frames = await src.frames(-1);
    render();
    $("problem").hidden = true;
  } catch (e) {
    $("problem").hidden = false;
    $("problem").textContent = `Could not open ${id}: ${e.message}`;
    return;
  }
  if (runStatus(state.manifest) === "running") state.timer = setTimeout(poll, POLL_MS);
}

async function main() {
  theme();
  $("theme").addEventListener("click", () => {
    const order = ["auto", "light", "dark"];
    theme(order[(order.indexOf(store.get("dashboard-theme") ?? "auto") + 1) % order.length]);
  });
  tabs();
  for (const b of document.querySelectorAll("#range button")) {
    b.addEventListener("click", () => {
      state.range = b.dataset.range;
      for (const o of document.querySelectorAll("#range button")) o.setAttribute("aria-pressed", String(o === b));
      if (state.frames.length) render();
    });
  }
  const runs = await listRuns().catch((e) => {
    $("problem").hidden = false;
    $("problem").textContent = `Could not list runs: ${e.message}`;
    return [];
  });
  const pick = $("run");
  for (const r of runs) {
    const o = document.createElement("option");
    o.value = r.id;
    o.textContent = `${r.id} (${runStatus(r)}, ${r.frames ?? 0} days)`;
    pick.append(o);
  }
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
