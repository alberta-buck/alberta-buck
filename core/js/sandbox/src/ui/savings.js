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
import { MAX_DEVIATION_BP, Saver } from "../savings/wallet.js";
import { channels, chooseIndex, compact, dateOf, deviationBp, equityWorth, newSid, prices, receiptWorth, row,
         serverBase, tokenFor, usd } from "../savings/model.js";

const S1 = "var(--s1)";
const S2 = "var(--s2)";
const S3 = "var(--s3)";
const S4 = "var(--s4)";
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

  // World settings: where, and what a new world is made of.
  const serverIn = h("input", { type: "text", "aria-label": "sim server", dataset: { key: "savings:server-in" },
                                size: 30 });
  const pricesSel = h("select", { "aria-label": "prices" },
    h("option", { value: "history" }, "History (five years of real prices)"),
    h("option", { value: "revert" }, "Reverting (cycles about a mean)"));
  const seedIn = h("input", { type: "text", "aria-label": "seed", placeholder: "default", size: 8,
                              dataset: { key: "savings:seed" } });
  const worldLink = h("code", { class: "wrap" });

  const noServer = h("div", { class: "notice", hidden: true });
  const worldCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "The savings world"), stateChip),
    noServer,
    kpis,
    h("div", { class: "row ctl" }, runBtn, h("span", { class: "seg-label" }, "Step"), ...stepBtns,
      h("span", { class: "seg", role: "group", "aria-label": "the wheel's gas" },
        h("span", { class: "seg-label" }, "Gas"), ...chainBtns)),
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
          h("label", { class: "field" }, h("span", { class: "field-label" }, "Seed"), seedIn)),
        h("div", { class: "row" },
          h("button", { type: "button", class: "danger", onclick: () => newWorld() }, "New world")),
        h("p", { class: "hint" }, "A new world runs from its first day with these prices and seed; ",
          "this one keeps running on the server until it is idle for ten minutes.  ",
          "The link to this world (anyone holding it can watch and steer it): "), worldLink)));

  // ---- the saver's card --------------------------------------------------
  const saveUsd = h("input", { type: "number", min: "1", step: "any", value: "10000", "aria-label": "dollars to save",
                               dataset: { key: "savings:usd" } });
  const tokenSel = h("select", { "aria-label": "token to save in", dataset: { key: "savings:token" } });
  const saveBtn = h("button", { type: "button", class: "primary", onclick: () => save() }, "Save");
  const keyLine = h("p", { class: "hint" });
  const receiptsBox = h("div", {});
  const balancesBox = h("div", {});
  // How a saving works, by the world's basket (sim_info's basket_kind).
  const HOW = {
    prorata: ["Saving mints the TOKEN at its market price (the world's TOKENs are faucets), ",
      "deposits it and takes a receipt: a claim on the basket's pools.  The basket partners it with ",
      "freshly minted BUCK; redeeming burns that BUCK and pays you in TOKENs."],
    equity: ["Saving mints the TOKEN at its market price (the world's TOKENs are faucets), ",
      "deposits it and takes a receipt: shares of the basket's equity.  The basket draws K of credit ",
      "against it and its wheel places it in the pools; redeeming pays your shares' value in BUCK, less ",
      "the treasury's quarter of any gain.  Your key is registered as an identity (with the world's issuer) ",
      "before its first redemption: only identities may hold BUCK."],
  };
  const howLine = h("p", { class: "hint" }, HOW.prorata);
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
    buck: lineChart({ title: "BUCK", ref: 1, fmt: f3, series: [
      { label: "USD per BUCK (pool)", color: S1 }, { label: "the basket, in BUCK", color: S2 }],
      note: "K steers the basket's BUCK price to 1; the BUCK/USDC pool says what a BUCK fetches." }),
    k: lineChart({ title: "K: credit per unit of insured value", fmt: f4, series: [{ label: "K", color: S3 }] }),
    index: lineChart({ title: "The savings index", ref: 1, fmt: f4, series: [
      { label: "paid per BUCK saved", color: S1 }],
      note: "What a saving is worth, in BUCK value, per BUCK deposited (the equity basket: its share "
        + "price): 1 at the start, raised by the wheel's credits and the pools' harvest of the "
        + "commodities' cycles." }),
    wheel: lineChart({ title: "The work wheel (cumulative)", fmt: dollars, series: [
      { label: "to depositors", color: S1 }, { label: "to callers", color: S2 }, { label: "gas", color: S4 }],
      note: "Consistency cycles through TOKEN/USDC, TOKEN/BUCK and BUCK/USDC: the harvest reaches the depositors." }),
    ops: lineChart({ title: "The undertakings' open books (BUCK)", fmt: (v) => compact(v), series: [
      { label: "issued", color: S1 }, { label: "absorbed", color: S2 }] }),
  };
  const commodityGrid = h("div", { class: "chart-grid small" });
  S.commodity = [];

  fill(panel,
    h("p", { class: "intro" }, "A whole Alberta Buck economy runs on a server: five years of real commodity ",
      "prices (or their reverting twins: the same commodities cycling, trend removed), the BuckBasket and ",
      "its work wheel, the undertakings, K, arbitrageurs pinning every TOKEN to its commodity, depositors ",
      "and retirees.  Watch it day by day, shock it, and save in it: the basket harvests the commodities' ",
      "cycles and the gaps between BUCK, USDC and the TOKENs for its depositors.  It is a separate world ",
      "from the other tabs', which runs in your browser on a clock of its own."),
    h("div", { class: "cols savings-cols" },
      h("div", { class: "stack" }, worldCard, walletCard),
      h("div", { class: "stack" },
        h("div", { class: "chart-grid" }, charts.buck.el, charts.index.el, charts.wheel.el, charts.k.el,
          charts.ops.el),
        h("h3", { class: "grid-title" }, "Commodities: the real price, the TOKEN/USDC pool, and via BUCK"),
        commodityGrid)));

  // ---- behaviour ---------------------------------------------------------

  async function op(label, msg) {
    if (!S.link) return undefined;
    return ctx.act(label, async () => {
      await S.link.op(msg);
      setTimeout(pollStatus, 300);
    }, msg.op === "step" ? `${label}: queued for the day's end.` : "Queued for the day's end.");
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
    S.commodity = [];
    commodityGrid.replaceChildren();
    S.base = serverBase({ param: q.get("sim"), configured: ctx.simServer, saved: prefs.get("savings:server", ""),
                          location });
    serverIn.value = S.base;
    S.sid = q.get("world") || prefs.get("savings:sid", "") || newSid();
    prefs.set("savings:sid", S.sid);
    S.sets = jsonPref("savings:sets", {});
    pricesSel.value = S.sets["scenario.prices"] || "history";
    seedIn.value = S.sets["scenario.seed"] ?? "";
    const u = new URL(location.href);
    u.search = "";
    u.searchParams.set("tab", "savings");
    if (!ctx.sameOrigin) u.searchParams.set("sim", S.base);
    u.searchParams.set("world", S.sid);
    worldLink.textContent = u.toString();
    S.link = new SimLink({
      urls: channels(S.base, S.sid, S.sets),
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
    for (let i = 0; i < 60 && S.link; i++) {
      try {
        const info = await S.link.call("sim_info");
        if (info && info.basket) {
          S.info = info;
          const key = prefs.get("savings:key", "");
          S.saver = new Saver({ link: S.link, info, key });
          fill(howLine, HOW[info.basket_kind] ?? HOW.prorata);
          prefs.set("savings:key", S.saver.key);
          buildCommodities();
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
    if (!confirm("Start a new world?  This one keeps running until it is idle, but this tab leaves it.")) return;
    const sets = {};
    if (pricesSel.value !== "history") sets["scenario.prices"] = pricesSel.value;
    if (seedIn.value.trim()) sets["scenario.seed"] = seedIn.value.trim();
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
      { label: "real", color: S4, dash: true }, { label: "TOKEN/USDC", color: S1 }, { label: "via BUCK", color: S2 }] }));
    commodityGrid.replaceChildren(...S.commodity.map((c) => c.el));
    fill(tokenSel, h("option", { value: "hint" }, "the basket's choice"),
      S.info.tokens.map((t, i) => h("option", { value: String(i) }, t.symbol)));
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
      stat("Savings index", last.D === null ? "–" : last.D.toFixed(4), "Paid per BUCK saved, in BUCK value"),
      stat("To depositors", dollars(last.credited), "The work wheel's harvest, credited to the depositors"),
      stat("Pace", last.pace > 0 ? `${(1 / last.pace).toFixed(1)} s/day` : "–"),
      stat("Prices", !S.info ? "–" : String(S.info.prices?.[0] ?? "").startsWith("rev-") ? "reverting" : "history",
        "Set when the world is made (World settings)"),
    ] : [h("span", { class: "hint" }, S.link ? STATES[S.link.state] : "")]);
    drawControls();
    if (rows.length === 0) return;
    const xs = rows.map((r) => r.day);
    charts.buck.update(xs, [rows.map((r) => r.bu || null), rows.map((r) => r.bv || null)]);
    charts.k.update(xs, [rows.map((r) => r.k || null)]);
    charts.index.update(xs, [rows.map((r) => r.D)]);
    charts.wheel.update(xs, [rows.map((r) => r.credited), rows.map((r) => r.callers), rows.map((r) => r.gasUsd)]);
    charts.ops.update(xs, [rows.map((r) => r.utIssued), rows.map((r) => r.utAbsorbed)]);
    S.commodity.forEach((c, i) => {
      const p = rows.map((r) => prices(r, i));
      c.update(xs, [p.map((v) => v.ref || null), p.map((v) => v.usdc || null), p.map((v) => v.viaBuck || null)]);
    });
    drawWallet();
  }

  function drawControls() {
    const st = S.status;
    const live = S.link?.state === "live";
    runBtn.disabled = !live;
    runBtn.textContent = st?.paused ? "Run" : "Pause";
    for (const b of stepBtns) b.disabled = !live;
    for (const b of chainBtns) {
      b.setAttribute("aria-pressed", String(b.dataset.chain === (st?.chain ?? "l2")));
      b.disabled = !live;
    }
    if (st && live) {
      const bits = [st.paused ? `paused at day ${st.day}` : st.step_left !== null && st.step_left !== undefined
        ? `stepping (${st.step_left + 1} to go)` : "running", `wheel on ${(st.chain ?? "l2").toUpperCase()} gas`];
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

    const sv = { O: last.O, S: last.S, B: last.B };
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
      const worth = !c?.live ? null : last.kind === "equity"
        ? equityWorth(c.shares, c.basis, { sp: last.sp, lam: last.lam, chg: last.chg })
        : receiptWorth(c.buckPrincipal, sv);
      const worthUsd = worth ? (Number(worth.paid) / 1e6) * last.bu : null;
      const held = (Number(BigInt(rec.amount)) / 10 ** (tok?.decimals ?? 18)) * prices(last, rec.i).usdc;
      return h("li", { class: "receipt" },
        head(c?.live ? h("button", { type: "button", onclick: () => redeem(rec, worthUsd) }, "Redeem") : null),
        h("dl", { class: "kv" },
          h("dt", {}, "Saved"), h("dd", {}, usd(rec.usd)),
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

  S.activate = () => {
    if (S.started) return;
    S.started = true;
    connect();
  };
}
