// The world bar: always in view.  The simulated date and block, BUCK's
// price in the pool, BUCK_K, the supply and the insurance pool; the clock
// (+1 hour, +1 day, +30 days); the world itself (export, import, reset);
// and the journal of every transaction.

import { amount, download, fill, h, money } from "./dom.js";

const HOUR = 3_600;
const DAY = 86_400;

export function mountWorldBar(ctx) {
  const { app, act } = ctx;
  const picker = h("input", {
    type: "file", accept: "application/json,.json", hidden: true,
    onchange: async () => {
      const f = picker.files?.[0];
      picker.value = "";
      if (!f) return;
      const text = await f.text();
      await act(`Importing ${f.name}`, () => app.importText(text), "World imported.");
    },
  });
  const journalBtn = h("button", {
    type: "button", "aria-expanded": "false", "aria-controls": "journal",
    onclick: () => {
      const j = document.getElementById("journal");
      j.hidden = !j.hidden;
      journalBtn.setAttribute("aria-expanded", String(!j.hidden));
      if (!j.hidden) renderJournal(ctx, ctx.lastView);
    },
  }, "Journal");
  fill(document.getElementById("controls"),
    h("button", { type: "button", title: "Move the clock forward one hour",
                  onclick: () => act("Advancing one hour", () => app.advance(HOUR), "An hour passed.") }, "+1 hour"),
    h("button", { type: "button", title: "Move the clock forward one day",
                  onclick: () => act("Advancing one day", () => app.advance(DAY), "A day passed.") }, "+1 day"),
    h("button", { type: "button", title: "Move the clock forward thirty days",
                  onclick: () => act("Advancing thirty days", () => app.advance(30 * DAY),
                    "Thirty days passed: demurrage accrued, assets depreciated.") }, "+30 days"),
    journalBtn,
    h("button", {
      type: "button", title: "Save this whole world to a file",
      onclick: () => act("Exporting", async () => {
        const text = await app.exportText();
        const v = ctx.lastView?.status;
        download(`alberta-buck-sandbox-day-${v ? v.day : 0}.json`, text);
      }, "World exported."),
    }, "Export"),
    h("button", { type: "button", title: "Replace this world with one from a file",
                  onclick: () => picker.click() }, "Import"),
    h("button", {
      type: "button", class: "danger", title: "Start a new world (this one is lost unless exported)",
      onclick: () => {
        if (!confirm("Start a new world?  This one is gone unless you exported it.")) return;
        act("Deploying a new world", () => app.reset(), "A new world.");
      },
    }, "Reset"),
    picker,
  );
}

const stat = (k, ...v) => h("div", { class: "stat" }, h("span", { class: "k" }, k),
  h("span", { class: "v" }, ...v));

export function renderWorldBar(ctx, view) {
  const s = view.status;
  const price = Number(s.buckPrice) / 1e6;
  fill(document.getElementById("stats"),
    stat("Day", `${s.day}`, h("span", { class: "unit" }, ` ${s.date.slice(0, 10)}`)),
    stat("Block", `${s.block}`),
    stat("BUCK", h("span", { title: `${amount(s.buckPrice, 6)} USDC per BUCK in the BUCK/USDC pool` },
      `$${price.toFixed(4)}`)),
    stat("BUCK_K", h("span", { title: "Credit per unit of insured value (the controller's output)" },
      (Number(s.buckK) / 1e18).toFixed(4))),
    stat("Supply", money(s.supply, "BUCK", 0)),
    stat("Insurance pool", money(s.insurancePool, "BUCK")),
    stat("Wallets", `${s.wallets}`),
  );
  const notice = document.getElementById("notice");
  const notes = [view.notice, ctx.storageNote].filter(Boolean);
  notice.hidden = notes.length === 0;
  notice.textContent = notes.join("  ");
  if (!document.getElementById("journal").hidden) renderJournal(ctx, view);
}

function renderJournal(ctx, view) {
  if (!view) return;
  const rows = [...view.journal].reverse().slice(0, 300);
  fill(document.getElementById("journal"),
    h("div", { class: "card-head" },
      h("h2", {}, "Journal"),
      h("button", { type: "button", onclick: () => {
        document.getElementById("journal").hidden = true;
      } }, "Close")),
    h("p", { class: "hint" }, "Every transaction this world has sent, newest first: what it was, ",
      "whether it went through, and the gas it used.  Refusals are part of the story."),
    h("ol", {}, rows.map((e) => h("li", {},
      h("span", { class: e.outcome === "ok" ? "ok" : "revert" }, e.outcome === "ok" ? "ok" : "refused"),
      h("span", { title: e.err || "" }, e.tag || e.fn),
      h("span", { class: "gas" }, `${e.gas.toLocaleString()} gas`)))),
  );
}
