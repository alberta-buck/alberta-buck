// Credit: Sandbox Mutual insures simulated assets.  An insured asset becomes
// a BuckCredit NFT held by its owner; the owner activates credit against it,
// depositing ten years' premium with the insurance pool, which invests it to
// earn the premiums and returns it when the insurance is dropped -- a deposit
// the funding gate lets through only once the owner already holds as much.
// The credits' depreciated values move as the clock does.

import { ASSET_CLASSES } from "../app.js";
import { amount, field, fill, h, money, parseAmount, preserving } from "./dom.js";

const DEP_NAMES = ["none", "linear", "declining balance"];
const pct = (bp) => `${(Number(bp) / 100).toLocaleString(undefined, { maximumFractionDigits: 2 })} %`;
const toBp = (text, what) => {
  const t = String(text ?? "").trim();
  if (!/^\d+(\.\d{1,2})?$/.test(t)) {
    throw Object.assign(new Error(`${what}: a percentage`), { reason: `${what}: a percentage` });
  }
  return Math.round(Number(t) * 100);
};

function schedule(c) {
  if (c.depType === 0) return "none";
  return `${DEP_NAMES[c.depType]} ${pct(c.depRate)}/yr`;
}

export function mountCredit(ctx) {
  const { app, act } = ctx;
  const panel = document.getElementById("panel-credit");

  // ---- insure -------------------------------------------------------------
  const holder = h("select", { "data-key": "credit:holder", "aria-label": "holder" });
  const cls = h("select", { "aria-label": "asset class" },
    ASSET_CLASSES.map((c) => h("option", { value: c.key }, c.label)));
  const face = h("input", { inputmode: "decimal", placeholder: "BUCKs", value: "400000" });
  const floor = h("input", { inputmode: "decimal", placeholder: "BUCKs" });
  const dep = h("select", { "aria-label": "depreciation" },
    DEP_NAMES.map((n, i) => h("option", { value: String(i) }, n)));
  const rate = h("input", { inputmode: "decimal", placeholder: "% a year" });
  const premium = h("input", { inputmode: "decimal", placeholder: "% a year" });
  const note = h("p", { class: "hint" });
  let floorTouched = false;
  floor.addEventListener("input", () => { floorTouched = true; });
  const defaults = () => {
    const c = ASSET_CLASSES.find((a) => a.key === cls.value);
    dep.value = String(c.depType);
    rate.value = String(c.depRate / 100);
    premium.value = String(c.premiumRate / 100);
    note.textContent = `${c.label}: ${c.note}.`;
    if (!floorTouched) {
      try {
        floor.value = amount(parseAmount(face.value) * BigInt(c.floorBp) / 10_000n, 6).replace(/,/g, "")
          .replace(/\.?0+$/, "");
      } catch {
        floor.value = "";
      }
    }
  };
  cls.addEventListener("change", () => { floorTouched = false; defaults(); });
  face.addEventListener("input", defaults);
  cls.value = "home";
  defaults();

  const insure = h("form", {
    class: "card",
    onsubmit: async (e) => {
      e.preventDefault();
      await act("Insuring", () => app.insure(holder.value, {
        assetClass: cls.value, face: parseAmount(face.value, "face value"),
        floor: floor.value.trim() ? parseAmount(floor.value, "floor") : 0n,
        depType: Number(dep.value), depRate: toBp(rate.value, "depreciation"),
        premiumRate: toBp(premium.value, "premium"),
      }), (id) => `Insured: credit #${id}.`);
    },
  },
    h("h2", {}, "Insure an asset"),
    h("p", { class: "hint" }, "Sandbox Mutual appraises a simulated asset and issues a BuckCredit ",
      "NFT to its owner, on a depreciation schedule and a premium.  Nothing is activated yet."),
    field("Owner", holder),
    h("div", { class: "row" }, field("Asset", cls), field("Value (BUCKs)", face)),
    note,
    h("div", { class: "row" }, field("Floor (BUCKs)", floor), field("Depreciation", dep),
      field("Rate, % a year", rate)),
    field("Premium, % of insured value a year", premium,
      "Not spent: activating credit deposits ten years' worth with the insurance pool, which " +
      "invests it to earn the premiums and returns it when the insurance is dropped."),
    h("button", { type: "submit", class: "primary" }, "Insure"),
    h("p", { class: "discloses" }, h("b", {}, "Discloses: "), "the owner's address, the insurer, ",
      "the asset class, its value, schedule and premium, to everyone.  Not whose asset it is."));

  // ---- activate -----------------------------------------------------------
  const who = h("select", { "data-key": "credit:who", "aria-label": "wallet" });
  const amt = h("input", { "data-key": "credit:amount", inputmode: "decimal", placeholder: "BUCKs",
                           value: "50000" });
  const quote = h("div", { class: "quote", "aria-live": "polite" });
  let seq = 0;
  const requote = async () => {
    const mine = ++seq;
    if (!who.value) return fill(quote, h("p", { class: "hint" }, "Insure an asset first."));
    let q;
    try {
      q = await app.quote(who.value, parseAmount(amt.value, "amount"));
    } catch (e) {
      if (mine === seq) fill(quote, h("p", { class: "hint" }, e.reason ?? e.message));
      return;
    }
    if (mine !== seq) return;
    const w = ctx.lastView?.wallets.find((x) => x.id === who.value);
    fill(quote,
      h("dl", { class: "kv" },
        h("dt", { title: "Face value activated on the credits drawn" }, "Coverage"),
        h("dd", {}, money(q.coverage, "BUCK")),
        h("dt", { title: "Ten years of premium, deposited with the insurance pool; returned when the " +
                               "insurance is dropped" }, "Premium deposit"),
        h("dd", {}, money(q.principal, "BUCK")),
        h("dt", { title: "The deposit times the funding factor" }, "Must already hold"),
        h("dd", {}, money(q.required, "BUCK")),
        h("dt", { title: "Held BUCKs plus unused credit" }, "Holds"), h("dd", {}, money(q.balance, "BUCK")),
        h("dt", {}, "Shortfall"), h("dd", { class: q.shortfall ? "bad" : "good" }, money(q.shortfall, "BUCK"))),
      q.shortfall === 0n
        ? h("button", {
          type: "button", class: "primary",
          onclick: () => act("Activating credit", () => app.activate(who.value, parseAmount(amt.value)),
            (m) => `Activated: ${amount(m.premium)} BUCKs of premium deposited with the insurance pool.`),
        }, "Activate")
        : w?.trading
          ? h("button", {
            type: "button", class: "primary",
            onclick: () => act(`Buying ${amount(q.buy)} BUCKs`, () => app.buy(who.value, { buck: q.buy }),
              (r) => `Bought ${amount(r.received)} BUCKs for ${amount(r.paid)} USDC.`),
          }, `Buy ${amount(q.buy)} BUCKs first`)
          : h("p", { class: "hint" }, "Buying the shortfall needs trading open: see Wallets."));
  };
  who.addEventListener("change", requote);
  amt.addEventListener("input", requote);
  ctx.credit = { holder, who, requote, list: h("div", {}) };

  const activate = h("div", { class: "card form" },
    h("h2", {}, "Activate credit"),
    h("p", { class: "hint" }, "The owner draws on their insured value: BUCK_K of it becomes credit ",
      "they can spend.  Its premium deposit goes to the insurance pool now, and the funding gate ",
      "lets that through only once the owner already holds as much."),
    h("div", { class: "row" }, field("Wallet", who), field("Amount (BUCKs)", amt)),
    quote,
    h("p", { class: "discloses" }, h("b", {}, "Discloses: "), "the amount, the coverage and the ",
      "premium, to everyone."));

  fill(panel,
    h("p", { class: "intro" }, "Money issued against insured real assets: an insurer vouches for ",
      "an asset's value and how it wears; its owner can then spend part of that value as BUCKs, ",
      "depositing the premium with a mutual insurance pool that invests it to pay for the cover."),
    h("div", { class: "cols" }, h("div", { class: "form" }, insure, activate),
      h("div", {}, h("h2", {}, "Credits"),
        h("p", { class: "hint" }, "Values depreciate on their schedules as the clock moves; try +30 days."),
        ctx.credit.list)));
}

export function renderCredit(ctx, view) {
  const registered = view.wallets.filter((w) => w.registered);
  const withCredit = registered.filter((w) => view.credits.some((c) => c.wallet === w.id));
  const opts = (ws) => ws.map((w) => h("option", { value: w.id }, `${w.id} ${w.label}`));
  preserving(ctx.credit.holder.parentElement.parentElement, () => {
    fill(ctx.credit.holder, opts(registered));
    fill(ctx.credit.who, opts(withCredit));
  });
  const label = (id) => view.wallets.find((w) => w.id === id)?.label ?? id;
  fill(ctx.credit.list, view.credits.length === 0
    ? h("p", { class: "empty" }, "No credits yet.")
    : h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Credit", "Value", "Depreciation", "Premium", "Activated",
        "Worth now", "Activated, now"].map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, view.credits.map((c) => h("tr", { "data-credit": String(c.tokenId) },
        h("td", {}, h("b", {}, ASSET_CLASSES.find((a) => a.key === c.className)?.label ?? c.className),
          ` #${c.tokenId}`, h("div", { class: "sub" }, label(c.wallet))),
        h("td", { class: "r" }, money(c.face, "BUCK", 0)),
        h("td", { title: c.depType ? `floor ${amount(c.floor, 0)} BUCKs` : "" }, schedule(c)),
        h("td", { class: "r" }, `${pct(c.premiumRate)}/yr`),
        h("td", { class: "r" }, money(c.activated, "BUCK", 0)),
        h("td", { class: "r", "data-col": "worth", title: "The whole asset, depreciated to today" },
          money(c.depreciatedFace, "BUCK", 0)),
        h("td", { class: "r", "data-col": "activated-now", title: "The activated part, depreciated to today" },
          money(c.currentValue, "BUCK")),
      ))))));
  ctx.credit.requote();
}
