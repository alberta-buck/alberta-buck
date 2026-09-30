// The sandbox page: boot the world (the wasm kernels, an in-tab EVM, the
// saved world or a new one), then wire the tools to the SandboxApp
// controller.  Everything here is presentation; every chain fact comes from
// the controller.  The Savings tab is the exception: it watches a world on
// a sim server, needs none of the in-tab one, and so the in-tab world boots
// only when one of its own tools is first shown.  Served BY the sim server
// (the savings sandbox) the page is the Savings tab alone: the in-browser
// tools are the static sandbox's, and it links there.
//
// Build: make nix-sandbox-build  (esbuild bundle -> sandbox/dist/app.js)

import { tevmSession } from "../../src/backends.js";
import { loadIdentity } from "../../src/identity-web.js";
import { artifact } from "../../artifacts/sandbox.mjs";
import { SandboxApp } from "./app.js";
import { idbStore } from "./store.js";
import { prefs } from "./ui/dom.js";
import { mountWorldBar, renderWorldBar } from "./ui/worldbar.js";
import { mountIssuer, renderIssuer } from "./ui/issuer.js";
import { mountWallets, renderWallets } from "./ui/wallets.js";
import { mountCredit, renderCredit } from "./ui/credit.js";
import { mountObserver, renderObserver } from "./ui/observer.js";
import { mountSavings } from "./ui/savings.js";

const $ = (id) => document.getElementById(id);
const TABS = ["issuer", "wallets", "credit", "observer", "savings"];
let worldReady = false;
let worldBoot = null;
const ctx = { act, say, lastView: null };
const FRESH_WORLD_TXS = 40;          // about how many transactions a new world takes

function say(text, kind = "") {
  const s = $("status");
  s.className = `status ${kind}`;
  s.textContent = text;
}

// Run a controller action with a busy line; a refusal shows its reason.
// Returns the action's result (or true), or undefined when it was refused.
async function act(label, fn, ok) {
  say(`${label}…`, "busy");
  try {
    const r = await fn();
    say(typeof ok === "function" ? ok(r) : (ok ?? "Done."), "ok");
    return r ?? true;
  } catch (e) {
    say(e.reason ?? e.shortMessage ?? e.message ?? String(e), "error");
    if (!e.reason) console.error(e);
    return undefined;
  }
}

function selectTab(name) {
  for (const t of TABS) {
    const on = t === name;
    $(`tab-${t}`).setAttribute("aria-selected", String(on));
    $(`tab-${t}`).tabIndex = on ? 0 : -1;
    $(`panel-${t}`).hidden = !on;
  }
  prefs.set("tab", name);
  // Two worlds: the tools' own, in this tab, on the clock the world bar moves;
  // and the Savings tab's, on a server, on its own clock.  The badge says which.
  document.body.dataset.mode = name === "savings" ? "savings" : "world";
  const kind = $("world-kind");
  kind.textContent = name === "savings" ? "SERVER WORLD" : "THIS TAB'S WORLD";
  kind.title = name === "savings"
    ? (ctx.savingsOnly
      ? "A whole economy run on a sim server, on its own clock."
      : "The Savings tab watches a separate, much larger world run on a sim server, on its own clock.")
    : "Issuer, Wallets, Credit and Observer share one small world in this browser tab; its clock moves "
      + "only when you move it.  The Savings tab's world is a separate one, on a server.";
  if (name === "savings") {
    $("loading").hidden = true;
    ctx.savings?.activate();
  } else if (!worldReady) {
    $("loading").hidden = false;
    ensureWorld();
  }
}

function mountTabs() {
  for (const t of TABS) {
    $(`tab-${t}`).addEventListener("click", () => selectTab(t));
    $(`tab-${t}`).addEventListener("keydown", (e) => {
      const i = TABS.indexOf(t);
      const j = e.key === "ArrowRight" ? (i + 1) % TABS.length
        : e.key === "ArrowLeft" ? (i + TABS.length - 1) % TABS.length : -1;
      if (j < 0) return;
      selectTab(TABS[j]);
      $(`tab-${TABS[j]}`).focus();
    });
  }
  const asked = new URLSearchParams(location.search).get("tab");
  const saved = TABS.includes(asked) ? asked : ctx.sameOrigin ? "savings" : prefs.get("tab", "issuer");
  selectTab(TABS.includes(saved) ? saved : TABS[0]);
}

function ensureWorld() {
  worldBoot ??= bootWorld().catch((e) => {
    console.error(e);
    $("loading-step").textContent = `The sandbox could not start: ${e.message}`;
  });
  return worldBoot;
}

// Where the Savings tab's worlds are: sim-server.json, which the build
// writes ({"server": null}, or SANDBOX_SIM_SERVER's URL) and the sim server
// answers itself ("same-origin") when it serves the page (--static).
async function simServer() {
  try {
    const r = await fetch("./sim-server.json", { cache: "no-store" });
    return r.ok ? (await r.json()).server ?? null : null;
  } catch {
    return null;
  }
}

async function boot() {
  ctx.simServer = await simServer();
  ctx.sameOrigin = ctx.simServer === "same-origin";
  // No sim server configured (the static site, sandbox.albertabuck.ca) and
  // none asked for (?sim=): the sandbox is the browser-only one, no Savings tab.
  if (ctx.simServer || new URLSearchParams(location.search).get("sim")) {
    if (ctx.sameOrigin) {
      // The savings sandbox: the Savings tab alone.
      ctx.savingsOnly = true;
      for (const t of ["issuer", "wallets", "credit", "observer"]) {
        TABS.splice(TABS.indexOf(t), 1);
        $(`tab-${t}`).remove();
        $(`panel-${t}`).remove();
      }
      document.querySelector(".tabs .tab-sep").remove();
    }
    mountSavings(ctx);
  } else {
    TABS.splice(TABS.indexOf("savings"), 1);
    for (const el of [$("tab-savings"), $("panel-savings"), document.querySelector(".tabs .tab-sep")]) el.remove();
  }
  mountTabs();
}

async function bootWorld() {
  const step = (text, frac) => {
    $("loading-step").textContent = text;
    if (frac !== undefined) $("loading-fill").style.width = `${Math.min(100, frac * 100)}%`;
  };
  step("Loading the cryptography…", 0.05);
  const identity = await loadIdentity("./wasm/buck_identity_bg.wasm");
  const store = await idbStore();
  step("Opening your world…", 0.1);
  let txs = 0;
  const app = await SandboxApp.open({
    identity, artifacts: artifact, store,
    newSession: () => tevmSession(),
    onJournal: (e) => {
      txs += 1;
      if ($("loading").hidden) return;
      step(`Deploying a new world: ${e.tag || e.fn}`, 0.1 + 0.85 * (txs / FRESH_WORLD_TXS));
    },
  });

  Object.assign(ctx, {
    app,
    storageNote: store.persistent ? ""
      : "This browser refuses site storage: the world lasts until the tab closes (Export keeps it).",
  });
  mountWorldBar(ctx);
  mountIssuer(ctx);
  mountWallets(ctx);
  mountCredit(ctx);
  mountObserver(ctx);

  // One render at a time; a change during a render asks for one more.
  let rendering = false;
  let again = false;
  const render = async () => {
    if (rendering) {
      again = true;
      return;
    }
    rendering = true;
    try {
      do {
        again = false;
        const view = await app.view();
        ctx.lastView = view;
        renderWorldBar(ctx, view);
        renderIssuer(ctx, view);
        renderWallets(ctx, view);
        renderCredit(ctx, view);
        renderObserver(ctx, view);
      } while (again);
    } catch (e) {
      console.error("render failed", e);
      say(`Could not read the world: ${e.message}`, "error");
    } finally {
      rendering = false;
    }
  };
  app.onChange(render);
  await render();
  worldReady = true;
  if (document.body.dataset.mode !== "savings") {
    $("loading").hidden = true;
    say("Ready.  Certify someone in the Issuer, then give them a wallet.", "ok");
  }
  globalThis.sandbox = app;          // for the curious, in the console
}

boot().catch((e) => {
  console.error(e);
  $("loading-step").textContent = `The sandbox could not start: ${e.message}`;
});
