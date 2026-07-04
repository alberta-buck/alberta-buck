// eqworld.html's DOM glue over the EqWorldApp controller: presentation
// only -- a tab bar over per-area instrument panels (overview, pools,
// controller, basket, savers, debtors, journal), each rendered from
// the controller's snapshot/chart accessors.  Every chain fact comes
// from the controller (and lands in the journal panel via the
// Session's JournalWriter).
//
// Build: make nix-core-demo-eqworld  (esbuild bundle -> demo/eqapp.js)

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { tevmSession } from "../../src/backends.js";
import { loadIdentity } from "../../src/identity-web.js";
import { JournalWriter } from "../../src/journal.js";
import { artifact } from "../../artifacts/bundle.mjs";
import { EqWorldApp } from "./eqapp.js";

const $ = (id) => document.getElementById(id);
const USD = (v) => v === null || v === undefined ? "-"
  : Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
const TABS = ["overview", "pools", "controller", "basket",
              "savers", "debtors", "journal"];

let app = null;
let activeTab = "overview";

function logLine(cls, text) {
  const d = document.createElement("div");
  d.className = cls;
  d.textContent = text;
  $("log").prepend(d);
}

function journalLine(line) {
  const j = JSON.parse(line);
  logLine(j.matched ? "j-ok" : "j-miss",
    `#${j.i} ${j.tag || j.fn} -> ${j.outcome}` +
    (j.err ? `  [${j.err}]` : ""));
}

// ---- per-tab renderers ------------------------------------------------

function renderOverview() {
  const ch = app.charts();
  $("charts").innerHTML = ch
    ? [ch.k, ch.bvib, ch.price]
        .map((svg) => `<div class="chart">${svg}</div>`).join("")
    : `<div class="addr">tick a day to start the charts</div>`;
}

function renderRoster() {
  const roster = $("roster");
  roster.innerHTML = "";
  for (const r of app.roster()) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = r.kind === "saver"
      ? `<b>${r.name}</b> <span class="addr">saver</span> · ${r.state} · ` +
        `cost $${USD(r.cost)} · mark $${USD(r.mark)} · P&amp;L $${USD(r.profit)}`
      : `<b>${r.name}</b> <span class="addr">debtor</span> · ` +
        `mortgage $${USD(r.mortgage)} (hypo $${USD(r.hypo)}) · ` +
        `drawn $${USD(r.drawn)} · net <b>$${USD(r.net)}</b> vs $${USD(r.hypoNet)}`;
    roster.appendChild(card);
  }
}

async function renderPools() {
  const ps = await app.poolsSnapshot();
  const cards = [];
  for (const t of ps.tokens) {
    cards.push(
      `<div class="card"><b>${t.sym}/USDC</b> ` +
      `<span class="addr">${t.usdcPool.address.slice(0, 10)}… (whale territory)</span><br>` +
      `spot $${t.usdcPool.spot.toFixed(4)} · reference $${t.usdcPool.ref.toFixed(4)}<br>` +
      `reserves ${USD(t.usdcPool.tok)} ${t.sym} · $${USD(t.usdcPool.usdc)}</div>`);
    cards.push(
      `<div class="card"><b>${t.sym}/BUCK</b> ` +
      `<span class="addr">${t.buckPool.address.slice(0, 10)}… (basket pool, arb-tied)</span><br>` +
      `spot ${t.buckPool.spot.toFixed(4)} BUCK · implied ${t.buckPool.implied.toFixed(4)} · ` +
      `divergence <b>${t.buckPool.divBp} bp</b><br>` +
      `reserves ${USD(t.buckPool.tok)} ${t.sym} · ${USD(t.buckPool.buck)} BUCK</div>`);
  }
  cards.push(
    `<div class="card"><b>BUCK/USDC</b> ` +
    `<span class="addr">${ps.ub.address.slice(0, 10)}… (floating, never controlled)</span><br>` +
    `spot $${ps.ub.spot.toFixed(4)} · reserves ${USD(ps.ub.buck)} BUCK · $${USD(ps.ub.usdc)}</div>`);
  $("poolcards").innerHTML = cards.join("");
  const ch = app.poolCharts();
  $("poolcharts").innerHTML = ch
    ? [ch.spotVsRef, ch.divergence, ch.float]
        .map((svg) => `<div class="chart">${svg}</div>`).join("")
    : "";
}

async function renderController() {
  const cs = await app.controllerSnapshot();
  const c = cs.config;
  $("ctrlcard").innerHTML =
    `<div class="card"><b>BuckKControllerDirect</b> (permissionless compute(), ` +
    `advanced by the PidKeeper each tick)<br>` +
    `buckK <b>${cs.K.toFixed(6)}</b> · fundingFactor <b>${cs.ff.toFixed(4)}</b> · ` +
    `lastBasketCost ${cs.lastBasketCost.toFixed(6)}<br>` +
    `<span class="addr">gains (real x 1e12): Kp ${c.kp} · Ki ${c.ki} · Kd ${c.kd} · ` +
    `dt ${c.dt}s · K rails [${Number(c.kmin) / 1e18}, ${Number(c.kmax) / 1e18}] · ` +
    `K0 ${Number(c.k0) / 1e18}</span></div>`;
  const ch = app.controllerCharts();
  $("ctrlcharts").innerHTML = ch
    ? [ch.k, ch.ff].map((svg) => `<div class="chart">${svg}</div>`).join("")
    : "";
}

async function renderBasket() {
  const bs = await app.basketSnapshot();
  $("basketcard").innerHTML =
    `<div class="card"><b>BuckBasketProRata</b> (deposits mint BUCK and LP ` +
    `the pair; the depositor holds the receipt NFT)<br>` +
    `basketValueInBuck <b>${bs.bvib.toFixed(6)}</b> · receipts issued ${bs.receipts}<br>` +
    bs.constituents.map((c) =>
      `<span class="addr">backing in ${c.sym}/BUCK: ${USD(c.tok)} ${c.sym} + ` +
      `${USD(c.buck)} BUCK</span>`).join("<br>") +
    `</div>`;
  const svg = app.basketChart();
  $("basketcharts").innerHTML = svg ? `<div class="chart">${svg}</div>` : "";
}

function fillSelect(sel, names) {
  const cur = sel.value;
  sel.innerHTML = "";
  names.forEach((name, i) => {
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = name;
    sel.appendChild(o);
  });
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  else if (names.length) sel.value = String(names.length - 1);
}

function renderSavers() {
  fillSelect($("saversel"), app.savers.map((s) => s.name));
  const i = parseInt($("saversel").value || "0", 10);
  const row = app.roster().filter((r) => r.kind === "saver")[i];
  if (!row) {
    $("saverdetail").innerHTML =
      `<div class="addr">no savers yet -- add one above</div>`;
    return;
  }
  const a = app.savers[i].agent;
  const roi = a.dollarDays > 0n
    ? (Number(a.profitUsd) * 365 * 100 / Number(a.dollarDays)).toFixed(2) + "%/yr"
    : "-";
  const svg = app.saverChart(i);
  $("saverdetail").innerHTML =
    `<div class="card"><b>${row.name}</b> ` +
    `<span class="addr">${a.account.address.slice(0, 10)}…</span><br>` +
    `${row.state}<br>` +
    `cost $${USD(row.cost)} · mark $${USD(row.mark)} · ` +
    `realized P&amp;L $${USD(row.profit)} · annualized ${roi}</div>` +
    (svg ? `<div class="chart">${svg}</div>` : "");
}

function renderDebtors() {
  fillSelect($("debtorsel"), app.debtors.map((d) => d.name));
  const i = parseInt($("debtorsel").value || "0", 10);
  const row = app.roster().filter((r) => r.kind === "debtor")[i];
  if (!row) {
    $("debtordetail").innerHTML =
      `<div class="addr">no debtors yet -- add one above</div>`;
    return;
  }
  const ch = app.debtorCharts(i);
  $("debtordetail").innerHTML =
    `<div class="card"><b>${row.name}</b><br>` +
    `mortgage $${USD(row.mortgage)} (untouched: $${USD(row.hypo)})<br>` +
    `credit drawn $${USD(row.drawn)} · banked $${USD(row.banked)}<br>` +
    `net worth <b>$${USD(row.net)}</b> vs counterfactual $${USD(row.hypoNet)}</div>` +
    (ch ? `<div class="chart">${ch.net}</div><div class="chart">${ch.position}</div>` : "");
}

async function renderActive() {
  renderRoster();
  if (activeTab === "overview") renderOverview();
  else if (activeTab === "pools") await renderPools();
  else if (activeTab === "controller") await renderController();
  else if (activeTab === "basket") await renderBasket();
  else if (activeTab === "savers") renderSavers();
  else if (activeTab === "debtors") renderDebtors();
  // journal streams live; nothing to render
}

function showTab(name) {
  activeTab = name;
  for (const t of TABS) {
    $(`view-${t}`).style.display = t === name ? "" : "none";
    $(`tab-${t}`).className = t === name ? "tabon" : "";
  }
  renderActive().catch((e) => logLine("j-miss", `render: ${e.message ?? e}`));
}

async function refresh() {
  const st = await app.status();
  $("statusbar").textContent =
    `day ${st.day} · K ${st.K.toFixed(4)} · basket/BUCK ${st.bvib.toFixed(4)}` +
    ` · BUCK $${st.price.toFixed(4)} · mismatches ${st.mismatches}`;
  await renderActive();
}

async function act(label, fn) {
  const buttons = document.querySelectorAll("button, select, input");
  buttons.forEach((b) => (b.disabled = true));
  try {
    await fn();
  } catch (e) {
    logLine("j-miss", `${label}: ${e.message ?? e}`);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
    await refresh();
  }
}

async function main() {
  $("status").textContent = "loading the identity kernel…";
  const identity = await loadIdentity("./wasm-web/buck_identity_bg.wasm");

  $("status").textContent =
    "deploying the equilibrium world on the in-browser EVM " +
    "(BUCK stack, basket, three pools, Universal Router)…";
  const session = await tevmSession({
    journal: new JournalWriter(journalLine),
  });
  app = new EqWorldApp({
    session, identity, artifacts: artifact,
    urArtifact: artifact("UniversalRouter"),
    makeAccount: () => privateKeyToAccount(generatePrivateKey()),
  });
  await app.boot();

  // The opening cast: one saver (full onboarding ceremony in this tab),
  // one debtor (pledges through its public proxy), two days of life.
  $("status").textContent = "onboarding the opening cast…";
  await app.addSaver();
  await app.addDebtor();
  await app.tick();
  await app.tick();
  $("status").textContent =
    "live: the BUCK equilibrium loop on an in-browser EVM — " +
    "whale + arb + PID in the background; add savers and debtors freely";

  for (const t of TABS) $(`tab-${t}`).onclick = () => showTab(t);
  for (const sel of ["saversel", "debtorsel"]) {
    $(sel).onchange = () => renderActive();
  }

  const num = (id, dflt) => {
    const v = parseFloat($(id).value);
    return BigInt(Math.round(Number.isFinite(v) ? v : dflt)) * 1_000_000n;
  };
  $("addsaver").onclick = () => act("add saver", async () => {
    await app.addSaver({ budget: num("sbudget", 25_000),
                         holdDays: parseInt($("shold").value || "60", 10) });
  });
  $("adddebtor").onclick = () => act("add debtor", async () => {
    await app.addDebtor({ house: num("dhouse", 400_000),
                          payment: num("dpay", 3_000) });
  });

  let timer = null;
  let ticking = false;
  const tickOnce = async () => {
    if (ticking) return;
    ticking = true;
    try { await app.tick(); } finally { ticking = false; }
    await refresh();
  };
  $("step").onclick = () => tickOnce();
  $("play").onclick = () => {
    if (timer) return;
    timer = setInterval(tickOnce, Number($("speed").value));
  };
  $("pause").onclick = () => {
    clearInterval(timer);
    timer = null;
  };

  showTab("overview");
  await refresh();
}

main().catch((e) => {
  $("status").textContent = "FAILED: " + (e.message ?? e);
  console.error(e);
});
