// The Savings tab: the BuckBasket as a savings facility, in a world far
// bigger than a browser tab can run -- the full stack (the ops basket, the
// undertakings, the work wheel, K, the arbs that pin every TOKEN to its real
// commodity, depositors, retirees) runs on a server (alberta_buck/sim/
// server.py), and this tab watches it day by day, steers it (pause, step,
// shocks, the wheel's gas) and saves in it with a key of its own.
//
// Nothing here is the in-tab world of the other tools: its own chain, its
// own clock, its own key.

import { lineChart } from "./chart.js";
import { fill, h, prefs } from "./dom.js";
import { SimLink } from "../savings/link.js";
import { LocalLink } from "../savings/local-link.js";
import { SAVINGS_BORROWERS } from "../../../src/scenarios/savings-presets.js";
import { cyclePeriod } from "../../../src/scenarios/savings-prices.js";
import { MAX_DEVIATION_BP, Saver } from "../savings/wallet.js";
import { annualizedSavings, channels, chooseIndex, compact, dateOf, deviationBp, equityWorth, newSid, prices,
         row, serverBase, tokenFor, usd } from "../savings/model.js";

const S1 = "var(--s1)";
const S2 = "var(--s2)";
const S3 = "var(--s3)";
const S4 = "var(--s4)";
const COMMODITY_COLORS = [S1, S2, S3, S4, "var(--opaque)"];
const STATES = {
  idle: "not connected", connecting: "connecting…", building: "building the world (about a minute)…",
  live: "live", reconnecting: "reconnecting…", refused: "the server is full", failed: "the world failed",
  done: "the world has run its course", closed: "disconnected", unreachable: "no sim server",
};

const jsonPref = (k, dflt) => {
  try { return JSON.parse(prefs.get(k, "")) ?? dflt; } catch { return dflt; }
};
const f3 = (v) => v.toFixed(3);
const f4 = (v) => v.toFixed(4);
const dollars = (v) => `$${compact(v)}`;
const bucks = (v) => compact(v);
const pct1 = (v) => `${v.toFixed(1)}%`;
const pct2 = (v) => `${v.toFixed(2)}%`;
// The held benchmark starts once the pools have settled: the world's first
// placements move thin pools a long way, and a benchmark struck in that
// churn would credit the basket with the bootstrap's noise.
const HOLD_FROM_DAY = 7;
const signed = (v, f) => (v === null || v === undefined ? "–" : `${v >= 0 ? "+" : ""}${f(v)}`);
const priceFmt = (v) => (v >= 1000 ? compact(v, 4) : v >= 10 ? v.toFixed(1) : v.toFixed(3));

export function mountSavings(ctx) {
  const panel = document.getElementById("panel-savings");
  const q = new URLSearchParams(location.search);
  const S = {
    base: null, sid: null, sets: {}, link: null, info: null, saver: null,
    rows: [], status: null, holdings: null, reading: false, drawQueued: false, readTimer: null,
    started: false,
  };
  ctx.savings = S;
  globalThis.savings = S;            // for the curious, in the console

  // ---- the world card ----------------------------------------------------
  const stateChip = h("span", { class: "chip" }, STATES.idle);
  const kpis = h("div", { class: "stats kpis" });
  const runBtn = h("button", { type: "button", class: "primary", onclick: () => toggleRun() }, "Pause");
  const resetBtn = h("button", { type: "button", class: "danger", onclick: () => resetWorld(),
                                 title: "Start this world over from its first day: the same link, prices and seed" },
    "Reset");
  const stepBtns = [1, 7, 30].map((n) => h("button", {
    type: "button", title: `Run ${n} simulated day${n > 1 ? "s" : ""}, then pause`,
    onclick: () => op(`Running ${n} day${n > 1 ? "s" : ""}`, { op: "step", days: n }),
  }, `${n} day${n > 1 ? "s" : ""}`));
  const chainBtns = ["l2", "l1"].map((name) => h("button", {
    type: "button", "aria-pressed": "false", dataset: { chain: name },
    title: name === "l1" ? "Callers pay mainnet gas: the wheel acts only on wide gaps"
      : "Callers pay a rollup's gas: the wheel closes narrow gaps too",
    onclick: () => op(`Switching the wheel to ${name.toUpperCase()} gas`, { op: "chain", name }),
  }, name.toUpperCase()));
  // The wheel's callers' gas profile: a server world's (a world in this tab has no gas profile).
  const gasGroup = h("span", { class: "seg", role: "group", "aria-label": "the wheel's gas" },
    h("span", { class: "seg-label" }, "Gas"), ...chainBtns);
  const shockUsd = h("input", { type: "number", min: "0.1", step: "0.1", value: "5", "aria-label": "shock size, $ millions",
                                dataset: { key: "savings:shock-usd" }, class: "narrow" });
  const shockDays = h("input", { type: "number", min: "1", step: "1", value: "3", "aria-label": "shock days",
                                 dataset: { key: "savings:shock-days" }, class: "narrow" });
  const shock = (side) => {
    const m = Number(shockUsd.value);
    const days = Math.max(1, Math.round(Number(shockDays.value)));
    if (!(m > 0)) return ctx.say("A shock needs a size in $ millions.", "error");
    return op(side === "buy" ? `Arming a $${m}M BUCK demand shock over ${days} days`
      : `Arming a $${m}M BUCK supply shock over ${days} days`,
    { op: "shock", side, usd: m * 1e6, days });
  };

  // The pace, live: a target number of seconds a simulated day (the world's "pace" op, days a second).
  const secondsIn = h("input", { type: "number", min: "0", step: "0.1", value: prefs.get("savings:seconds", "1"),
                                 "aria-label": "seconds per day", class: "narrow",
                                 onchange: () => setPace() });
  function setPace() {
    const secs = Number(secondsIn.value);
    if (!(secs >= 0)) return ctx.say("The pace needs a number of seconds a day (0: as fast as it runs).", "error");
    prefs.set("savings:seconds", String(secs));
    return op(secs ? `Pacing the world at ${secs} s a day` : "Running the world as fast as it goes",
              { op: "pace", days_per_second: secs ? 1 / secs : 0 });
  }

  // World settings: where, and what a new world is made of.
  const serverIn = h("input", { type: "text", "aria-label": "sim server", dataset: { key: "savings:server-in" },
                                size: 30 });
  const cycleOption = h("option", { value: "three-phase" }, "Commodity cycles (simulated)");
  const sineOption = h("option", { value: "sine" }, "Sine waves with USD inflation");
  const pricesSel = h("select", { "aria-label": "prices", onchange: () => drawPriceOptions() },
    h("option", { value: "history" }, "History (five years of real prices)"),
    h("option", { value: "revert" }, "Reverting (historical volatility, trend removed)"), cycleOption, sineOption);
  const priceSetting = (label, min, max, value) => h("input", { type: "number", "aria-label": label,
    min, max, step: "any", value, class: "narrow" });
  const priceField = (label, input) => h("label", { class: "field" }, h("span", { class: "field-label" }, label), input);
  const noise = priceSetting("Price noise (%)", 0, 50, 0);
  const amplitude = priceSetting("Sine amplitude (%)", 0, 50, 12);
  const inflation = priceSetting("Annual USD inflation (%)", -30, 50, 2);
  const noiseField = priceField("Price noise (±%)", noise);
  const periodFields = h("div", { class: "row" });
  const sineFields = h("div", {}, h("div", { class: "row" }, priceField("Sine amplitude (%)", amplitude),
    priceField("Annual USD inflation (%)", inflation)), periodFields);
  let periodInputs = {};
  function drawPriceOptions() {
    noiseField.hidden = !S.local;
    sineFields.hidden = !S.local || pricesSel.value !== "sine";
  }
  const borrowerSel = h("select", { "aria-label": "borrower scenario" },
    h("option", { value: "" }, "Mixed households"),
    ...Object.entries(SAVINGS_BORROWERS).map(([key, preset]) => h("option", { value: key }, preset.label)));
  const borrowerField = h("label", { class: "field" }, h("span", { class: "field-label" }, "Borrower scenario"), borrowerSel);
  const seedIn = h("input", { type: "text", "aria-label": "seed", placeholder: "default", size: 8,
                              dataset: { key: "savings:seed" } });
  // Ticks a simulated day: the whales re-pin the TOKEN/USDC pools and the arbitrageurs and the wheel
  // act at each, so more ticks track the commodities through the day more closely (and cost more).
  const ticksSel = h("select", { "aria-label": "ticks per day" },
    ...[1, 2, 4, 8, 12, 24].map((n) => h("option", { value: String(n) }, String(n))));
  const householdChoices = {
    surplus_policy: ["Surplus cash", [["adaptive", "Adaptive saving / discounted paydown"],
      ["basket", "Always invest"], ["retire", "Always retire liability"], ["discount", "Retire only at discount"]]],
    ...Object.fromEntries(["debtors", "savers", "investors"].map((role) =>
      [`initial_${role}`, [`Opening ${role}`, [0, 2, 6, 12, 24, 30, 60].map((n) => [n, String(n)])]])),
    ...Object.fromEntries(["debtor", "saver", "investor"].map((role) =>
      [`${role}_rate`, [`New ${role}s / year`, [0, 2, 3, 6, 12, 30, 60].map((n) => [n, String(n)])]])),
  };
  const householdDefaults = { surplus_policy: "adaptive", initial_debtors: 6, initial_savers: 6,
    initial_investors: 30, debtor_rate: 6, saver_rate: 6, investor_rate: 30 };
  const householdSelects = Object.fromEntries(Object.entries(householdChoices).map(([key, [label, choices]]) =>
    [key, h("select", { "aria-label": label }, ...choices.map(([value, text]) => h("option", { value: String(value) }, text)))]));
  const householdFields = Object.entries(householdChoices).map(([key, [label]]) =>
    h("label", { class: "field" }, h("span", { class: "field-label" }, label), householdSelects[key]));
  const worldLink = h("code", { class: "wrap" });
  const newWorldHint = h("p", { class: "hint" });

  const noServer = h("div", { class: "notice", hidden: true });
  const worldCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "The savings world"), stateChip),
    noServer,
    kpis,
    h("div", { class: "row ctl" }, runBtn, resetBtn, h("span", { class: "seg-label" }, "Step"), ...stepBtns,
      gasGroup),
    h("div", { class: "row ctl" },
      h("label", { class: "inline", title: "The world's pace: at most one simulated day each this many seconds "
        + "(0: as fast as it runs). A day that takes longer to compute runs at its own speed." },
        "Pace", secondsIn, "s / day")),
    h("div", { class: "row ctl" },
      h("span", { class: "seg-label" }, "Shock"),
      h("button", { type: "button", title: "Someone buys BUCK with USDC, uncapped, over the days",
                    onclick: () => shock("buy") }, "BUCK demand"),
      h("button", { type: "button", title: "Someone issues BUCK and sells it for USDC, over the days",
                    onclick: () => shock("sell") }, "BUCK supply"),
      h("label", { class: "inline" }, "$", shockUsd, "M over"), h("label", { class: "inline" }, shockDays, "days")),
    h("details", {},
      h("summary", {}, "World settings"),
      h("div", { class: "form" },
        h("label", { class: "field" }, h("span", { class: "field-label" }, "Sim server"),
          h("span", { class: "row" }, serverIn,
            h("button", { type: "button", onclick: () => { prefs.set("savings:server", serverIn.value.trim()); connect(); } },
              "Connect"))),
        h("div", { class: "row" },
          h("label", { class: "field" }, h("span", { class: "field-label" }, "Prices"), pricesSel),
          h("label", { class: "field" }, h("span", { class: "field-label" }, "Seed"), seedIn),
          h("label", { class: "field" }, h("span", { class: "field-label" }, "Ticks / day"), ticksSel), borrowerField),
        h("div", { class: "row" }, ...householdFields),
        h("div", { class: "row" }, noiseField), sineFields,
        h("div", { class: "row" },
          h("button", { type: "button", class: "danger", onclick: () => newWorld() }, "New world")),
        newWorldHint, worldLink)));

  // ---- the saver's card --------------------------------------------------
  const saveUsd = h("input", { type: "number", min: "1", step: "any", value: "10000", "aria-label": "dollars to save",
                               dataset: { key: "savings:usd" } });
  const tokenSel = h("select", { "aria-label": "token to save in", dataset: { key: "savings:token" } });
  const saveBtn = h("button", { type: "button", class: "primary", onclick: () => save() }, "Save");
  const keyLine = h("p", { class: "hint" });
  const receiptsBox = h("div", {});
  const balancesBox = h("div", {});
  const householdPosition = h("div", {});
  const householdSelector = h("select", { "aria-label": "household basket position",
    onchange: () => drawHouseholdPosition(S.rows.at(-1)) });
  let householdRoster = "";
  const householdCard = h("div", { class: "card" }, h("h3", {}, "Household basket position"),
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Household"), householdSelector),
    householdPosition, h("p", { class: "hint" }, "Open receipts only, in BUCK. The mark uses the basket's "
      + "share price before exit costs. Return is mark divided by remaining deposit basis, less one; "
      + "closed positions and realized gains are not included."));
  const borrowerTitle = h("h3", {});
  const borrowerDetails = h("div", {});
  const borrowerChart = lineChart({ title: "Debt remaining (USD)", fmt: dollars, series: [
    { label: "Mortgage", color: S1 }, { label: "Mortgage + BUCK at spot", color: S2 },
    { label: "Conventional mortgage", color: S4, dash: true },
  ] });
  const borrowerCard = h("div", { class: "card", hidden: true }, borrowerTitle, borrowerDetails,
    borrowerChart.el, h("p", { class: "hint" }, "Income and collateral value are fixed in this model. Commodity prices move the markets used for refinancing and BUCK paydown."));
  const forecastBox = h("div", { class: "card", hidden: true });
  const rebalancesBox = h("div", { class: "card", hidden: true });
  // How a saving works on the world's basket (the equity basket; the pro-rata one is retired).
  const HOW = {
    equity: ["Saving mints the TOKEN at its market price (the world's TOKENs are faucets), ",
      "deposits it and takes a receipt: shares of the basket's equity.  The basket draws K of credit ",
      "against it and its wheel places it in the pools; redeeming pays your shares' value in BUCK, less ",
      "the treasury's quarter of any gain.  Your key is registered as an identity (with the world's issuer) ",
      "before its first redemption: only identities may hold BUCK."],
  };
  const howLine = h("p", { class: "hint" }, HOW.equity);
  const walletCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Your savings")),
    keyLine,
    h("div", { class: "row" },
      h("label", { class: "field" }, h("span", { class: "field-label" }, "Save (USD)"), saveUsd),
      h("label", { class: "field" }, h("span", { class: "field-label" }, "In"), tokenSel), saveBtn),
    howLine,
    receiptsBox, balancesBox);

  // ---- the charts --------------------------------------------------------
  const charts = {
    householdNet: lineChart({ title: "Households: financial net worth (USD)", fmt: dollars, series: [
      { label: "BUCK route", color: S1 }, { label: "untouched mortgage", color: S2 }],
      note: "Arrived households only, with the same starting cash and income. Includes basket assets, insurance "
        + "principal and unpaid expenses; excludes the insured property common to both paths. Basket shares "
        + "are marked before exit costs; pro-rata receipts use principal. New arrivals change the totals." }),
    householdMortgage: lineChart({ title: "External mortgages remaining (USD)", fmt: dollars, series: [
      { label: "BUCK route", color: S1 }, { label: "untouched mortgage", color: S2 }] }),
    householdFlows: lineChart({ title: "Households: cumulative allocation", fmt: bucks, series: [
      { label: "basket purchases (USD)", color: S1 }, { label: "liabilities retired (BUCK)", color: S2 }],
      note: "Purchases include TOKEN awaiting an accepted deposit. The lines use different currencies, "
        + "so their levels are not a return comparison." }),
    householdBasket: lineChart({ title: "Households: open basket positions (BUCK)", fmt: bucks, series: [
      { label: "capital at risk", color: S1 }, { label: "share mark", color: S2 }],
      note: "Remaining deposit basis and current share mark, before exit costs. Deposits and redemptions "
        + "change these totals; their difference is the mark on open positions, not realized profit." }),
    buck: lineChart({ title: "BUCK", ref: 1, fmt: f3, series: [
      { label: "USD per BUCK (pool)", color: S1 }, { label: "the basket, in BUCK", color: S2 }],
      note: "K steers the basket's BUCK price to 1; the BUCK/USDC pool says what a BUCK fetches." }),
    k: lineChart({ title: "K: credit per unit of insured value", fmt: f4, series: [{ label: "K", color: S3 }] }),
    index: lineChart({ title: "The savings index", ref: 1, fmt: f4, series: [
      { label: "paid per BUCK saved", color: S1 }, { label: `its commodities, held from day ${HOLD_FROM_DAY}`, color: S4, dash: true }],
      note: "What a saving is worth, in BUCK value, per BUCK deposited (the basket's share price), against "
        + "the basket's declared commodities bought on the day the pools have settled and simply held: the "
        + "gap is what the basket's work has added -- the pools' fees, the wheel's captures, the timing "
        + "of its rebalancing -- or cost." }),
    wheel: lineChart({ title: "The work wheel (cumulative)", fmt: dollars, series: [
      { label: "to depositors", color: S1 }, { label: "to callers", color: S2 }, { label: "gas", color: S4 }],
      note: "Consistency cycles through TOKEN/USDC, TOKEN/BUCK and BUCK/USDC: the harvest reaches the depositors." }),
    ops: lineChart({ title: "The undertakings' open books (BUCK)", fmt: bucks, series: [
      { label: "issued", color: S1 }, { label: "absorbed", color: S2 }],
      note: "BUCK the undertakings have issued at a premium and not yet bought back, and BUCK bought at a "
        + "discount and not yet sold back." }),
    earned: lineChart({ title: "What the desks earned over holding (BUCK)", ref: 0, fmt: bucks, series: [
      { label: "the monetary desk", color: S1 }, { label: "the undertakings", color: S2 }],
      note: "Each desk's book -- what it owns, less what it has drawn -- against the commodities it started "
        + "with simply held, at the same prices: above zero, operating has paid." }),
    credit: lineChart({ title: "The basket's credit (BUCK)", fmt: bucks, series: [
      { label: "equity", color: S1 }, { label: "lien", color: S2 }, { label: "liquidity", color: S3 }],
      note: "The depositors' equity; the BUCK the basket has drawn (at most K times that equity) to pair "
        + "its commodities in the pools; the BUCK it keeps ready for exits." }),
    machineSteps: lineChart({ title: "What the basket's wheel did (cumulative BUCK)", fmt: bucks, series: [
      { label: "paired", color: S1 }, { label: "sold", color: S2 }, { label: "bought", color: S3 }],
      note: "A deposit is paired at once with BUCKs the basket issues beside it: nothing is sold to place "
        + "it.  After that each commodity is worked on its own toward its target: one above it is sold "
        + "and one below it bought, held back while its price is still parting from the others and then "
        + "released at a steady rate.  The basket's credit carries the difference between the two." }),
    deskOps: lineChart({ title: "The desk's operations (cumulative BUCK)", fmt: bucks, series: [
      { label: "absorbed (Q1)", color: S1 }, { label: "retired (Q2)", color: S2 },
      { label: "supplied (Q3)", color: S3 }, { label: "issued (Q4)", color: S4 }],
      note: "Q1 buys cheap BUCK with the desk's reserve, Q3 sells it back when BUCK is dear: supply "
        + "unchanged.  Q4 issues when BUCK stays dear, Q2 retires when it stays cheap." }),
    machineSignal: lineChart({ title: "The desk's signal: BUCK over its basket (%)", ref: 0, fmt: pct2, series: [
      { label: "premium (as last sampled)", color: S1 }],
      note: "Above zero BUCK is dear, and the desk supplies or issues; below, cheap, and it absorbs or "
        + "retires.  It acts only beyond an outer band, and outright only once the reading has stayed "
        + "beyond it for days on end (the Desk signal above counts them)." }),
    deskInv: lineChart({ title: "The desk's inventory (BUCK)", fmt: bucks, series: [
      { label: "BUCK it holds", color: S1 }, { label: "BUCK it has issued", color: S2 }],
      note: "What its operations have left it holding: read it beside what it earned -- a desk that "
        + "damps BUCK by holding a position it could never unwind has moved the loss, not removed it." }),
  };
  const leanGrid = h("div", { class: "chart-grid small" });
  S.lean = [];
  const leanTitle = h("h3", { class: "grid-title" },
    "The basket's lean against price: each commodity's weight, % (actual, leaned target, declared)");
  const commodityGrid = h("div", { class: "chart-grid small" });
  S.commodity = [];

  // Where the world runs decides the intro: a sim server's economy, or this tab's (connect()).
  const intro = h("p", { class: "intro" });
  const describe = (local) => fill(intro, local
    ? ["The Alberta Buck economy runs in this tab. Set the borrower, commodity prices and population under ",
       "World settings. Watch households refinance, save and invest as TOKEN, BUCK and USDC prices move. ",
       "Use the shock controls or save with the simulated wallet. A reload starts a new world.",
       ctx.savingsOnly ? "" : "  It is a separate world from the other tabs', on a clock of its own."]
    : ["A whole Alberta Buck economy runs on a server: five years of real commodity ",
       "prices (or their reverting twins: the same commodities cycling, trend removed), the BuckBasket and ",
       "its work wheel, the monetary desk and the undertakings, K, arbitrageurs pinning every TOKEN to its ",
       "commodity, depositors and retirees.  Watch it day by day, shock it, and save in it: the basket harvests the commodities' ",
       "cycles and the gaps between BUCK, USDC and the TOKENs for its depositors.",
       ctx.savingsOnly ? "" : "  It is a separate world from the other tabs', which runs in your browser on a "
         + "clock of its own."]);
  fill(panel,
    intro,
    ctx.savingsOnly ? h("p", { class: "hint" }, "The sandbox's other tools -- an identity issuer, wallets ",
      "and payments, insured credit, what an observer of the chain sees -- run in your browser, in a small ",
      "world of their own: ", h("a", { href: "https://sandbox.albertabuck.ca/" }, "sandbox.albertabuck.ca"), ".")
      : null,
    h("div", { class: "cols savings-cols" },
      h("div", { class: "stack" }, worldCard, borrowerCard, walletCard, householdCard),
      h("div", { class: "stack" },
        forecastBox, rebalancesBox,
        h("div", { class: "chart-grid" }, charts.buck.el, charts.index.el, charts.wheel.el, charts.k.el),
        h("h3", { class: "grid-title" }, "Households: refinancing and investing"),
        h("div", { class: "chart-grid" }, charts.householdNet.el, charts.householdMortgage.el,
          charts.householdFlows.el, charts.householdBasket.el),
        h("h3", { class: "grid-title" }, "The basket: its credit, and what its wheel did"),
        h("div", { class: "chart-grid" }, charts.credit.el, charts.machineSteps.el),
        leanTitle,
        leanGrid,
        h("h3", { class: "grid-title" }, "The desks: what they did, and what it earned"),
        h("div", { class: "chart-grid" }, charts.machineSignal.el, charts.deskOps.el, charts.earned.el,
          charts.deskInv.el, charts.ops.el),
        h("h3", { class: "grid-title" }, "Commodity prices: reference, TOKEN/USDC and via BUCK"),
        commodityGrid)));

  // ---- behaviour ---------------------------------------------------------

  async function op(label, msg) {
    if (!S.link) return undefined;
    return ctx.act(label, async () => {
      await S.link.op(msg);
      setTimeout(pollStatus, 300);
    }, msg.op === "step" ? `${label}: queued for the day's end.` : "Queued for the day's end.");
  }

  // Rebuild this world from its first day, on the server, in place: everyone
  // watching its link sees it start over.  A world that has run its course
  // has no day left to take the op, so a fresh connection rebuilds it.
  function resetWorld() {
    if (!S.link) return undefined;
    if (!confirm("Reset this world to its first day?  Everyone watching its link sees it start over, "
      + "and savings in it are gone.")) return undefined;
    if (S.link.state === "done") return connect();
    return op("Resetting the world", { op: "reset" });
  }

  // The server said the world starts over: forget the old one's days, its
  // key's holdings and its receipts, and wait for the new one's first frame.
  function forgetWorld() {
    S.rows = [];
    S.info = null;
    S.saver = null;
    S.holdings = null;
    clearPriceViews();
    keepReceipts([]);
    S.commodity = [];
    commodityGrid.replaceChildren();
    S.lean = [];
    leanGrid.replaceChildren();
    draw();
  }

  function toggleRun() {
    const paused = S.status?.paused;
    return op(paused ? "Running the world" : "Pausing the world", { op: paused ? "resume" : "pause" });
  }

  function connect() {
    S.link?.close();
    S.rows = [];
    S.info = null;
    S.saver = null;
    S.holdings = null;
    clearPriceViews();
    S.commodity = [];
    commodityGrid.replaceChildren();
    S.lean = [];
    leanGrid.replaceChildren();
    // The world runs in this tab (a Web Worker on webevm) unless a server is asked for (?sim=), named
    // under World settings, or serves this page; ?sim=local insists on the tab.
    const saved = prefs.get("savings:server", "");
    S.local = q.get("sim") === "local" || (!q.get("sim") && !saved && !ctx.simServer);
    S.base = S.local ? "local" : serverBase({ param: q.get("sim"), configured: ctx.simServer, saved, location });
    ctx.savingsLocal = S.local;
    describe(S.local);
    ctx.onSavingsWorld?.();
    serverIn.value = S.local ? "" : S.base;
    S.sid = q.get("world") || prefs.get("savings:sid", "") || newSid();
    prefs.set("savings:sid", S.sid);
    S.sets = jsonPref("savings:sets", {});
    borrowerField.hidden = !S.local;
    cycleOption.hidden = cycleOption.disabled = !S.local;
    sineOption.hidden = sineOption.disabled = !S.local;
    noise.value = S.sets["scenario.price_noise_pct"] ?? 0;
    amplitude.value = S.sets["scenario.sine_amplitude_pct"] ?? 12;
    inflation.value = S.sets["scenario.usd_inflation_pct"] ?? 2;
    periodInputs = {};
    fill(periodFields);
    borrowerSel.value = S.local ? S.sets["scenario.borrower"] ?? "" : "";
    if (!S.local) {
      delete S.sets["scenario.borrower"];
      if (["three-phase", "sine"].includes(S.sets["scenario.prices"])) delete S.sets["scenario.prices"];
      for (const key of Object.keys(S.sets)) {
        if (/^scenario\.(price_noise_pct|usd_inflation_pct|sine_)/.test(key)) delete S.sets[key];
      }
    }
    for (const [key, select] of Object.entries(householdSelects)) {
      select.value = String(S.sets[`agents.HouseholdPopulationAgent.${key}`] ?? householdDefaults[key]);
    }
    pricesSel.value = S.sets["scenario.prices"] || "history";
    drawPriceOptions();
    seedIn.value = S.sets["scenario.seed"] ?? "";
    const u = new URL(location.href);
    u.search = "";
    u.searchParams.set("tab", "savings");
    if (!ctx.sameOrigin) u.searchParams.set("sim", S.base);
    u.searchParams.set("world", S.sid);
    fill(newWorldHint, S.local ? "A borrower preset adds one household to the selected population. Start a new world to apply these settings."
      : "Start a new world to apply these settings. Anyone with the current world’s link can watch and steer it.");
    worldLink.hidden = S.local;
    worldLink.textContent = S.local ? "" : u.toString();
    const Link = S.local ? LocalLink : SimLink;
    S.link = new Link({
      urls: S.local ? undefined : channels(S.base, S.sid, S.sets),
      sets: S.sets,
      onState: (state, detail) => {
        if (state === "building") S.rows = [];          // the replay redraws from day 0
        stateChip.textContent = STATES[state] ?? state;
        stateChip.className = `chip ${state === "live" ? "good" : ["failed", "refused"].includes(state) ? "bad" : ""}`;
        if (detail) ctx.say(`The world: ${detail}`, "error");
        noServer.hidden = state !== "unreachable";
        if (state === "unreachable") {
          noServer.replaceChildren(`No sim server answers at ${S.base}.  This tab watches a world run on one: `,
            h("code", {}, "make nix-venv-sim-savings"), " serves this page and its worlds together at ",
            h("code", {}, "http://127.0.0.1:8797/"), ", or name another server under World settings (or ",
            h("code", {}, "?sim=wss://…"), " in the address).");
        }
        if (state === "live" && !S.info) loadInfo();
      },
      onReset: () => forgetWorld(),
      onProgress: (time) => {
        S.priceTime = time;
        drawForecast();
      },
      onFrame: (f) => {
        S.rows.push(row(f));
        queueDraw();
        clearTimeout(S.readTimer);
        S.readTimer = setTimeout(readHoldings, 30);
      },
    });
    S.link.connect();
    pollStatus();
    draw();
  }

  async function loadInfo() {
    const link = S.link;
    for (let i = 0; i < 60 && S.link === link; i++) {
      try {
        const info = await link.call("sim_info");
        if (S.link !== link) return;
        if (info && info.basket) {
          S.info = info;
          const key = prefs.get("savings:key", "");
          S.saver = new Saver({ link: S.link, info, key });
          fill(howLine, HOW.equity);
          for (const [key, select] of Object.entries(householdSelects)) {
            select.value = String(info.households?.[key] ?? householdDefaults[key]);
          }
          ticksSel.value = String(info.ticks_per_day ?? S.sets["scenario.ticks_per_day"] ?? (S.local ? 4 : 2));
          if (prefs.get("savings:seconds", "") !== "") setPace();
          prefs.set("savings:key", S.saver.key);
          buildCommodities();
          if (S.local) {
            buildPriceViews();
            link.call("sim_price_tape").then((tape) => {
              if (S.link !== link || S.info !== info) return;
              S.priceTape = tape;
              drawForecast();
            }).catch((err) => {
              if (S.link === link && S.info === info) ctx.say(`Price preview: ${err.message}`, "error");
            });
          }
          draw();
          readHoldings();
          return;
        }
      } catch { /* not built yet */ }
      await new Promise((r) => setTimeout(r, 2000));
    }
  }

  async function pollStatus() {
    if (!S.link) return;
    try {
      S.status = await S.link.call("sim_status");
      drawControls();
    } catch { /* between connections */ }
  }
  setInterval(() => { if (!panel.hidden && S.link?.state === "live") pollStatus(); }, 4000);

  async function readHoldings() {
    if (!S.saver || S.reading) return;
    S.reading = true;
    try {
      S.holdings = await S.saver.holdings(receipts().filter((r) => !r.redeemedDay).map((r) => r.id));
      drawWallet();
    } catch (e) {
      console.warn("savings: reading holdings", e);
    } finally {
      S.reading = false;
    }
  }

  const receiptsKey = () => `savings:receipts:${S.sid}`;
  const receipts = () => jsonPref(receiptsKey(), []);
  const keepReceipts = (list) => prefs.set(receiptsKey(), JSON.stringify(list));

  async function save() {
    const last = S.rows[S.rows.length - 1];
    if (!S.saver || !last) return ctx.say("The world is not ready yet.", "error");
    const dollarsIn = Number(saveUsd.value);
    if (!(dollarsIn > 0)) return ctx.say("Save how many dollars?", "error");
    const i = tokenSel.value === "hint" ? choice(last) : Number(tokenSel.value);
    const tok = S.info.tokens[i];
    const p = prices(last, i).usdc;
    const dev = devs()[i];
    if (dev !== undefined && dev > MAX_DEVIATION_BP) {
      return ctx.say(`${tok.symbol}'s pool is ${(dev / 100).toFixed(1)}% off its recent average: the basket `
        + `refuses deposits past ${MAX_DEVIATION_BP / 100}% (a guard against being priced by a pool that just `
        + "moved).  Choose another, or wait a day.", "error");
    }
    saveBtn.disabled = true;
    try {
      await ctx.act(`Saving ${usd(dollarsIn)} in ${tok.symbol} (it lands between simulated days)`, async () => {
        const amount = tokenFor(dollarsIn, p, tok.decimals);
        const r = await S.saver.save(i, amount, S.holdings?.balances?.[i] ?? 0n);
        keepReceipts([...receipts(), { id: r.id.toString(), day: last.day, usd: dollarsIn, i,
                                       amount: r.tokenAmount.toString() }]);
        await readHoldings();
        return r;
      }, (r) => (S.saver.equity
        ? `Saved: receipt #${r.id}, ${usd(dollarsIn)} of ${tok.symbol}: the basket's equity, `
          + `drawing ${compact(Number(r.buckMinted) / 1e6)} BUCK of credit.`
        : `Saved: receipt #${r.id}, ${usd(dollarsIn)} of ${tok.symbol} partnered with `
          + `${compact(Number(r.buckMinted) / 1e6)} BUCK.`));
    } finally {
      saveBtn.disabled = false;
    }
  }

  async function redeem(rec, worthUsd) {
    const last = S.rows[S.rows.length - 1];
    await ctx.act(`Redeeming receipt #${rec.id} (it lands between simulated days)`, async () => {
      const { paid } = await S.saver.redeem(rec.id);
      // BUCK at the BUCK/USDC pool; TOKEN (a pro-rata payout, or an equity
      // exit in kind) at its TOKEN/USDC pool.
      const got = paid.reduce((a, p) => a + (p.i < 0 ? (Number(p.amount) / 1e6) * last.bu
        : (Number(p.amount) / 10 ** S.info.tokens[p.i].decimals) * prices(last, p.i).usdc), 0);
      keepReceipts(receipts().map((r) => (r.id === rec.id ? { ...r, redeemedDay: last?.day, paidUsd: got, paid:
        paid.map((p) => [p.i, p.amount.toString()]) } : r)));
      await readHoldings();
      return { got, what: paidIn(paid.map((p) => p.i)) };
    }, ({ got, what }) => `Redeemed #${rec.id}: paid ${usd(got)} in ${what || "nothing"} (quoted ${usd(worthUsd)}).`);
  }

  // What a payout came in: "BUCK", "TOKENs", or "BUCK and TOKENs".
  function paidIn(indexes) {
    const buck = indexes.some((i) => i < 0);
    const toks = indexes.some((i) => i >= 0);
    return [buck && "BUCK", toks && "TOKENs"].filter(Boolean).join(" and ");
  }

  function newWorld() {
    const priceInputs = pricesSel.value === "sine" ? [noise, amplitude, inflation, ...Object.values(periodInputs)] : [noise];
    if (S.local && priceInputs.some((input) => !input.reportValidity())) return;
    if (!confirm(S.local ? "Start a new world? The current world's balance and receipts will be cleared."
      : "Start a new world? This one keeps running until it is idle, but this tab leaves it.")) return;
    const sets = {};
    for (const [key, select] of Object.entries(householdSelects)) sets[`agents.HouseholdPopulationAgent.${key}`] = select.value;
    if (pricesSel.value !== "history") sets["scenario.prices"] = pricesSel.value;
    if (S.local) {
      if (noise.value) sets["scenario.price_noise_pct"] = noise.value;
      if (pricesSel.value === "sine") {
        sets["scenario.sine_amplitude_pct"] = amplitude.value;
        sets["scenario.usd_inflation_pct"] = inflation.value;
        for (const [symbol, input] of Object.entries(periodInputs)) sets[`scenario.sine_period.${symbol}`] = input.value;
      }
    }
    if (S.local && borrowerSel.value) sets["scenario.borrower"] = borrowerSel.value;
    if (seedIn.value.trim()) sets["scenario.seed"] = seedIn.value.trim();
    sets["scenario.ticks_per_day"] = ticksSel.value;
    const chain = S.status?.chain;
    if (chain && chain !== "l2") sets["agents.BasketWheelAgent.chain"] = chain;
    prefs.set("savings:sets", JSON.stringify(sets));
    prefs.set("savings:sid", newSid());
    if (q.get("world")) {
      q.delete("world");
      history.replaceState(null, "", `${location.pathname}?${q}`);
    }
    connect();
  }

  function buildCommodities() {
    S.commodity = S.info.tokens.map((t) => lineChart({ title: t.symbol, fmt: priceFmt, height: 100, series: [
      { label: "reference", color: S4, dash: true }, { label: "TOKEN/USDC", color: S1 }, { label: "via BUCK", color: S2 }] }));
    commodityGrid.replaceChildren(...S.commodity.map((c) => c.el));
    S.lean = S.info.tokens.map((t) => lineChart({ title: t.symbol, fmt: pct1, height: 90, series: [
      { label: "actual", color: S1 }, { label: "leaned", color: S2 }, { label: "declared", color: S4, dash: true }] }));
    leanGrid.replaceChildren(...S.lean.map((c) => c.el));
    fill(tokenSel, h("option", { value: "hint" }, "the basket's choice"),
      S.info.tokens.map((t, i) => h("option", { value: String(i) }, t.symbol)));
  }

  function clearPriceViews() {
    Object.assign(S, { priceTape: null, priceTime: null, forecastStart: null, forecast: null, rebalances: null });
    forecastBox.hidden = rebalancesBox.hidden = true;
    fill(forecastBox);
    fill(rebalancesBox);
  }

  function buildPriceViews() {
    const series = S.info.tokens.map(({ symbol }, i) => ({ label: symbol, color: COMMODITY_COLORS[i % COMMODITY_COLORS.length] }));
    periodInputs = Object.fromEntries(series.map(({ label }, i) => [label, priceSetting(`${label} period (days)`, 2, 1827,
      S.sets[`scenario.sine_period.${label}`] ?? cyclePeriod(i))]));
    fill(periodFields, Object.entries(periodInputs).map(([symbol, input]) => priceField(`${symbol} period (days)`, input)));
    S.forecast = lineChart({ title: "Upcoming commodity reference prices", fmt: (n) => `${n.toFixed(3)}×`, series,
      note: "USD prices indexed to day 0. The vertical line follows the current simulated tick." });
    fill(forecastBox, S.forecast.el);
    S.rebalances = lineChart({ title: "Basket rebalancing executions", fmt: (n) => `${n.toFixed(3)}×`, series,
      note: "▲ Buy · ▼ Sell. Lines connect fills from the same wheel response. BUCK prices are indexed to day 0." });
    fill(rebalancesBox, S.rebalances.el);
    rebalancesBox.hidden = false;
  }

  function drawForecast() {
    const tape = S.priceTape;
    if (!S.forecast || !tape?.[0]?.length) return;
    forecastBox.hidden = false;
    const now = S.priceTime ?? S.rows.at(-1)?.day ?? 0;
    const start = Math.max(0, Math.floor(now / 30) * 30 - 7);
    if (S.forecastStart !== start) {
      const lines = tape.map((feed) => feed.slice(start, start + 97).map((price) => price / feed[0]));
      S.forecast.update(lines[0].map((_, i) => start + i), lines);
      S.forecastStart = start;
    }
    S.forecast.setPlayhead(Math.min(now, tape[0].length - 1));
  }

  function drawRebalances(rows) {
    if (!S.rebalances || !rows.length) return;
    const shown = rows.slice(-120);
    const tokens = S.info.tokens, base = rows[0].sb;
    const markers = shown.flatMap((r) => r.rebalanceFills.flatMap((fill) => {
      const i = fill.token;
      if (!base[i] || !tokens[i]) return [];
      const { symbol, decimals } = tokens[i];
      const amount = Number(fill.token_amount) / 10 ** decimals;
      const buck = Number(fill.buck_amount) / 1e6;
      const price = buck / amount;
      const urgency = fill.urgency === null ? "unavailable" : `${(Number(fill.urgency) / 86400e18).toPrecision(4)} share-days`;
      const gap = fill.gap === null ? "unavailable" : pct2(Number(fill.gap) / 1e16);
      const planned = fill.planned_sell === null ? "unavailable" : `${bucks(Number(fill.planned_sell) / 1e6)} BUCK`;
      return [{ x: r.day + fill.tick / S.info.ticks_per_day, y: price * 1e6 / base[i], group: fill.group,
        color: COMMODITY_COLORS[i % COMMODITY_COLORS.length], side: fill.side,
        detail: `Day ${r.day}, tick ${fill.tick + 1} · ${fill.side} ${compact(amount)} ${symbol} for ${bucks(buck)} nominal BUCK. `
          + `Execution price ${priceFmt(price)} BUCK per ${symbol}. Urgency ${urgency}. Gap ${gap}. `
          + `Planned amount ${planned}. Binding cap unavailable.` }];
    }));
    S.rebalances.update(shown.map((r) => r.day), tokens.map((_, i) =>
      shown.map((r) => r.sb[i] && base[i] ? r.sb[i] / base[i] : null)), markers);
  }

  function drawHouseholdPosition(last) {
    const positions = last?.positions ?? [];
    const roster = positions.map((a) => `${a.member}:${a.role}`).join("|");
    if (roster !== householdRoster) {
      const selected = householdSelector.value;
      fill(householdSelector, positions.map((a) => h("option", { value: a.member }, `${a.member} (${a.role})`)));
      if (positions.some((a) => a.member === selected)) householdSelector.value = selected;
      householdRoster = roster;
    }
    const a = positions.find((p) => p.member === householdSelector.value);
    fill(householdPosition, a ? h("dl", { class: "kv" },
      h("dt", {}, "Open capital at risk"), h("dd", {}, `${bucks(a.atRisk ?? 0)} BUCK`),
      h("dt", {}, "Share mark"), h("dd", {}, a.mark === null ? "Unavailable" : `${bucks(a.mark)} BUCK`),
      h("dt", {}, "Marked return"), h("dd", {}, a.roi === null ? "–" : signed(a.roi * 100, pct2)))
      : h("p", { class: "empty" }, "No arrived households yet."));
  }

  function queueDraw() {
    if (S.drawQueued) return;
    S.drawQueued = true;
    requestAnimationFrame(() => {
      S.drawQueued = false;
      draw();
    });
  }

  function draw() {
    const rows = S.rows;
    const last = rows[rows.length - 1];
    const stat = (k, v, title) => h("div", { class: "stat", title }, h("span", { class: "k" }, k), h("span", { class: "v" }, v));
    fill(kpis, last ? [
      stat("Day", `${last.day}`, dateOf(S.info?.start_date, last.day) || undefined),
      stat("Date", dateOf(S.info?.start_date, last.day) || "–"),
      stat("BUCK", `$${last.bu.toFixed(4)}`, "USD per BUCK in the BUCK/USDC pool"),
      stat("Basket", `${last.bv.toFixed(4)} BUCK`, "What the basket costs in BUCK (K steers it to 1)"),
      stat("K", last.k.toFixed(4)),
      last.households ? stat("Households", `${last.households.active} / ${last.households.scheduled}`,
        `${last.households.savers} insured savers, ${last.households.investors} investors; the rest hold mortgages.`) : null,
      last.households ? stat("Household decisions", last.households.errors ? `${last.households.errors} waiting after errors` : "ready",
        "Members with an execution error at their latest decision; pending assets remain in their accounts.") : null,
      stat("Savings index", last.D === null ? "–" : last.D.toFixed(4), "Paid per BUCK saved, in BUCK value"),
      stat("A year", signed(annualizedSavings(rows), (v) => `${(v * 100).toFixed(2)}%`),
        "The savings index's growth, compounded to a year"),
      stat("Over holding", signed(overHolding(rows), (v) => `${(v * 100).toFixed(2)}%`),
        `The savings index against the basket's commodities held from day ${HOLD_FROM_DAY}`),
      stat("Pool fees", last.eqSync === null ? "–" : bucks(last.eqSync), "BUCK the basket's pools have paid it in fees"),
      stat("Desk", last.deskNav === null || last.deskHold === null ? "–"
        : signed(last.deskNav - last.deskHold, bucks), "The monetary desk's book against its grant held, BUCK"),
      last.deskSide === null ? null : stat("Desk signal",
        last.deskSide === 0 ? "in band" : `${last.deskSide > 0 ? "cheap" : "dear"}, ${last.deskDays} d`,
        "Whether BUCK has stayed cheap or dear beyond the desk's band, and for how many days"),
      stat("To depositors", dollars(last.credited), "The work wheel's harvest, credited to the depositors"),
      stat("Pace", last.pace > 0 ? `${(1 / last.pace).toFixed(1)} s/day` : "–"),
      stat("Prices", !S.info ? "–" : S.info.local ? ({ revert: "reverting", "three-phase": "commodity cycles" }[S.info.prices?.[0]] ?? S.info.prices?.[0] ?? "–")
        : String(S.info.prices?.[0] ?? "").startsWith("rev-") ? "reverting" : "history",
        "Set when the world is made (World settings)"),
    ] : [h("span", { class: "hint" }, S.link ? STATES[S.link.state] : "")]);
    drawControls();
    drawBorrower(rows, last);
    drawForecast();
    if (rows.length === 0) return;
    drawRebalances(rows);
    const xs = rows.map((r) => r.day);
    charts.buck.update(xs, [rows.map((r) => r.bu || null), rows.map((r) => r.bv || null)]);
    charts.k.update(xs, [rows.map((r) => r.k || null)]);
    charts.householdNet.update(xs, ["nw", "hypo"].map((key) => rows.map((r) => r.households?.[key] ?? null)));
    charts.householdMortgage.update(xs, ["mortgage", "hypo_mortgage"].map((key) => rows.map((r) => r.households?.[key] ?? null)));
    charts.householdFlows.update(xs, ["invested_usd", "unwound"].map((key) => rows.map((r) => r.households?.[key] ?? null)));
    charts.householdBasket.update(xs, ["basket_at_risk", "basket_mtm"].map((key) => rows.map((r) => r.households?.[key] ?? null)));
    drawHouseholdPosition(last);
    charts.index.update(xs, [rows.map((r) => r.D), heldLine(rows)]);
    charts.credit.update(xs, [rows.map((r) => r.E), rows.map((r) => r.L), rows.map((r) => r.Q)]);
    charts.machineSteps.update(xs, [rows.map((r) => r.eqPlace), rows.map((r) => r.eqSell), rows.map((r) => r.eqBuy)]);
    charts.machineSignal.update(xs, [rows.map((r) => r.premium)]);
    charts.deskOps.update(xs, [0, 1, 2, 3].map((q) => rows.map((r) => r.dkQ[q])));
    const gap = (a, b) => (a === null || b === null ? null : a - b);
    charts.earned.update(xs, [rows.map((r) => gap(r.deskNav, r.deskHold)), rows.map((r) => gap(r.utNw, r.utHold))]);
    charts.deskInv.update(xs, [rows.map((r) => r.deskHeld), rows.map((r) => r.deskOut)]);
    S.lean.forEach((c, i) => {
      const at = (v) => (v && v[i] !== undefined ? v[i] / 100 : null);
      c.update(xs, [rows.map((r) => at(r.w)), rows.map((r) => at(r.lean)), rows.map((r) => at(r.decl))]);
    });
    charts.wheel.update(xs, [rows.map((r) => r.credited), rows.map((r) => r.callers), rows.map((r) => r.gasUsd)]);
    charts.ops.update(xs, [rows.map((r) => r.utIssued), rows.map((r) => r.utAbsorbed)]);
    S.commodity.forEach((c, i) => {
      const p = rows.map((r) => prices(r, i));
      c.update(xs, [p.map((v) => v.ref || null), p.map((v) => v.usdc || null), p.map((v) => v.viaBuck || null)]);
    });
    drawWallet();
  }

  function drawBorrower(rows, last) {
    const preset = SAVINGS_BORROWERS[S.info?.borrower];
    const b = last?.borrower;
    borrowerCard.hidden = !preset || !b;
    if (!preset || !b) return;
    borrowerTitle.textContent = preset.label;
    const { home_k, ltv, income_frac, apr } = preset.settings;
    fill(borrowerDetails, h("p", { class: "hint" },
      `${dollars(home_k * ltv * 1000)} opening mortgage at ${pct1(apr * 100)}, `
      + `${dollars(home_k * 1000)} collateral, ${dollars(home_k * income_frac * 1000)} annual income.`),
    h("dl", { class: "kv" },
      h("dt", {}, "External mortgage remaining"), h("dd", {}, usd(b.mortgage)),
      h("dt", {}, "BUCK principal outstanding"), h("dd", {}, `${bucks(b.buckPrincipal)} BUCK`),
      h("dt", {}, "Paid to the mortgage lender"), h("dd", {}, usd(b.paid))),
    b.error ? h("p", { class: "hint" }, b.error) : null,
    b.stale ? h("p", { class: "hint" }, "BUCK principal is from the last household mark.") : null);
    borrowerChart.update(rows.map((r) => r.day), [
      rows.map((r) => r.borrower?.mortgage ?? null),
      rows.map((r) => r.borrower && !r.borrower.stale && r.bu ? r.borrower.mortgage + r.borrower.buckPrincipal * r.bu : null),
      rows.map((r) => r.borrower?.baseline ?? null),
    ]);
  }

  function drawControls() {
    const st = S.status;
    const live = S.link?.state === "live";
    runBtn.disabled = !live;
    resetBtn.disabled = !(live || S.link?.state === "done");
    runBtn.textContent = st?.paused ? "Run" : "Pause";
    for (const b of stepBtns) b.disabled = !live;
    gasGroup.style.display = S.local ? "none" : "";
    for (const b of chainBtns) {
      b.setAttribute("aria-pressed", String(b.dataset.chain === (st?.chain ?? "l2")));
      b.disabled = !live;
    }
    if (st && live) {
      const bits = [st.paused ? `paused at day ${st.day}` : st.step_left !== null && st.step_left !== undefined
        ? `stepping (${st.step_left + 1} to go)` : "running",
        ...(S.local ? ["in this tab"] : [`wheel on ${(st.chain ?? "l2").toUpperCase()} gas`])];
      const last = S.rows[S.rows.length - 1];
      if (last?.shockActive) bits.push(`${last.shockActive} shock${last.shockActive > 1 ? "s" : ""} under way`);
      stateChip.textContent = bits.join(" · ");
    }
  }

  function drawWallet() {
    const last = S.rows[S.rows.length - 1];
    if (!S.saver || !last) {
      keyLine.textContent = "Your key appears once the world is built.";
      saveBtn.disabled = true;
      receiptsBox.replaceChildren();
      balancesBox.replaceChildren();
      return;
    }
    saveBtn.disabled = false;
    keyLine.replaceChildren("Your key (simulated, kept in this browser): ",
      h("code", { title: S.saver.address }, `${S.saver.address.slice(0, 6)}…${S.saver.address.slice(-4)}`));
    const opt = tokenSel.querySelector("option[value=hint]");
    if (opt) opt.textContent = `the basket's choice (${S.info.tokens[choice(last)]?.symbol ?? "?"})`;
    devs().forEach((d, i) => {
      const o = tokenSel.querySelector(`option[value="${i}"]`);
      if (o) {
        o.textContent = `${S.info.tokens[i].symbol}: pool ${(d / 100).toFixed(1)}% off its average`
          + (d > MAX_DEVIATION_BP ? " (refused now)" : "");
      }
    });

    const onChain = new Map((S.holdings?.receipts ?? []).map((r) => [r.id, r]));
    const list = receipts();
    // One block per receipt: what it would pay now against what it cost,
    // and against keeping the TOKEN instead -- the comparison that is exact.
    const blocks = list.map((rec) => {
      const tok = S.info.tokens[rec.i];
      const sym = tok?.symbol ?? "?";
      const head = (...extra) => h("div", { class: "receipt-head" },
        h("b", {}, `#${rec.id}`), ` ${sym}, saved on day ${rec.day}`, ...extra);
      if (rec.redeemedDay !== undefined) {
        const paidIn = (rec.paid ?? []).map(([i]) => (i < 0 ? "BUCK" : S.info.tokens[i]?.symbol))
          .filter(Boolean).join(", ");
        return h("li", { class: "receipt done" }, head(`, paid on day ${rec.redeemedDay}`),
          h("dl", { class: "kv" },
            h("dt", {}, "Saved"), h("dd", {}, usd(rec.usd)),
            h("dt", {}, `Paid${paidIn ? ` in ${paidIn}` : ""}`),
            h("dd", { class: "paid" }, usd(rec.paidUsd), " ", pct(rec.paidUsd / rec.usd - 1))));
      }
      const c = onChain.get(rec.id);
      const worth = !c?.live ? null
        : equityWorth(c.shares, c.basis, { sp: last.sp, lam: last.lam, chg: last.chg });
      const worthUsd = worth ? (Number(worth.paid) / 1e6) * last.bu : null;
      const markRoi = last.kind === "equity" && worth && c.basis > 0n
        ? Number(worth.claim - c.basis) / Number(c.basis) : null;
      const held = (Number(BigInt(rec.amount)) / 10 ** (tok?.decimals ?? 18)) * prices(last, rec.i).usdc;
      return h("li", { class: "receipt" },
        head(c?.live ? h("button", { type: "button", onclick: () => redeem(rec, worthUsd) }, "Redeem") : null),
        h("dl", { class: "kv" },
          h("dt", {}, "Saved"), h("dd", {}, usd(rec.usd)),
          last.kind === "equity" && worth ? [h("dt", {}, "Open basis (BUCK)"),
            h("dd", {}, compact(Number(c.basis) / 1e6)), h("dt", {}, "Share mark (BUCK)"),
            h("dd", {}, compact(Number(worth.claim) / 1e6), " ", markRoi === null ? "" : pct(markRoi))] : [],
          h("dt", { title: last.kind === "equity"
            ? "What redeeming would pay now, in BUCK valued at the BUCK/USDC pool"
            : "What redeeming would pay now, in TOKENs valued at their USDC pools" }, "Worth now"),
          h("dd", { class: "worth" }, worthUsd === null ? "…" : [usd(worthUsd), " ", pct(worthUsd / rec.usd - 1)],
            worth ? h("div", { class: "sub" }, last.kind === "equity"
              ? `${compact(Number(worth.paid) / 1e6)} BUCK`
              : `${compact(Number(worth.paid) / 1e6)} BUCK of TOKENs`) : null),
          h("dt", {}, `Kept the ${sym} instead`), h("dd", {}, usd(held), " ", pct(held / rec.usd - 1))));
    });
    fill(receiptsBox, list.length === 0 ? h("p", { class: "empty" }, "No savings yet.")
      : h("ul", { class: "receipts" }, blocks));

    const bal = (S.holdings?.balances ?? []).map((b, i) => [b, i]).filter(([b]) => b > 0n);
    const buck = S.holdings?.buck ?? 0n;
    fill(balancesBox, bal.length === 0 && buck === 0n ? null : [
      h("h3", { class: "grid-title" }, "In your wallet"),
      h("dl", { class: "kv wallet" },
        buck > 0n ? [h("dt", {}, "BUCK"), h("dd", {}, `${compact(Number(buck) / 1e6, 4)} (${usd((Number(buck) / 1e6) * last.bu)})`)] : [],
        bal.flatMap(([b, i]) => {
          const t = S.info.tokens[i];
          const n = Number(b) / 10 ** t.decimals;
          return [h("dt", {}, t.symbol), h("dd", {}, `${compact(n, 4)} (${usd(n * prices(last, i).usdc)})`)];
        }))]);
  }

  // Each TOKEN/BUCK pool's distance from its recent average (bp): the deposit guard's measure.
  const devs = () => (S.holdings?.ticks ?? []).map((t) => deviationBp(t.tick, t.twap));
  const choice = (last) => chooseIndex(S.holdings?.hint, last.weights, devs(), MAX_DEVIATION_BP);

  const pct = (x) => h("span", { class: x >= 0 ? "good" : "bad" }, `${x >= 0 ? "+" : ""}${(x * 100).toFixed(2)}%`);

  // The basket's commodities bought on HOLD_FROM_DAY at the savings index's
  // value that day, and held: a fixed bundle's value moves by eq_hold's ratio.
  const heldLine = (rows) => {
    const a = rows.findIndex((r) => r.day >= HOLD_FROM_DAY && r.hold > 0 && r.D !== null);
    return rows.map((r, i) => (a < 0 || i < a || !(r.hold > 0) ? null : rows[a].D * (r.hold / rows[a].hold)));
  };
  const overHolding = (rows) => {
    const held = heldLine(rows);
    const last = rows[rows.length - 1];
    const h0 = held[held.length - 1];
    return last && last.D !== null && h0 ? last.D / h0 - 1 : null;
  };

  S.activate = () => {
    if (S.started) return;
    S.started = true;
    connect();
  };
}
