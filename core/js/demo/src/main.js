// buckworld.html's DOM glue over the BuckWorldApp controller: everything
// here is presentation; every chain fact comes from the controller (and
// lands in the journal panel via the Session's JournalWriter).
//
// Build: make nix-core-demo-buckworld  (esbuild bundle -> demo/app.js)

import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";

import { tevmSession } from "../../src/backends.js";
import { loadIdentity } from "../../src/identity-web.js";
import { JournalWriter } from "../../src/journal.js";
import { artifact } from "../../artifacts/bundle.mjs";
import mathInit, * as mathWasm from "../wasm-web/buck_math.js";
import { BuckWorldApp, SAMPLE_CITIZENS } from "./app.js";

const $ = (id) => document.getElementById(id);
const BUCK = (v) => (Number(v) / 1e6).toFixed(6);

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
    (j.err ? `  [${j.err}]` : "") + `  (${j.gas} gas)`);
}

let app = null;
let sampleIdx = 0;

async function refresh() {
  const snap = await app.snapshot();
  $("clock").textContent = new Date(Number(snap.clock) * 1000)
    .toISOString().replace("T", " ").slice(0, 19) + " (simulated)";
  if (app.market) {
    const m = await app.marketSnapshot();
    $("market").style.display = "";
    $("market").innerHTML =
      `<b>TOKEN/USDC market</b> · simulated day ${m.day}<br>` +
      `pool spot <b>$${(Number(m.spot) / 1e6).toFixed(4)}</b> · ` +
      `reference $${(Number(m.ref) / 1e6).toFixed(4)} ` +
      `<span class="addr">(whale pins the seeded walk; a trader round-trips daily)</span>`;
  }
  const roster = $("roster");
  roster.innerHTML = "";
  for (const c of snap.citizens) {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML =
      `<b>${c.name}</b> <span class="addr">${c.address.slice(0, 10)}…</span><br>` +
      `balance <b>${BUCK(c.balance)}</b> BUCK` +
      (c.signed < 0n ? `  <span class="drawn">(credit drawn ${BUCK(-c.signed)})</span>` : "") +
      `<br>demurrage owing ${BUCK(c.feeOwing)} · credit limit ${BUCK(c.creditLimit)}`;
    roster.appendChild(card);
  }
  for (const sel of [$("from"), $("to")]) {
    const cur = sel.value;
    sel.innerHTML = "";
    for (const c of app.citizens) {
      const o = document.createElement("option");
      o.value = c.name;
      o.textContent = c.name;
      sel.appendChild(o);
    }
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
  }
  if ($("to").options.length > 1 && $("to").value === $("from").value) {
    $("to").selectedIndex = 1;
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
  $("status").textContent = "loading kernels…";
  const identity = await loadIdentity("./wasm-web/buck_identity_bg.wasm");
  await mathInit({ module_or_path: "./wasm-web/buck_math_bg.wasm" });

  $("status").textContent = "booting in-browser EVM (tevm)…";
  const session = await tevmSession({
    journal: new JournalWriter(journalLine),
  });
  app = new BuckWorldApp({
    session, identity, artifacts: artifact,
    makeAccount: () => privateKeyToAccount(generatePrivateKey()),
    math: mathWasm,
  });
  await app.boot();
  $("status").textContent =
    "live: real BUCK stack on an in-browser EVM; every proof generated in this tab";

  // The opening story: two citizens, a credit line, the handshake, a
  // payment, a month of simulated time -- and the bit-exact badge.
  const chloe = await app.addCitizen(SAMPLE_CITIZENS[sampleIdx++]);
  const bob = await app.addCitizen(SAMPLE_CITIZENS[sampleIdx++]);
  await app.credit(chloe, 1_000_000000n);
  await app.approvePair(chloe, bob);
  const t0 = (await session.client.getBlock()).timestamp;
  await app.pay(chloe, bob, 250_000000n);
  const t1 = await app.jump(30 * BuckWorldApp.DAY);
  const snap = await app.snapshot();
  const owed = snap.citizens.find((c) => c.name === bob.name).feeOwing;
  const predicted = app.predictFee(250_000000n, t1 - t0);
  $("badge").textContent = owed === predicted
    ? `demurrage after 30 simulated days: chain ${BUCK(owed)} == buck-math kernel ${BUCK(predicted)} (bit-exact)`
    : `KERNEL MISMATCH: chain ${BUCK(owed)} != kernel ${BUCK(predicted)}`;
  $("badge").className = owed === predicted ? "ok" : "bad";

  // Wire the controls.
  $("add").onclick = () => act("add citizen", async () => {
    const fields = SAMPLE_CITIZENS[sampleIdx % SAMPLE_CITIZENS.length];
    sampleIdx += 1;
    await app.addCitizen({ ...fields, id_number: `${fields.id_number}-${sampleIdx}` });
  });
  $("credit").onclick = () => act("credit", async () => {
    await app.credit(app.citizen($("from").value), 1_000_000000n);
  });
  $("handshake").onclick = () => act("handshake", async () => {
    await app.approvePair(app.citizen($("from").value), app.citizen($("to").value));
  });
  $("pay").onclick = () => act("pay", async () => {
    const amt = BigInt(Math.round(parseFloat($("amount").value || "0") * 1e6));
    const res = await app.pay(
      app.citizen($("from").value), app.citizen($("to").value), amt);
    if (!res.ok) logLine("j-miss", `payment refused: ${res.reason}`);
  });
  for (const [id_, secs] of [["h1", 3600], ["d1", BuckWorldApp.DAY],
                             ["d30", 30 * BuckWorldApp.DAY]]) {
    $(id_).onclick = () => act("time", () => app.jump(secs));
  }

  // The background market: open once, then tick by button or by timer.
  let timer = null;
  let ticking = false;
  const tickOnce = async () => {
    if (ticking || !app.market) return;
    ticking = true;
    try { await app.marketTick(); } finally { ticking = false; }
    await refresh();
  };
  $("openmkt").onclick = () => act("open market", async () => {
    await app.openMarket();
    $("openmkt").textContent = "market open";
    for (const b of ["mtick", "play", "pause"]) $(b).disabled = false;
  });
  $("mtick").onclick = () => tickOnce();
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
