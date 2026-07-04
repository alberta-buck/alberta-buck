// eqworld.html's DOM glue over the EqWorldApp controller: presentation
// only; every chain fact comes from the controller (and lands in the
// journal panel via the Session's JournalWriter).
//
// Build: make nix-core-demo-eqworld  (esbuild bundle -> demo/eqapp.js)

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { tevmSession } from "../../src/backends.js";
import { loadIdentity } from "../../src/identity-web.js";
import { JournalWriter } from "../../src/journal.js";
import { artifact } from "../../artifacts/bundle.mjs";
import { EqWorldApp } from "./eqapp.js";

const $ = (id) => document.getElementById(id);
const USD = (v) => v === null ? "-" : v.toLocaleString(undefined,
  { maximumFractionDigits: 0 });

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

let app = null;

async function refresh() {
  const st = await app.status();
  $("statusbar").textContent =
    `day ${st.day} · K ${st.K.toFixed(4)} · basket/BUCK ${st.bvib.toFixed(4)}` +
    ` · BUCK $${st.price.toFixed(4)} · mismatches ${st.mismatches}`;
  const ch = app.charts();
  if (ch) {
    $("charts").innerHTML =
      [ch.k, ch.bvib, ch.price, ch.debtors, ch.savers]
        .filter(Boolean).map((svg) => `<div class="chart">${svg}</div>`)
        .join("");
  }
  const roster = $("roster");
  roster.innerHTML = "";
  for (const r of app.roster()) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = r.kind === "saver"
      ? `<b>${r.name}</b> <span class="addr">saver</span><br>` +
        `${r.state}<br>` +
        `cost $${USD(r.cost)} · mark $${USD(r.mark)} · ` +
        `realized P&amp;L $${USD(r.profit)}`
      : `<b>${r.name}</b> <span class="addr">debtor</span><br>` +
        `mortgage $${USD(r.mortgage)} (untouched: $${USD(r.hypo)})<br>` +
        `drawn $${USD(r.drawn)} · banked $${USD(r.banked)} · ` +
        `net <b>$${USD(r.net)}</b> vs $${USD(r.hypoNet)}`;
    roster.appendChild(card);
  }
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

  await refresh();
}

main().catch((e) => {
  $("status").textContent = "FAILED: " + (e.message ?? e);
  console.error(e);
});
